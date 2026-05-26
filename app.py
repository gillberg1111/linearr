"""Flask web UI for managing rotating playlists across Plex and Jellyfin."""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)

load_dotenv()

import db  # noqa: E402
import scheduler  # noqa: E402
import service  # noqa: E402
from media_client import (  # noqa: E402
    available_backends,
    get_client,
    normalize_title,
    titles_match,
)
from rotation import VALID_SORT_MODES  # noqa: E402
from service import ShowConfig  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")


# --------------------------------------------------------------------------- #
# Show aggregation across configured backends
# --------------------------------------------------------------------------- #


def _aggregated_shows() -> list[dict]:
    """List every show across configured backends, deduplicated by
    title+year. Each row carries `plex_rating_key` and `jellyfin_rating_key`
    (each nullable) plus a `backends` set indicating which backends have it.

    Single-backend installs get the same list shape that `client.list_all_shows()`
    returns, just wrapped in dicts.
    """
    backends = available_backends()
    out: list[dict] = []
    # Key on normalized title; year only disambiguates when both sides have
    # non-None years that differ (e.g. reboots). If one backend omits the
    # year, we merge — same logic as titles_match().
    seen: dict[str, list[int]] = {}  # normalized title -> indices in out

    for backend in backends:
        try:
            shows = get_client(backend).list_all_shows()
        except Exception:
            log.exception("Failed to list shows on %s", backend)
            continue
        for s in shows:
            nk = normalize_title(s.title)
            candidates = seen.get(nk, [])
            merged = False
            for idx in candidates:
                existing = out[idx]
                # Only split into separate entries when both sides carry a
                # non-None year and they differ.
                if (
                    existing["year"] is not None
                    and s.year is not None
                    and existing["year"] != s.year
                ):
                    continue
                # Compatible — merge into this row.
                existing[f"{backend}_rating_key"] = s.rating_key
                existing["backends"].add(backend)
                if not existing["year"] and s.year:
                    existing["year"] = s.year
                if not existing["thumb"] and s.thumb:
                    existing["thumb"] = s.thumb
                    existing["thumb_backend"] = backend
                merged = True
                break
            if merged:
                continue
            row = {
                "rating_key": s.rating_key,
                "title": s.title,
                "year": s.year,
                "library": s.library,
                "thumb": s.thumb,
                "thumb_backend": backend if s.thumb else None,
                "plex_rating_key": s.rating_key if backend == "plex" else None,
                "jellyfin_rating_key": s.rating_key if backend == "jellyfin" else None,
                "backends": {backend},
            }
            seen.setdefault(nk, []).append(len(out))
            out.append(row)

    out.sort(key=lambda r: r["title"].lower())
    return out


def _lookup_show_record(aggregated: list[dict], rating_key: str) -> dict | None:
    """Find an aggregated-show row by its source-backend rating_key."""
    for r in aggregated:
        if r["rating_key"] == rating_key:
            return r
        # Same record might be addressed via either ID when the user has both:
        if r.get("plex_rating_key") == rating_key or r.get("jellyfin_rating_key") == rating_key:
            return r
    return None


# --------------------------------------------------------------------------- #
# Form parsing
# --------------------------------------------------------------------------- #


def _parse_excluded_form_value(raw: str) -> set[tuple[int, int]]:
    """Parse the hidden `exclude_<rk>` form field ('S:E,S:E,...') into a set."""
    out: set[tuple[int, int]] = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            s_str, e_str = token.split(":", 1)
            out.add((int(s_str), int(e_str)))
        except ValueError:
            continue
    return out


