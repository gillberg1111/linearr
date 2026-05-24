"""Flask web UI for managing rotating Plex playlists."""

from __future__ import annotations

import logging
import os

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    redirect,
    render_template,
    request,
    url_for,
)

load_dotenv()

import db  # noqa: E402
import plex_client as plex  # noqa: E402
import scheduler  # noqa: E402
import service  # noqa: E402
from service import ShowConfig  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("app")


def _parse_configs_from_form(form, show_keys: list[str]) -> list[ShowConfig]:
    """Pull per-show fields out of the configure form.

    Field names: start_<rk>, end_<rk>, specials_<rk>, include_movies_<rk>,
    movies_<rk> (multi-valued)
    """
    configs: list[ShowConfig] = []
    for rk in show_keys:
        start = form.get(f"start_{rk}", "1")
        end = form.get(f"end_{rk}", "")
        specials = form.get(f"specials_{rk}", "")
        inc_movies = form.get(f"include_movies_{rk}", "")
        selected_movies = form.getlist(f"movies_{rk}")
        try:
            start_i = max(1, int(start))
        except ValueError:
            start_i = 1
        try:
            end_i = int(end) if end.strip() else None
        except ValueError:
            end_i = None
        # Safety net: end_season below start_season makes no sense. The UI
        # already hides invalid choices, but stale form data could still ship.
        if end_i is not None and end_i < start_i:
            end_i = None
        configs.append(
            ShowConfig(
                rating_key=rk,
                start_season=start_i,
                end_season=end_i,
                include_specials=bool(specials),
                include_movies=bool(inc_movies),
                movie_rating_keys=[k for k in selected_movies if k],
            )
        )
    return configs


def _gather_season_meta(show_keys: list[str]) -> dict:
    """Returns {rating_key: {summary, seasons, movies}}.

    `movies` is a list of associated movies found in your movie libraries by
    word-boundary title match on the show's name.
    """
    out: dict = {}
    for rk in show_keys:
        try:
            summary = plex.get_show_summary(rk)
            out[rk] = {
                "summary": summary,
                "seasons": plex.season_summaries(rk),
                "movies": plex.find_associated_movies(summary.title),
            }
        except Exception:
            log.exception("metadata fetch failed for %s", rk)
    return out


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = os.environ.get("FLASK_SECRET", "dev-secret-change-me")

    db.init_db()
    scheduler.start()

    # ------------------------------------------------------------------ #
    # Thumb proxy
    # ------------------------------------------------------------------ #
    @app.route("/thumb")
    def thumb():
        path = request.args.get("path") or ""
        if not path.startswith("/"):
            abort(400)
        try:
            w = int(request.args.get("w", 240))
            h = int(request.args.get("h", 360))
        except ValueError:
            w, h = 240, 360
        try:
            data, ctype = plex.fetch_image(path, width=w, height=h)
        except Exception:
            log.exception("thumb fetch failed: %s", path)
            abort(502)
        resp = Response(data, mimetype=ctype)
        resp.headers["Cache-Control"] = "public, max-age=86400"
        return resp

    # ------------------------------------------------------------------ #
    # AJAX preview (returns rendered HTML partial)
    # ------------------------------------------------------------------ #
    @app.route("/api/preview", methods=["POST"])
    @app.route("/api/preview/<int:playlist_id>", methods=["POST"])
    def api_preview(playlist_id: int | None = None):
        show_keys = request.form.getlist("shows")
        configs = _parse_configs_from_form(request.form, show_keys)

        if playlist_id is not None:
            view = service.get_playlist_view(playlist_id)
            if not view:
                return ("", 404)
            sort_mode = view.sort_mode
            unwatched_only = view.unwatched_only
            existing_rows = db.list_shows(playlist_id)
            existing_configs = [service._config_from_row(r) for r in existing_rows]
            all_configs = existing_configs + configs
        else:
            sort_mode = request.form.get("sort_mode", "rotation")
            if sort_mode not in ("rotation", "air_date"):
                sort_mode = "rotation"
            unwatched_only = bool(request.form.get("unwatched_only"))
            all_configs = configs

        try:
            preview = service.preview_playlist(
                all_configs,
                limit=2000,
                sort_mode=sort_mode,
                unwatched_only=unwatched_only,
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
        try:
            shows = plex.list_all_shows()
        except Exception as e:
            log.exception("listing shows failed")
            flash(f"Couldn't reach Plex: {e}", "error")
            shows = []
        prev_name = request.args.get("name", "")
        selected = {k for k in request.args.get("selected", "").split(",") if k}
        return render_template(
            "new.html", shows=shows, prev_name=prev_name, selected=selected
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
        if sort_mode not in ("rotation", "air_date"):
            sort_mode = "rotation"
        unwatched_only = bool(request.form.get("unwatched_only"))
        configs = _parse_configs_from_form(request.form, show_keys)
        meta = _gather_season_meta(show_keys)

        if action == "commit":
            try:
                pid = service.create_managed_playlist(
                    name, configs, sort_mode=sort_mode, unwatched_only=unwatched_only
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
                    unwatched_only=unwatched_only,
                    preview_api_url=url_for("api_preview"),
                )
            flash(f"Created '{name}'.", "ok")
            return redirect(url_for("view_playlist", playlist_id=pid))

        # preview / initial render
        try:
            preview = service.preview_playlist(
                configs, limit=2000, sort_mode=sort_mode, unwatched_only=unwatched_only
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
            unwatched_only=unwatched_only,
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
        try:
            all_shows = plex.list_all_shows()
        except Exception:
            all_shows = []
        in_playlist = {s["show_rating_key"] for s in view.shows}
        available = [s for s in all_shows if s.rating_key not in in_playlist]
        selected = {k for k in request.args.get("selected", "").split(",") if k}
        return render_template(
            "playlist.html",
            playlist=view,
            available=available,
            selected=selected,
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
        configs = _parse_configs_from_form(request.form, show_keys)
        meta = _gather_season_meta(show_keys)

        if action == "commit":
            try:
                service.add_shows_to_playlist(playlist_id, configs)
            except Exception as e:
                log.exception("add failed")
                flash(f"Failed to add shows: {e}", "error")
                return redirect(url_for("view_playlist", playlist_id=playlist_id))
            flash(f"Added {len(configs)} show(s).", "ok")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))

        # preview / initial render — preview includes existing shows too so
        # the user sees how the rotation actually starts.
        existing_rows = db.list_shows(playlist_id)
        existing_configs = [service._config_from_row(r) for r in existing_rows]
        try:
            preview = service.preview_playlist(
                existing_configs + configs,
                limit=2000,
                sort_mode=view.sort_mode,
                unwatched_only=view.unwatched_only,
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
            unwatched_only=view.unwatched_only,
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

    @app.route("/playlist/<int:playlist_id>/sort_mode", methods=["POST"])
    def change_sort_mode(playlist_id: int):
        mode = (request.form.get("sort_mode") or "").strip()
        if mode not in ("rotation", "air_date"):
            abort(400)
        try:
            service.set_playlist_sort_mode(playlist_id, mode)
        except Exception as e:
            log.exception("sort_mode change failed")
            flash(f"Failed to change sort: {e}", "error")
            return redirect(url_for("view_playlist", playlist_id=playlist_id))
        flash(f"Sort mode set to {'Air Date' if mode == 'air_date' else 'Rotation'}.", "ok")
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