def _parse_configs_from_form(
    form,
    show_keys: list[str],
    aggregated: list[dict] | None = None,
) -> list[ShowConfig]:
    """Pull per-show fields out of the configure form.

    Field names: start_<rk>, end_<rk>, specials_<rk>, include_movies_<rk>,
    movies_<rk> (multi-valued), jf_movies_<rk> (multi-valued — Jellyfin movie IDs).
    """
    configs: list[ShowConfig] = []
    for rk in show_keys:
        start = form.get(f"start_{rk}", "1")
        end = form.get(f"end_{rk}", "")
        specials = form.get(f"specials_{rk}", "")
        inc_movies = form.get(f"include_movies_{rk}", "")
        selected_movies = form.getlist(f"movies_{rk}")
        selected_jf_movies = form.getlist(f"jf_movies_{rk}")
        try:
            start_i = max(1, int(start))
        except ValueError:
            start_i = 1
        try:
            end_i = int(end) if end.strip() else None
        except ValueError:
            end_i = None
        if end_i is not None and end_i < start_i:
            end_i = None

        # Aggregated lookup gives us both Plex and Jellyfin IDs (when both
        # backends have the show). When only one backend is configured,
        # aggregated may be None and we fall back to ShowConfig's __post_init__
        # auto-fill (numeric rating_key → plex_rating_key).
        plex_id = None
        jf_id = None
        title = ""
        thumb = None
        if aggregated:
            rec = _lookup_show_record(aggregated, rk)
            if rec:
                plex_id = rec.get("plex_rating_key")
                jf_id = rec.get("jellyfin_rating_key")
                title = rec.get("title") or ""
                thumb = rec.get("thumb")

        excluded = _parse_excluded_form_value(form.get(f"exclude_{rk}", ""))
        try:
            weight_i = max(1, int(form.get(f"weight_{rk}", "1") or "1"))
        except ValueError:
            weight_i = 1
        configs.append(
            ShowConfig(
                rating_key=rk,
                title=title,
                thumb=thumb,
                start_season=start_i,
                end_season=end_i,
                include_specials=bool(specials),
                include_movies=bool(inc_movies),
                movie_rating_keys=[k for k in selected_movies if k],
                plex_rating_key=plex_id,
                jellyfin_rating_key=jf_id,
                jellyfin_movie_rating_keys=[k for k in selected_jf_movies if k],
                excluded_episodes=excluded,
                weight=weight_i,
            )
        )
    return configs


def _missing_side_shows(
    configs: list[ShowConfig], backend_choice: str, available: list[str]
) -> list[dict]:
    """Return [{title, missing}] for shows that lack an id on a targeted backend.

    Empty when only one backend is configured (no triple-pill in the UI, so
    no concept of missing-side). Empty when backend_choice is single and all
    shows have that side's id.
    """
    if len(available) < 2:
        return []
    out: list[dict] = []
    target_backends = ["plex", "jellyfin"] if backend_choice == "both" else [backend_choice]
    for tb in target_backends:
        label = "Plex" if tb == "plex" else "Jellyfin"
        for c in configs:
            if c.id_for(tb) is None:
                out.append({"title": c.title or c.rating_key, "missing": label})
    return out


def _gather_season_meta(configs: list[ShowConfig], primary_backend: str) -> dict:
    """Per-show {summary, seasons, movies} keyed by ShowConfig.rating_key.

    `primary_backend` is the backend we query for season/movie metadata in the
    UI. For 'both' playlists we still use one backend's metadata for the
    configure page (typically Plex, since it's more featureful for movies).
    Jellyfin-only shows fall back to the Jellyfin backend automatically.
    """
    out: dict = {}
    client_primary = get_client(primary_backend) if primary_backend in available_backends() else None
    client_jf = get_client("jellyfin") if "jellyfin" in available_backends() else None

    for cfg in configs:
        # Pick the backend that actually has this show.
        if primary_backend == "plex" and cfg.plex_rating_key and client_primary:
            tb, target_id, client = "plex", cfg.plex_rating_key, client_primary
        elif cfg.jellyfin_rating_key and client_jf:
            tb, target_id, client = "jellyfin", cfg.jellyfin_rating_key, client_jf
        elif cfg.plex_rating_key and client_primary:
            tb, target_id, client = "plex", cfg.plex_rating_key, client_primary
        else:
            continue
        try:
            summary = client.get_show_summary(target_id)
            seasons = client.season_summaries(target_id)
            movies = client.find_associated_movies(summary.title)
            out[cfg.rating_key] = {
                "summary": summary,
                "seasons": seasons,
                "movies": movies,
                "source_backend": tb,
            }
        except Exception:
            log.exception("metadata fetch failed for %s on %s", target_id, tb)
    return out


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET") or "dev-secret-change-me"

    db.init_db()
    scheduler.start()

    # Make available_backends visible to all templates (used by the picker
    # and badges to decide what to render).
    @app.context_processor
    def _inject_backends():
        return {"AVAILABLE_BACKENDS": available_backends()}

    # ------------------------------------------------------------------ #
    # Thumb proxy — dispatches by thumb reference shape
    # ------------------------------------------------------------------ #
    @app.route("/thumb")
    def thumb():
        ref = request.args.get("path") or ""
        if not ref:
            abort(400)
        explicit_backend = request.args.get("b")
        try:
            w = int(request.args.get("w", 240))
            h = int(request.args.get("h", 360))
        except ValueError:
            w, h = 240, 360

        # Dispatch: explicit b= wins; otherwise infer from shape.
        # Plex thumb refs start with '/'. Jellyfin refs are bare GUIDs.
        if explicit_backend in ("plex", "jellyfin"):
            backend = explicit_backend
        elif ref.startswith("/"):
            backend = "plex"
        else:
            backend = "jellyfin"

        if backend not in available_backends():
            abort(404)
        try:
            data, ctype = get_client(backend).fetch_image(ref, width=w, height=h)
        except Exception:
            log.exception("thumb fetch failed: %s on %s", ref, backend)
            abort(502)
        resp = Response(data, mimetype=ctype)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    # ------------------------------------------------------------------ #
    # Episodes list (JSON) — used by the per-episode exclusion picker
    # ------------------------------------------------------------------ #
    @app.route("/api/episodes/<rk>")
    def api_episodes(rk: str):
        backend = request.args.get("b") or (available_backends() or ["plex"])[0]
        if backend not in available_backends():
            abort(400)
        try:
            eps = get_client(backend).episodes_for_show(
                rk, start_season=1, end_season=None, include_specials=True
            )
        except Exception:
            log.exception("api_episodes failed for %s on %s", rk, backend)
            abort(502)
        return jsonify([
            {"season": e.season, "episode": e.episode, "title": e.title,
             "air_date": e.air_date}
            for e in eps
        ])

    # ------------------------------------------------------------------ #
    # AJAX preview (returns rendered HTML partial)
    # ------------------------------------------------------------------ #
    @app.route("/api/preview", methods=["POST"])
    @app.route("/api/preview/<int:playlist_id>", methods=["POST"])
    def api_preview(playlist_id: int | None = None):
        show_keys = request.form.getlist("shows")
        # Aggregated lookup only needed when both backends are configured.
        agg = _aggregated_shows() if len(available_backends()) > 1 else None
        configs = _parse_configs_from_form(request.form, show_keys, aggregated=agg)

        if playlist_id is not None:
            view = service.get_playlist_view(playlist_id)
            if not view:
                return ("", 404)
            sort_mode = view.sort_mode
            unwatched_only = view.unwatched_only
            preview_backend = "jellyfin" if view.backend == "jellyfin" else "plex"
            block_size = view.block_size
            shuffle_seed = view.shuffle_seed
            existing_rows = db.list_shows(playlist_id)
            existing_configs = [service._config_from_row(r) for r in existing_rows]
            all_configs = existing_configs + configs
        else:
            sort_mode = request.form.get("sort_mode", "rotation")
            if sort_mode not in VALID_SORT_MODES:
                sort_mode = "rotation"
            unwatched_only = bool(request.form.get("unwatched_only"))
            target_backend = request.form.get("backend") or available_backends()[0]
            preview_backend = "jellyfin" if target_backend == "jellyfin" else "plex"
            try:
                block_size = max(1, int(request.form.get("block_size", "1") or "1"))
            except ValueError:
                block_size = 1
            # Preview uses a stable seed during a single editing session so
            # the user doesn't see a fresh random order on every keystroke.
            shuffle_seed = 12345 if sort_mode == "shuffle_chronological" else None
            all_configs = configs

        try:
            preview = service.preview_playlist(
                all_configs,
                limit=2000,
                sort_mode=sort_mode,
                unwatched_only=unwatched_only,
                backend=preview_backend,
                block_size=block_size,
                shuffle_seed=shuffle_seed,
            )
        except Exception:
            log.exception("api_preview failed")
            preview = []
        return render_template("_preview_partial.html", preview=preview)

    # ------------------------------------------------------------------ #
    # Index
    # ------------------------------------------------------------------ #
    @app.route("/")
    def index():
        views = service.list_playlist_views()
        return render_template("index.html", playlists=views)

    # ------------------------------------------------------------------ #
    # Create playlist: pick → configure → commit
    # ------------------------------------------------------------------ #
    @app.route("/new", methods=["GET"])
    def new_playlist():
        backends = available_backends()
        if not backends:
            flash("No backends configured. Set PLEX_URL+PLEX_TOKEN and/or "
                  "JELLYFIN_URL+JELLYFIN_USERNAME+JELLYFIN_PASSWORD.", "error")
            return render_template("new.html", shows=[], prev_name="", selected=set(),
                                   default_backend="plex")
        try:
            shows = _aggregated_shows()
        except Exception as e:
            log.exception("listing shows failed")
            flash(f"Couldn't reach backend: {e}", "error")
            shows = []
        prev_name = request.args.get("name", "")
        selected = {k for k in request.args.get("selected", "").split(",") if k}
        return render_template(
            "new.html",
            shows=shows,
            prev_name=prev_name,
            selected=selected,
            default_backend=("both" if len(backends) > 1 else backends[0]),
        )

    @app.route("/new/configure", methods=["POST"])
    def new_configure():
        name = (request.form.get("name") or "").strip()
        show_keys = request.form.getlist("shows")
        if not name:
            flash("Playlist name is required.", "error")
            return redirect(url_for("new_playlist"))
        if not show_keys:
            flash("Pick at least one show.", "error")
            return redirect(url_for("new_playlist"))

        action = request.form.get("action", "preview")
        sort_mode = request.form.get("sort_mode", "rotation")
        if sort_mode not in VALID_SORT_MODES:
            sort_mode = "rotation"
        try:
            block_size = max(1, int(request.form.get("block_size", "1") or "1"))
        except ValueError:
            block_size = 1
        unwatched_only = bool(request.form.get("unwatched_only"))
        auto_sync = bool(request.form.get("auto_sync")) if "shows" in request.form else True

        backends = available_backends()
        backend_choice = request.form.get("backend")
        if backend_choice not in ("plex", "jellyfin", "both") or backend_choice not in backends + (["both"] if len(backends) > 1 else []):
            backend_choice = "both" if len(backends) > 1 else (backends[0] if backends else "plex")
        primary_backend = "plex" if "plex" in (backends if backend_choice == "both" else [backend_choice]) else "jellyfin"

        agg = _aggregated_shows() if len(backends) > 1 else None
        configs = _parse_configs_from_form(request.form, show_keys, aggregated=agg)
        meta = _gather_season_meta(configs, primary_backend)
        missing_shows = _missing_side_shows(configs, backend_choice, backends)

        if action == "commit":
            try:
                pid = service.create_managed_playlist(
                    name, configs,
                    sort_mode=sort_mode,
                    unwatched_only=unwatched_only,
                    auto_sync=auto_sync,
                    backend=backend_choice,
                    block_size=block_size,
                )
            except Exception as e:
                log.exception("create failed")
                flash(f"Failed to create playlist: {e}", "error")
                return render_template(
                    "configure.html",
                    mode="new",
                    form_action=url_for("new_configure"),
                    hidden={"name": name},
                    show_keys=show_keys,
                    meta=meta,
                    configs={c.rating_key: c for c in configs},
                    preview=[],
                    name=name,
                    sort_mode=sort_mode,
                    block_size=block_size,
                    unwatched_only=unwatched_only,
                    auto_sync=auto_sync,
                    backend=backend_choice,
                    missing_shows=missing_shows,
                    preview_api_url=url_for("api_preview"),
                )
            flash(f"Created '{name}'.", "ok")
            return redirect(url_for("view_playlist", playlist_id=pid))

        try:
            preview = service.preview_playlist(
                configs, limit=2000, sort_mode=sort_mode, unwatched_only=unwatched_only,
                backend=primary_backend, block_size=block_size,
                shuffle_seed=12345 if sort_mode == "shuffle_chronological" else None,
            )
        except Exception:
            log.exception("preview failed")
            preview = []
        return render_template(
            "configure.html",
            mode="new",
            form_action=url_for("new_configure"),
            hidden={"name": name},
            show_keys=show_keys,
            meta=meta,
            configs={c.rating_key: c for c in configs},
            preview=preview,
            name=name,
            sort_mode=sort_mode,
            block_size=block_size,
            unwatched_only=unwatched_only,
            auto_sync=auto_sync,
            backend=backend_choice,
            missing_shows=missing_shows,
            preview_api_url=url_for("api_preview"),
        )

    # ------------------------------------------------------------------ #
    # Playlist detail
    # ------------------------------------------------------------------ #
    @app.route("/playlist/<int:playlist_id>")
    def view_playlist(playlist_id: int):
        view = service.get_playlist_view(playlist_id)
        if not view:
            abort(404)

        # Available-to-add list, filtered to exclude shows already in the playlist.
        try:
            agg = _aggregated_shows()
        except Exception:
            agg = []
        existing_keys = {s["show_rating_key"] for s in view.shows}
        # Also exclude by Plex or Jellyfin id match (added via other backend).
        existing_plex = {s.get("plex_show_item_id") for s in view.shows if s.get("plex_show_item_id")}
        existing_jf = {s.get("jellyfin_show_item_id") for s in view.shows if s.get("jellyfin_show_item_id")}
        available = [
            r for r in agg
            if r["rating_key"] not in existing_keys
            and r.get("plex_rating_key") not in existing_plex
            and r.get("jellyfin_rating_key") not in existing_jf
        ]

        # Build the missing-side warning data: any show that lacks an id
        # for a backend this playlist targets is reported. Most actionable
        # for 'both' playlists; harmless for single-backend ones (those will
        # almost never have missing ids thanks to the add-time flow).
        missing_on = []
        for s in view.shows:
            if view.backend in ("both", "jellyfin") and not s.get("jellyfin_show_item_id"):
                missing_on.append({"title": s["show_title"], "missing": "Jellyfin"})
            if view.backend in ("both", "plex") and not s.get("plex_show_item_id"):
                missing_on.append({"title": s["show_title"], "missing": "Plex"})

        selected = {k for k in request.args.get("selected", "").split(",") if k}
        return render_template(
            "playlist.html",
            playlist=view,
            available=available,
            selected=selected,
            missing_on=missing_on,
        )

    # ------------------------------------------------------------------ #
    # Add to existing playlist: pick → configure → commit
    # ------------------------------------------------------------------ #
    @app.route("/playlist/<int:playlist_id>/add/configure", methods=["POST"])
    def add_configure(playlist_id: int):
        view = service.get_playlist_view(playlist_id)
        if not view:
            abort(404)
        show_keys = request.form.getlist("shows")
        if not show_keys:
            flash("Pick at least one show to add.", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))

        action = request.form.get("action", "preview")
        agg = _aggregated_shows() if len(available_backends()) > 1 else None
        configs = _parse_configs_from_form(request.form, show_keys, aggregated=agg)
        primary_backend = "jellyfin" if view.backend == "jellyfin" else "plex"
        meta = _gather_season_meta(configs, primary_backend)
        missing_shows = _missing_side_shows(configs, view.backend, available_backends())

        if action == "commit":
            try:
                service.add_shows_to_playlist(playlist_id, configs)
            except Exception as e:
                log.exception("add failed")
                flash(f"Failed to add shows: {e}", "error")
                return redirect(url_for("view_playlist", playlist_id=playlist_id))
            flash(f"Added {len(configs)} show(s).", "ok")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))

        existing_rows = db.list_shows(playlist_id)
        existing_configs = [service._config_from_row(r) for r in existing_rows]
        try:
            preview = service.preview_playlist(
                existing_configs + configs,
                limit=2000,
                sort_mode=view.sort_mode,
                unwatched_only=view.unwatched_only,
                backend=primary_backend,
                block_size=view.block_size,
                shuffle_seed=view.shuffle_seed,
            )
        except Exception:
            log.exception("preview failed")
            preview = []

        return render_template(
            "configure.html",
            mode="add",
            form_action=url_for("add_configure", playlist_id=playlist_id),
            hidden={},
            show_keys=show_keys,
            meta=meta,
            configs={c.rating_key: c for c in configs},
            preview=preview,
            name=view.name,
            playlist_id=playlist_id,
            sort_mode=view.sort_mode,
            block_size=view.block_size,
            unwatched_only=view.unwatched_only,
            auto_sync=view.auto_sync,
            backend=view.backend,
            missing_shows=missing_shows,
            preview_api_url=url_for("api_preview", playlist_id=playlist_id),
        )

    @app.route("/playlist/<int:playlist_id>/remove", methods=["POST"])
    def remove_show(playlist_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            service.remove_show_from_playlist(playlist_id, show)
        except Exception as e:
            log.exception("remove failed")
            flash(f"Failed to remove show: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Show removed.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    _MODE_LABELS = {
        "rotation": "Rotation",
        "rotation_weighted": "Weighted Rotation",
        "rotation_blocks": "Block Scheduling",
        "air_date": "Air Date",
        "shuffle_chronological": "Shuffle",
    }

    @app.route("/playlist/<int:playlist_id>/sort_mode", methods=["POST"])
    def change_sort_mode(playlist_id: int):
        mode = (request.form.get("sort_mode") or "").strip()
        if mode not in VALID_SORT_MODES:
            abort(400)
        try:
            service.set_playlist_sort_mode(playlist_id, mode)
        except Exception as e:
            log.exception("sort_mode change failed")
            flash(f"Failed to change sort: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Sort mode set to {_MODE_LABELS.get(mode, mode)}.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/block_size", methods=["POST"])
    def change_block_size(playlist_id: int):
        try:
            size = max(1, int(request.form.get("block_size", "1")))
        except ValueError:
            abort(400)
        try:
            service.set_playlist_block_size(playlist_id, size)
        except Exception as e:
            log.exception("block_size change failed")
            flash(f"Failed to set block size: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Block size set to {size}.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/reshuffle", methods=["POST"])
    def reshuffle(playlist_id: int):
        try:
            service.reshuffle_playlist(playlist_id)
        except Exception as e:
            log.exception("reshuffle failed")
            flash(f"Reshuffle failed: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Playlist reshuffled.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    # ------------------------------------------------------------------ #
    # Genre playlist creation (v1.4.0)
    # ------------------------------------------------------------------ #
    @app.route("/new/genre", methods=["GET"])
    def new_genre():
        backends = available_backends()
        if not backends:
            flash("No backends configured. Set credentials in your .env first.", "error")
            return redirect(url_for("index"))
        return render_template(
            "new_genre.html",
            backends=backends,
            default_backend=("both" if len(backends) > 1 else backends[0]),
            matched_shows=None,
            prev_name=request.args.get("name", ""),
            prev_genres=request.args.get("genres", ""),
            prev_sort_mode=request.args.get("sort_mode", "rotation"),
        )

    @app.route("/new/genre", methods=["POST"])
    def new_genre_action():
        backends = available_backends()
        if not backends:
            abort(400)
        name = (request.form.get("name") or "").strip()
        genres_raw = (request.form.get("genres") or "").strip()
        genre_list = [g.strip() for g in genres_raw.split(",") if g.strip()]
        backend_choice = request.form.get("backend") or (
            "both" if len(backends) > 1 else backends[0]
        )
        if backend_choice not in ("plex", "jellyfin", "both"):
            backend_choice = backends[0]
        sort_mode = request.form.get("sort_mode", "rotation")
        if sort_mode not in VALID_SORT_MODES:
            sort_mode = "rotation"
        try:
            block_size = max(1, int(request.form.get("block_size", "1") or "1"))
        except ValueError:
            block_size = 1
        unwatched_only = bool(request.form.get("unwatched_only"))
        auto_sync = bool(request.form.get("auto_sync")) if request.method == "POST" else True

        action = request.form.get("action", "preview")
        target_backends = ["plex", "jellyfin"] if backend_choice == "both" else [backend_choice]

        # On preview: resolve the genres to a candidate list to show the user.
        matched_shows = None
        if genre_list:
            try:
                configs = service._resolve_genre_shows(genre_list, target_backends)
                matched_shows = [
                    {
                        "title": c.title,
                        "rating_key": c.rating_key,
                        "plex": bool(c.plex_rating_key),
                        "jellyfin": bool(c.jellyfin_rating_key),
                    }
                    for c in configs
                ]
            except Exception as e:
                log.exception("genre preview failed")
                flash(f"Couldn't resolve genres: {e}", "error")

        if action == "create":
            if not name:
                flash("Playlist name is required.", "error")
            elif not genre_list:
                flash("Enter at least one genre.", "error")
            else:
                try:
                    pid = service.create_genre_playlist(
                        name, genre_list,
                        sort_mode=sort_mode,
                        unwatched_only=unwatched_only,
                        auto_sync=auto_sync,
                        backend=backend_choice,
                        block_size=block_size,
                    )
                except Exception as e:
                    log.exception("genre create failed")
                    flash(f"Failed to create genre playlist: {e}", "error")
                else:
                    flash(f"Created genre playlist '{name}' "
                          f"({len(matched_shows or [])} shows matched).", "ok")
                    return redirect(url_for("view_playlist", playlist_id=pid))

        return render_template(
            "new_genre.html",
            backends=backends,
            default_backend=backend_choice,
            matched_shows=matched_shows,
            prev_name=name,
            prev_genres=genres_raw,
            prev_sort_mode=sort_mode,
            prev_block_size=block_size,
            prev_unwatched=unwatched_only,
            prev_auto_sync=auto_sync,
        )

    @app.route("/playlist/<int:playlist_id>/exclude", methods=["POST"])
    def exclude_show(playlist_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            service.set_show_excluded(playlist_id, show, True)
        except Exception as e:
            log.exception("exclude failed")
            flash(f"Failed to exclude show: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Show excluded.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/include", methods=["POST"])
    def include_show(playlist_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            service.set_show_excluded(playlist_id, show, False)
        except Exception as e:
            log.exception("re-include failed")
            flash(f"Failed to re-include show: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Show re-included.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    # -- v1.5.0 crossover groups ------------------------------------------ #

    @app.route("/playlist/<int:playlist_id>/crossover/create", methods=["POST"])
    def crossover_create(playlist_id: int):
        label = (request.form.get("label") or "").strip()
        if not label:
            # Auto-name: count existing groups + 1
            existing = db.list_crossover_groups(playlist_id)
            label = f"Group {len(existing) + 1}"
        try:
            group_id = db.create_crossover_group(playlist_id, label)
        except Exception as e:
            log.exception("crossover group create failed")
            flash(f"Failed to create crossover group: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        # Rebuild tails with new group (even though empty, keeps the path consistent)
        try:
            row = db.get_playlist(playlist_id)
            if row and row["sort_mode"] == "air_date":
                configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
                service._rebuild_playlist_tails(row, configs, op_label="crossover create")
        except Exception:
            log.exception("tail rebuild after crossover create failed")
        flash(f"Crossover group '{label}' created.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/crossover/<int:group_id>/add", methods=["POST"])
    def crossover_add_link(playlist_id: int, group_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            season = int(request.form.get("season", "1"))
            episode = int(request.form.get("episode", "1"))
        except ValueError:
            abort(400)
        # Verify ownership and compute sort_index = max existing + 1
        groups = db.list_crossover_groups(playlist_id)
        group_ids = {g["id"] for g in groups}
        if group_id not in group_ids:
            abort(404)
        max_idx = 0
        for g in groups:
            if g["id"] == group_id:
                for li in g["links"]:
                    if li["sort_index"] > max_idx:
                        max_idx = li["sort_index"]
                break
        try:
            db.add_crossover_link(group_id, show, season, episode, max_idx + 1)
        except Exception as e:
            log.exception("crossover link add failed")
            flash(f"Failed to add episode to group: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        # Rebuild tails
        try:
            row = db.get_playlist(playlist_id)
            if row and row["sort_mode"] == "air_date":
                configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
                service._rebuild_playlist_tails(row, configs, op_label="crossover add link")
        except Exception:
            log.exception("tail rebuild after crossover add failed")
        flash("Episode added to crossover group.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/crossover/<int:group_id>/delete", methods=["POST"])
    def crossover_delete_group(playlist_id: int, group_id: int):
        # Verify the group belongs to this playlist before deleting
        if not any(g["id"] == group_id for g in db.list_crossover_groups(playlist_id)):
            abort(404)
        try:
            db.delete_crossover_group(group_id)
        except Exception as e:
            log.exception("crossover group delete failed")
            flash(f"Failed to delete crossover group: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        try:
            row = db.get_playlist(playlist_id)
            if row and row["sort_mode"] == "air_date":
                configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
                service._rebuild_playlist_tails(row, configs, op_label="crossover delete group")
        except Exception:
            log.exception("tail rebuild after crossover delete failed")
        flash("Crossover group deleted.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/crossover/link/<int:link_id>/remove", methods=["POST"])
    def crossover_remove_link(playlist_id: int, link_id: int):
        # Verify the link belongs to a group owned by this playlist before deleting
        groups = db.list_crossover_groups(playlist_id)
        if not any(li["id"] == link_id for g in groups for li in g["links"]):
            abort(404)
        try:
            db.remove_crossover_link(link_id)
        except Exception as e:
            log.exception("crossover link remove failed")
            flash(f"Failed to remove episode from group: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        try:
            row = db.get_playlist(playlist_id)
            if row and row["sort_mode"] == "air_date":
                configs = [service._config_from_row(r) for r in db.list_shows(playlist_id)]
                service._rebuild_playlist_tails(row, configs, op_label="crossover remove link")
        except Exception:
            log.exception("tail rebuild after crossover remove failed")
        flash("Episode removed from crossover group.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/weight", methods=["POST"])
    def change_weight(playlist_id: int):
        show = (request.form.get("show") or "").strip()
        if not show:
            abort(400)
        try:
            weight = max(1, int(request.form.get("weight", "1")))
        except ValueError:
            abort(400)
        try:
            service.set_show_weight(playlist_id, show, weight)
        except Exception as e:
            log.exception("weight change failed")
            flash(f"Failed to update weight: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Weight updated to {weight}.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/auto_sync", methods=["POST"])
    def change_auto_sync(playlist_id: int):
        enabled = bool(request.form.get("enabled"))
        try:
            service.set_playlist_auto_sync(playlist_id, enabled)
        except Exception as e:
            log.exception("auto_sync change failed")
            flash(f"Failed to update setting: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Auto-update {'enabled' if enabled else 'disabled'}.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/unwatched_only", methods=["POST"])
    def change_unwatched_only(playlist_id: int):
        new_value = bool(request.form.get("enabled"))
        try:
            service.set_playlist_unwatched_only(playlist_id, new_value)
        except Exception as e:
            log.exception("unwatched_only change failed")
            flash(f"Failed to change filter: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Unwatched-only filter {'enabled' if new_value else 'disabled'}.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/reorder", methods=["POST"])
    def reorder(playlist_id: int):
        ordered = request.form.getlist("order")
        if not ordered:
            abort(400)
        try:
            service.reorder_shows(playlist_id, ordered)
        except Exception as e:
            log.exception("reorder failed")
            flash(f"Reorder failed: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Order updated.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/delete", methods=["POST"])
    def delete_playlist(playlist_id: int):
        try:
            service.delete_managed_playlist(playlist_id)
        except Exception as e:
            log.exception("delete failed")
            flash(f"Failed to delete playlist: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash("Playlist deleted.", "ok")
        return redirect(url_for("index"))

    @app.route("/playlist/<int:playlist_id>/sync", methods=["POST"])
    def sync_now(playlist_id: int):
        try:
            added, removed = service.sync_playlist(playlist_id, force=True)
        except Exception as e:
            log.exception("manual sync failed")
            flash(f"Sync failed: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        if added == 0 and removed == 0:
            flash("Already up to date.", "ok")
        else:
            flash(f"Synced: +{added} added, -{removed} removed.", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    @app.route("/playlist/<int:playlist_id>/prune", methods=["POST"])
    def prune_now(playlist_id: int):
        try:
            removed = service.prune_playlist(playlist_id)
        except Exception as e:
            log.exception("prune failed")
            flash(f"Prune failed: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Removed {removed} watched item(s).", "ok")
        return redirect(url_for("view_playlist", playlist_id=playlist_id))

    return app


if __name__ == "__main__":
    app = create_app()
    host = os.environ.get("WEB_HOST", "0.0.0.0")
    port = int(os.environ.get("WEB_PORT", "5005"))
    app.run(host=host, port=port, debug=False, use_reloader=False)
