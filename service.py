"""High-level operations: create / edit / prune managed rotating playlists.

Bridges db (managed state), the `MediaClient` interface (server I/O), and
`rotation` (pure logic). Every server call goes through MediaClient; this
module never imports a backend-specific module directly.

Dual-backend (v1.1.0):
  * A `managed_playlists.backend` of 'plex', 'jellyfin', or 'both' decides
    which backends each operation iterates over.
  * `_clients_for_playlist(row)` is the single dispatch point.
  * Each show row carries both `plex_show_item_id` and `jellyfin_show_item_id`
    (each nullable). Per-backend operations filter to shows that have an ID
    on that backend; missing-side shows are silently skipped on that side.
  * For 'both' playlists, sync runs heal-on-sync: shows missing an ID on one
    side get a fresh title+year match attempt against that backend's library.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import db
import rotation
from media_client import (
    EpisodeRef,
    MediaClient,
    MovieSummary,
    get_client,
    titles_match,
)
from rotation import PlaylistItem

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Config struct
# --------------------------------------------------------------------------- #


@dataclass
class ShowConfig:
    """The per-show configuration carried through preview/create/sync.

    `rating_key` is the row's primary identifier. For legacy Plex-only rows
    it equals plex_rating_key. For Jellyfin-originated rows (without a Plex
    match) it equals jellyfin_rating_key. The dedicated *_rating_key fields
    below are what we actually use for API calls per backend.
    """

    rating_key: str
    title: str = ""
    thumb: str | None = None
    start_season: int = 1
    end_season: int | None = None
    include_specials: bool = False
    include_movies: bool = False
    movie_rating_keys: list[str] = field(default_factory=list)  # Plex movie ratingKeys
    # v1.3.0 — per-show weight for rotation_weighted (clamped to >=1).
    weight: int = 1
    # v1.4.0 — soft-delete flag for genre playlists. Excluded shows are
    # kept in the DB but skipped by tail-rebuild and the auto-add path
    # of sync. UI offers a re-include button.
    is_excluded: bool = False
    # v1.1.0 dual-backend identifiers
    plex_rating_key: str | None = None
    jellyfin_rating_key: str | None = None
    jellyfin_movie_rating_keys: list[str] = field(default_factory=list)
    # v1.2.0 per-episode exclusions: set of (season, episode) pairs.
    # Backend-agnostic — works on both Plex and Jellyfin since episodes use
    # the same (season, episode) shape on both sides.
    excluded_episodes: set[tuple[int, int]] = field(default_factory=set)

    def __post_init__(self) -> None:
        # Legacy callers (Phase 1 / single-backend Plex) pass only rating_key
        # for a Plex show; mirror it into plex_rating_key so backend dispatch
        # below works uniformly.
        if self.plex_rating_key is None and self.rating_key and self.rating_key.isdigit():
            self.plex_rating_key = self.rating_key

    def id_for(self, backend: str) -> str | None:
        return self.plex_rating_key if backend == "plex" else self.jellyfin_rating_key

    def movie_ids_for(self, backend: str) -> list[str]:
        return self.movie_rating_keys if backend == "plex" else self.jellyfin_movie_rating_keys

    @property
    def excluded_csv(self) -> str:
        """Serialize excluded_episodes as 'S:E,S:E,...' for the configure form."""
        return ",".join(f"{s}:{e}" for s, e in sorted(self.excluded_episodes))


# --------------------------------------------------------------------------- #
# Dispatch helpers
# --------------------------------------------------------------------------- #


def _watched_keep() -> int:
    try:
        return max(0, int(os.environ.get("WATCHED_KEEP", "2")))
    except ValueError:
        return 2


def _backends_for(row) -> list[str]:
    """Backend names this playlist row targets ('plex', 'jellyfin', or both)."""
    backend = row["backend"] if "backend" in row.keys() else "plex"
    if backend == "both":
        return ["plex", "jellyfin"]
    return [backend]


def _client_for(backend: str) -> MediaClient:
    return get_client(backend)


def _playlist_id_on(row, backend: str) -> str | None:
    """The backend-specific playlist id stored on this row (None if not created)."""
    if backend == "plex":
        return row["plex_rating_key"] if "plex_rating_key" in row.keys() else None
    return row["jellyfin_playlist_id"] if "jellyfin_playlist_id" in row.keys() else None


def _clients_for_playlist(row) -> list[tuple[str, MediaClient, str | None]]:
    """Return [(backend, client, backend_playlist_id), ...] for each backend
    this playlist targets. backend_playlist_id may be None if the playlist
    hasn't been created on that backend yet (e.g. mid-create failure)."""
    out: list[tuple[str, MediaClient, str | None]] = []
    for backend in _backends_for(row):
        out.append((backend, _client_for(backend), _playlist_id_on(row, backend)))
    return out


# --------------------------------------------------------------------------- #
# Per-backend config hydration + episode fetch
# --------------------------------------------------------------------------- #


def _hydrate_configs(configs: list[ShowConfig], backend: str) -> None:
    """Fill in missing title/thumb from the backend, using whichever id is
    populated for that backend. Configs already populated stay untouched."""
    client = _client_for(backend)
    for cfg in configs:
        target_id = cfg.id_for(backend)
        if not target_id:
            continue
        if cfg.title and cfg.thumb is not None:
            continue
        try:
            summary = client.get_show_summary(target_id)
        except Exception:
            log.debug("hydrate skipped for %s on %s (lookup failed)", target_id, backend)
            continue
        cfg.title = cfg.title or summary.title
        if cfg.thumb is None:
            cfg.thumb = summary.thumb


def _episodes_for_config(
    cfg: ShowConfig, backend: str, unwatched_only: bool = False
) -> list[EpisodeRef]:
    """Episodes for one show on one backend. Returns [] if the show has no
    id on this backend (caller should treat that as "skip on this side")."""
    target_id = cfg.id_for(backend)
    if not target_id:
        return []
    client = _client_for(backend)
    eps = client.episodes_for_show(
        target_id,
        start_season=cfg.start_season,
        end_season=cfg.end_season,
        include_specials=cfg.include_specials,
    )
    if unwatched_only:
        eps = [e for e in eps if e.view_count == 0]

    # v1.2.0: drop any explicitly-excluded (season, episode) pairs. Works
    # uniformly across backends since EpisodeRef uses the same shape on both.
    if cfg.excluded_episodes:
        eps = [e for e in eps if (e.season, e.episode) not in cfg.excluded_episodes]

    if cfg.include_movies:
        movie_refs: list[EpisodeRef] = []
        for mrk in cfg.movie_ids_for(backend):
            ms = client.get_movie_summary(mrk)
            if ms is None:
                continue
            if unwatched_only and ms.view_count > 0:
                continue
            movie_refs.append(client.movie_as_episode_ref(ms, target_id, cfg.title or ""))
        movie_refs.sort(key=lambda m: (m.air_date or "9999-99-99", m.title.lower()))
        eps = eps + movie_refs

    # rebuild_tail keys items by (season, episode) per show. Both backends use
    # the same show_rating_key field on EpisodeRef, but that's the backend's
    # own show id. For dual-backend rotation each side computes independently
    # so this is fine — the kept set is read from THIS side's playlist.
    return eps


# --------------------------------------------------------------------------- #
# DB row → ShowConfig
# --------------------------------------------------------------------------- #


def _parse_excluded_episodes(raw: str | None) -> set[tuple[int, int]]:
    """Parse a 'S:E,S:E,...' string into a set of (season, episode) tuples."""
    out: set[tuple[int, int]] = set()
    if not raw:
        return out
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            s_str, e_str = token.split(":", 1)
            out.add((int(s_str), int(e_str)))
        except (ValueError, AttributeError):
            log.warning("Skipping malformed excluded_episode entry: %r", token)
    return out


def _config_from_row(row) -> ShowConfig:
    raw_plex_keys = row["movie_rating_keys"] if "movie_rating_keys" in row.keys() else ""
    plex_movies = [k for k in (raw_plex_keys or "").split(",") if k]
    raw_jf_keys = row["jellyfin_movie_item_ids"] if "jellyfin_movie_item_ids" in row.keys() else ""
    jf_movies = [k for k in (raw_jf_keys or "").split(",") if k]
    plex_id = row["plex_show_item_id"] if "plex_show_item_id" in row.keys() else None
    jf_id = row["jellyfin_show_item_id"] if "jellyfin_show_item_id" in row.keys() else None
    # Legacy rows pre-migration don't have plex_show_item_id; fall back to the PK.
    if plex_id is None and str(row["show_rating_key"]).isdigit():
        plex_id = str(row["show_rating_key"])
    excluded_raw = row["excluded_episode_keys"] if "excluded_episode_keys" in row.keys() else ""
    weight_val = max(1, int(_row_get(row, "weight", 1) or 1))
    is_excl = bool(_row_get(row, "is_excluded", 0) or 0)
    return ShowConfig(
        rating_key=row["show_rating_key"],
        title=row["show_title"],
        thumb=row["show_thumb"],
        start_season=int(row["start_season"] or 1),
        end_season=row["end_season"],
        include_specials=bool(row["include_specials"]),
        include_movies=bool(row["include_movies"]) if "include_movies" in row.keys() else False,
        movie_rating_keys=plex_movies,
        plex_rating_key=plex_id,
        jellyfin_rating_key=jf_id,
        jellyfin_movie_rating_keys=jf_movies,
        excluded_episodes=_parse_excluded_episodes(excluded_raw),
        weight=weight_val,
        is_excluded=is_excl,
    )


# --------------------------------------------------------------------------- #
# Cross-backend show matching (used at add-time + heal-on-sync)
# --------------------------------------------------------------------------- #


def _candidates_for(backend: str) -> list:
    """List of every show on a backend. Network call — caller caches."""
    try:
        return _client_for(backend).list_all_shows()
    except Exception:
        log.exception("show matching: couldn't list shows on %s", backend)
        return []


def _find_match(candidates: list, title: str, year: int | None) -> str | None:
    for s in candidates:
        if titles_match(s.title, title, s.year, year):
            return s.rating_key
    return None


def _enrich_configs_with_matches(configs: list[ShowConfig], target_backends: list[str]) -> None:
    """For each config missing an id on a target backend, attempt to find it
    by title+year. Populates the appropriate *_rating_key field in-place.

    Caches the candidate list per backend so we make at most ONE
    list_all_shows call per backend, regardless of how many configs need
    matching.
    """
    cache: dict[str, list] = {}
    def cands(b: str) -> list:
        if b not in cache:
            cache[b] = _candidates_for(b)
        return cache[b]

    for cfg in configs:
        year = _year_hint(cfg)
        if "plex" in target_backends and cfg.plex_rating_key is None:
            mid = _find_match(cands("plex"), cfg.title or "", year)
            if mid:
                cfg.plex_rating_key = mid
        if "jellyfin" in target_backends and cfg.jellyfin_rating_key is None:
            mid = _find_match(cands("jellyfin"), cfg.title or "", year)
            if mid:
                cfg.jellyfin_rating_key = mid


def _year_hint(cfg: ShowConfig) -> int | None:
    """Best-effort year for the ShowConfig — used to disambiguate matches.
    Tries whichever backend already has an id for this show; cheap, tolerant."""
    for backend in ("plex", "jellyfin"):
        target_id = cfg.id_for(backend)
        if not target_id:
            continue
        try:
            summary = _client_for(backend).get_show_summary(target_id)
            if summary.year:
                return summary.year
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# The single tail-rebuild primitive (replaces 6 near-identical blocks)
# --------------------------------------------------------------------------- #


def _rebuild_tail_on(
    backend: str,
    client: MediaClient,
    playlist_id_on_backend: str,
    configs: list[ShowConfig],
    sort_mode: str,
    unwatched_only: bool,
    *,
    block_size: int = 1,
    shuffle_seed: int | None = None,
) -> tuple[int, int]:
    """Recompute the future portion of a playlist on a single backend.

    Returns (added_count, removed_count). Configs without an id on this
    backend are silently filtered out (they don't contribute on this side).

    `block_size` and `shuffle_seed` are only consulted when `sort_mode` is
    'rotation_blocks' or 'shuffle_chronological' respectively. Per-show
    weights for 'rotation_weighted' come from each ShowConfig.weight.
    """
    relevant_configs = [c for c in configs if c.id_for(backend)]
    if not relevant_configs:
        return (0, 0)

    items = client.get_playlist_items(playlist_id_on_backend)
    splice = rotation.splice_index(items)
    kept = items[:splice]

    shows_episodes = [
        _episodes_for_config(c, backend, unwatched_only=unwatched_only)
        for c in relevant_configs
    ]
    show_order = [c.id_for(backend) for c in relevant_configs]
    weights = [c.weight for c in relevant_configs]
    new_tail = rotation.rebuild_tail(
        kept, shows_episodes, mode=sort_mode, show_order=show_order,
        weights=weights, block_size=block_size, shuffle_seed=shuffle_seed,
    )

    current_tail = items[splice:]
    current_tail_keys = [it.rating_key for it in current_tail]
    new_tail_keys = [e.rating_key for e in new_tail]

    if current_tail_keys == new_tail_keys:
        return (0, 0)

    to_remove = list(set(current_tail_keys) - set(new_tail_keys))
    kept_keys = {it.rating_key for it in kept}
    to_add = [k for k in new_tail_keys if k not in kept_keys]

    if to_remove:
        client.remove_items_from_playlist(playlist_id_on_backend, to_remove)
    if to_add:
        client.add_items_to_playlist(playlist_id_on_backend, to_add)

    return (len(to_add), len(to_remove))


def _row_get(row, key, default=None):
    """Safe accessor for sqlite3.Row that returns default if column absent."""
    return row[key] if key in row.keys() else default


def _rebuild_playlist_tails(
    row,
    full_configs: list[ShowConfig],
    *,
    sort_mode: str | None = None,
    unwatched_only: bool | None = None,
    block_size: int | None = None,
    shuffle_seed: int | None = ...,  # sentinel — None is a valid value
    op_label: str = "tail rebuild",
) -> tuple[int, int]:
    """Iterate every enabled backend for a playlist and rebuild its tail.

    Defaults all params from the row; callers that have JUST written a new
    value to the DB (but haven't refetched the row) pass the new value via
    the corresponding kwarg.
    Returns (total_added, total_removed) across backends. Individual backend
    failures are logged but don't block the other backend.

    Soft-deleted shows (is_excluded=True) are filtered out — they remain in
    the DB but don't contribute to playlist contents.
    """
    sm = sort_mode if sort_mode is not None else row["sort_mode"]
    uw = unwatched_only if unwatched_only is not None else bool(row["unwatched_only"])
    bs = block_size if block_size is not None else int(_row_get(row, "block_size", 1) or 1)
    ss = _row_get(row, "shuffle_seed", None) if shuffle_seed is ... else shuffle_seed
    active_configs = [c for c in full_configs if not c.is_excluded]
    total_added = total_removed = 0
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            added, removed = _rebuild_tail_on(
                tb, client, pl_id, active_configs, sm, uw,
                block_size=bs, shuffle_seed=ss,
            )
            total_added += added
            total_removed += removed
        except Exception:
            log.exception("%s failed on %s for '%s'", op_label, tb, row["name"])
    return (total_added, total_removed)


# --------------------------------------------------------------------------- #
# Preview (no backend writes)
# --------------------------------------------------------------------------- #


def preview_playlist(
    configs: list[ShowConfig],
    limit: int = 2000,
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
    backend: str = "plex",
    *,
    block_size: int = 1,
    shuffle_seed: int | None = None,
) -> list[dict]:
    """Compute the first `limit` episodes of the resulting playlist on a
    single backend without touching its write APIs. For 'both' playlists,
    the UI typically previews on whichever backend the user picked shows
    from. Default 'plex' for back-compat with existing UI."""
    _hydrate_configs(configs, backend)
    relevant = [c for c in configs if c.id_for(backend) or not configs]  # keep all for first-render
    if not relevant:
        relevant = configs
    shows_eps = [_episodes_for_config(c, backend, unwatched_only=unwatched_only) for c in relevant]
    show_order = [c.id_for(backend) for c in relevant if c.id_for(backend)]
    weights = [c.weight for c in relevant]
    composed = rotation.compose(
        shows_eps, mode=sort_mode, show_order=show_order,
        weights=weights, block_size=block_size, shuffle_seed=shuffle_seed,
    )
    out: list[dict] = []
    for ep in composed[:limit]:
        out.append({
            "show": ep.show_title,
            "season": ep.season,
            "episode": ep.episode,
            "title": ep.title,
            "air_date": ep.air_date,
            "is_special": ep.season == 0,
        })
    return out


# --------------------------------------------------------------------------- #
# View models
# --------------------------------------------------------------------------- #


@dataclass
class PlaylistView:
    id: int
    name: str
    plex_rating_key: str | None
    jellyfin_playlist_id: str | None
    backend: str  # 'plex' | 'jellyfin' | 'both'
    shows: list[dict]  # active (non-excluded) show rows
    item_count: int  # max across enabled backends
    sort_mode: str = "rotation"
    unwatched_only: bool = False
    auto_sync: bool = True
    # v1.3.0 — advanced sequencing
    block_size: int = 1
    shuffle_seed: int | None = None
    # v1.4.0 — dynamic genre playlists
    playlist_type: str = "manual"  # 'manual' | 'genre'
    genre_filter: str | None = None
    excluded_shows: list[dict] = field(default_factory=list)  # soft-deleted rows


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def create_managed_playlist(
    name: str,
    configs: list[ShowConfig],
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
    auto_sync: bool = True,
    backend: str = "plex",
    *,
    block_size: int = 1,
    shuffle_seed: int | None = None,
) -> int:
    if not configs:
        raise ValueError("Need at least one show to create a playlist")
    if sort_mode not in rotation.VALID_SORT_MODES:
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")
    if backend not in ("plex", "jellyfin", "both"):
        raise ValueError(f"Invalid backend: {backend!r}")

    # Auto-generate a seed if creating in shuffle mode without one — gives
    # this playlist a stable shuffle that persists across syncs.
    if sort_mode == "shuffle_chronological" and shuffle_seed is None:
        import random as _random
        shuffle_seed = _random.randint(1, 2**31 - 1)

    block_size = max(1, int(block_size))

    target_backends = ["plex", "jellyfin"] if backend == "both" else [backend]

    # Hydrate against the source backend(s) so titles/thumbs are filled in.
    # For 'both' mode also attempt cross-backend matching.
    for tb in target_backends:
        _hydrate_configs(configs, tb)
    if backend == "both":
        _enrich_configs_with_matches(configs, target_backends)

    # Per-backend ordered key list (skip configs without an id on that backend).
    created_ids: dict[str, str] = {}  # backend -> new playlist id
    try:
        for tb in target_backends:
            relevant = [c for c in configs if c.id_for(tb)]
            if not relevant:
                # No shows match this side — for 'both' mode that's an edge
                # case (none of the picked shows existed here at all). Skip
                # creation on this side; the row will have NULL for it.
                continue
            shows_eps = [_episodes_for_config(c, tb, unwatched_only=unwatched_only) for c in relevant]
            show_order = [c.id_for(tb) for c in relevant]
            weights = [c.weight for c in relevant]
            composed = rotation.compose(
                shows_eps, mode=sort_mode, show_order=show_order,
                weights=weights, block_size=block_size, shuffle_seed=shuffle_seed,
            )
            if not composed:
                continue
            new_id = _client_for(tb).create_playlist(name, [e.rating_key for e in composed])
            created_ids[tb] = new_id

        if not created_ids:
            raise ValueError("Could not create the playlist on any enabled backend "
                             "(no selected show has episodes on the targeted backend(s))")
    except Exception:
        # Roll back any side that succeeded so we don't leak orphan playlists.
        for tb, pid in created_ids.items():
            try:
                _client_for(tb).delete_playlist(pid)
                log.warning("Rolled back %s playlist after create failure", tb)
            except Exception:
                log.exception("Rollback failed for %s playlist %s", tb, pid)
        raise

    playlist_id = db.create_playlist(
        name,
        sort_mode=sort_mode,
        unwatched_only=unwatched_only,
        auto_sync=auto_sync,
        backend=backend,
    )
    if "plex" in created_ids:
        db.set_plex_rating_key(playlist_id, created_ids["plex"])
    if "jellyfin" in created_ids:
        db.set_jellyfin_playlist_id(playlist_id, created_ids["jellyfin"])
    if block_size != 1:
        db.set_block_size(playlist_id, block_size)
    if shuffle_seed is not None:
        db.set_shuffle_seed(playlist_id, shuffle_seed)

    db.add_shows(playlist_id, [_config_to_db_dict(c) for c in configs])
    log.info(
        "Created playlist '%s' (backend=%s, sides created: %s)",
        name, backend, ",".join(sorted(created_ids.keys())) or "none",
    )
    return playlist_id


def _config_to_db_dict(c: ShowConfig) -> dict:
    return {
        "rating_key": c.rating_key,
        "plex_show_item_id": c.plex_rating_key,
        "jellyfin_show_item_id": c.jellyfin_rating_key,
        "title": c.title,
        "thumb": c.thumb,
        "start_season": c.start_season,
        "end_season": c.end_season,
        "include_specials": c.include_specials,
        "include_movies": c.include_movies,
        "movie_rating_keys": c.movie_rating_keys,
        "jellyfin_movie_item_ids": c.jellyfin_movie_rating_keys,
        "excluded_episodes": c.excluded_episodes,
        "weight": c.weight,
    }


# --------------------------------------------------------------------------- #
# Add shows mid-rotation
# --------------------------------------------------------------------------- #


def add_shows_to_playlist(playlist_id: int, new_configs: list[ShowConfig]) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")

    existing_rows = db.list_shows(playlist_id)
    existing_keys = {s["show_rating_key"] for s in existing_rows}
    new_configs = [c for c in new_configs if c.rating_key not in existing_keys]
    if not new_configs:
        return

    target_backends = _backends_for(row)
    for tb in target_backends:
        _hydrate_configs(new_configs, tb)
    if "both" == row["backend"]:
        _enrich_configs_with_matches(new_configs, target_backends)

    db.add_shows(playlist_id, [_config_to_db_dict(c) for c in new_configs])

    full_configs: list[ShowConfig] = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    added, removed = _rebuild_playlist_tails(row, full_configs, op_label="add_shows tail rebuild")
    log.info("Added shows to '%s': +%d -%d tail items", row["name"], added, removed)


# --------------------------------------------------------------------------- #
# Remove a show entirely
# --------------------------------------------------------------------------- #


def remove_show_from_playlist(playlist_id: int, show_rating_key: str) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")

    target_row = db.get_show(playlist_id, show_rating_key)
    if not target_row:
        return
    target_cfg = _config_from_row(target_row)

    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        show_id_on_backend = target_cfg.id_for(tb)
        if not show_id_on_backend:
            continue  # show wasn't on this backend in the first place
        try:
            items = client.get_playlist_items(pl_id)
            to_remove_keys = [
                it.rating_key for it in items
                if it.show_rating_key == show_id_on_backend
            ]
            client.remove_items_from_playlist(pl_id, to_remove_keys)
            log.info("Removed %d %s items for show %s from '%s'",
                     len(to_remove_keys), tb, show_id_on_backend, row["name"])
        except Exception:
            log.exception("remove_show failed on %s for '%s'", tb, row["name"])

    db.remove_show(playlist_id, show_rating_key)


# --------------------------------------------------------------------------- #
# Reorder rotation
# --------------------------------------------------------------------------- #


def reorder_shows(playlist_id: int, ordered_keys: list[str]) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")

    existing = {s["show_rating_key"]: s for s in db.list_shows(playlist_id)}
    if set(ordered_keys) != set(existing.keys()):
        raise ValueError("Reorder keys don't match the current show set")

    db.set_positions(playlist_id, ordered_keys)

    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(row, full_configs, op_label="reorder")
    log.info("Reordered '%s'", row["name"])


# --------------------------------------------------------------------------- #
# Change sort mode
# --------------------------------------------------------------------------- #


def set_playlist_sort_mode(playlist_id: int, sort_mode: str) -> None:
    if sort_mode not in rotation.VALID_SORT_MODES:
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    if row["sort_mode"] == sort_mode:
        return

    db.set_sort_mode(playlist_id, sort_mode)

    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(
        row, full_configs, sort_mode=sort_mode, op_label="sort_mode change"
    )
    log.info("Switched '%s' to sort_mode=%s", row["name"], sort_mode)


# --------------------------------------------------------------------------- #
# Toggle unwatched-only filter
# --------------------------------------------------------------------------- #


def set_playlist_unwatched_only(playlist_id: int, unwatched_only: bool) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    if bool(row["unwatched_only"]) == unwatched_only:
        return

    db.set_unwatched_only(playlist_id, unwatched_only)

    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(
        row, full_configs, unwatched_only=unwatched_only, op_label="unwatched-toggle"
    )
    log.info("Switched '%s' unwatched_only=%s", row["name"], unwatched_only)


# --------------------------------------------------------------------------- #
# Per-playlist auto-sync toggle
# --------------------------------------------------------------------------- #


def set_playlist_auto_sync(playlist_id: int, enabled: bool) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    db.set_auto_sync(playlist_id, enabled)
    log.info("Playlist '%s' auto_sync=%s", row["name"], enabled)


# --------------------------------------------------------------------------- #
# v1.3.0 — block size, shuffle seed, per-show weight
# --------------------------------------------------------------------------- #


def set_playlist_block_size(playlist_id: int, block_size: int) -> None:
    """Update block_size and rebuild tails on every enabled backend."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    new_size = max(1, int(block_size))
    db.set_block_size(playlist_id, new_size)
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(
        row, full_configs, block_size=new_size, op_label="block_size change"
    )
    log.info("Playlist '%s' block_size=%d", row["name"], new_size)


def reshuffle_playlist(playlist_id: int) -> None:
    """Regenerate the shuffle seed and rebuild tails. Only meaningful for
    playlists in shuffle_chronological mode, but callable in any mode."""
    import random as _random
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    new_seed = _random.randint(1, 2**31 - 1)
    db.set_shuffle_seed(playlist_id, new_seed)
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(
        row, full_configs, shuffle_seed=new_seed, op_label="reshuffle"
    )
    log.info("Playlist '%s' reshuffled (seed=%d)", row["name"], new_seed)


def set_show_weight(playlist_id: int, show_rating_key: str, weight: int) -> None:
    """Update a single show's weight and rebuild tails."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    db.set_show_weight(playlist_id, show_rating_key, max(1, int(weight)))
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(row, full_configs, op_label="weight change")
    log.info("Playlist '%s' show %s weight=%d", row["name"], show_rating_key, weight)


# --------------------------------------------------------------------------- #
# v1.4.0 — Dynamic genre playlists
# --------------------------------------------------------------------------- #


def _parse_genre_csv(s: str | None) -> list[str]:
    return [g.strip() for g in (s or "").split(",") if g and g.strip()]


def _resolve_genre_shows(
    genres: list[str],
    target_backends: list[str],
) -> list[ShowConfig]:
    """Query each configured backend for shows matching the given genres.
    Returns a list of ShowConfigs deduplicated across backends via
    title+year matching. For 'both' setups, configs carry both IDs when
    a match is found on each side.
    """
    if not genres:
        return []
    # Per-backend matches.
    per_backend: dict[str, list] = {}
    for tb in target_backends:
        try:
            per_backend[tb] = _client_for(tb).list_shows_by_genres(genres)
        except Exception:
            log.exception("genre resolve failed on %s", tb)
            per_backend[tb] = []

    # Aggregate, deduplicate via title+year.
    out: list[ShowConfig] = []
    seen_keys: dict[tuple[str, int], int] = {}
    for tb in target_backends:
        for s in per_backend[tb]:
            key = (s.title.lower().strip(), s.year or 0)
            if key in seen_keys:
                # Already added by a previous backend — annotate.
                idx = seen_keys[key]
                if tb == "plex" and out[idx].plex_rating_key is None:
                    out[idx].plex_rating_key = s.rating_key
                if tb == "jellyfin" and out[idx].jellyfin_rating_key is None:
                    out[idx].jellyfin_rating_key = s.rating_key
                # Prefer the first thumb that was set.
                if not out[idx].thumb and s.thumb:
                    out[idx].thumb = s.thumb
                continue
            cfg = ShowConfig(
                rating_key=s.rating_key,
                title=s.title,
                thumb=s.thumb,
                plex_rating_key=s.rating_key if tb == "plex" else None,
                jellyfin_rating_key=s.rating_key if tb == "jellyfin" else None,
            )
            seen_keys[key] = len(out)
            out.append(cfg)
    # Cross-backend completion: for 'both' setups, fill in missing IDs.
    if "plex" in target_backends and "jellyfin" in target_backends:
        _enrich_configs_with_matches(out, target_backends)
    out.sort(key=lambda c: (c.title or "").lower())
    return out


def create_genre_playlist(
    name: str,
    genres: list[str],
    *,
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
    auto_sync: bool = True,
    backend: str = "plex",
    block_size: int = 1,
    shuffle_seed: int | None = None,
) -> int:
    """Create a playlist whose member shows are determined by a genre query
    rather than hand-picked. Future syncs re-query the backend and auto-add
    new shows matching the genre."""
    cleaned_genres = [g.strip() for g in genres if g and g.strip()]
    if not cleaned_genres:
        raise ValueError("Need at least one genre to create a genre playlist")
    if backend not in ("plex", "jellyfin", "both"):
        raise ValueError(f"Invalid backend: {backend!r}")

    target_backends = ["plex", "jellyfin"] if backend == "both" else [backend]
    configs = _resolve_genre_shows(cleaned_genres, target_backends)
    if not configs:
        raise ValueError(
            f"No shows on the configured backend(s) match genres: {', '.join(cleaned_genres)}"
        )

    # Delegate to the manual creator. It already handles per-backend playlist
    # creation, rollback on partial failure, DB insert.
    playlist_id = create_managed_playlist(
        name, configs,
        sort_mode=sort_mode,
        unwatched_only=unwatched_only,
        auto_sync=auto_sync,
        backend=backend,
        block_size=block_size,
        shuffle_seed=shuffle_seed,
    )
    # Mark the playlist as genre type + persist the filter.
    with db.connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET playlist_type = 'genre', genre_filter = ? WHERE id = ?",
            (",".join(cleaned_genres), playlist_id),
        )
    log.info(
        "Created genre playlist '%s' (backend=%s, genres=%s, %d shows)",
        name, backend, cleaned_genres, len(configs),
    )
    return playlist_id


def set_show_excluded(playlist_id: int, show_rating_key: str, excluded: bool) -> None:
    """Soft-delete or re-include a single show. Rebuilds tails on every
    enabled backend after the change.

    Excluded shows stay in the DB so the next genre sync doesn't re-add
    them — the user explicitly removed them once."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    db.set_show_excluded(playlist_id, show_rating_key, excluded)
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(row, full_configs, op_label="exclude/re-include")
    log.info(
        "Playlist '%s' show %s excluded=%s",
        row["name"], show_rating_key, excluded,
    )


# --------------------------------------------------------------------------- #
# Sync (with heal-on-sync for missing IDs in 'both' mode)
# --------------------------------------------------------------------------- #


def _heal_missing_ids(playlist_id: int, row, full_configs: list[ShowConfig]) -> None:
    """For 'both' playlists, re-attempt cross-backend matching for any show
    missing an id on one side. Persists newly-discovered ids so subsequent
    syncs include the show on that backend too.

    Caches the candidate list per backend (one list_all_shows call max per
    backend per sync run, regardless of how many shows need healing).
    """
    if row["backend"] != "both":
        return
    needs_plex = [c for c in full_configs if c.plex_rating_key is None]
    needs_jf = [c for c in full_configs if c.jellyfin_rating_key is None]
    if not needs_plex and not needs_jf:
        return

    plex_cands = _candidates_for("plex") if needs_plex else []
    jf_cands = _candidates_for("jellyfin") if needs_jf else []

    for cfg in needs_plex:
        matched = _find_match(plex_cands, cfg.title or "", _year_hint(cfg))
        if matched:
            cfg.plex_rating_key = matched
            db.set_plex_show_item_id(playlist_id, cfg.rating_key, matched)
            log.info("Healed Plex id for '%s' on playlist %s: %s",
                     cfg.title, playlist_id, matched)
    for cfg in needs_jf:
        matched = _find_match(jf_cands, cfg.title or "", _year_hint(cfg))
        if matched:
            cfg.jellyfin_rating_key = matched
            db.set_jellyfin_show_item_id(playlist_id, cfg.rating_key, matched)
            log.info("Healed Jellyfin id for '%s' on playlist %s: %s",
                     cfg.title, playlist_id, matched)


def sync_playlist(playlist_id: int, force: bool = False) -> tuple[int, int]:
    """Re-evaluate canonical episode lists against current backend metadata
    on every enabled backend. Returns (added, removed) — totals across
    backends.

    `force=True` overrides the per-playlist auto_sync opt-out. Use it for
    user-initiated "Sync Now" actions; the scheduler always uses force=False.
    """
    row = db.get_playlist(playlist_id)
    if not row:
        return (0, 0)
    if not force and not bool(row["auto_sync"]):
        return (0, 0)

    show_rows = db.list_shows(playlist_id)
    full_configs = [_config_from_row(r) for r in show_rows]

    # v1.4.0: genre playlists discover new shows on every sync.
    if _row_get(row, "playlist_type", "manual") == "genre":
        _genre_sync_discover(playlist_id, row, full_configs)
        # Reload after potential adds.
        show_rows = db.list_shows(playlist_id)
        full_configs = [_config_from_row(r) for r in show_rows]

    if not full_configs:
        return (0, 0)

    _heal_missing_ids(playlist_id, row, full_configs)

    added, removed = _rebuild_playlist_tails(row, full_configs, op_label="sync")
    if added or removed:
        log.info("Synced '%s': +%d, -%d", row["name"], added, removed)
    return (added, removed)


def _genre_sync_discover(playlist_id: int, row, current_configs: list[ShowConfig]) -> None:
    """For genre playlists: re-query the backends for genre-matching shows
    and add any that aren't already in the playlist (respecting the
    is_excluded flag — excluded rows count as 'already here, don't re-add').
    """
    genres = _parse_genre_csv(_row_get(row, "genre_filter", None))
    if not genres:
        return
    target_backends = _backends_for(row)
    try:
        candidates = _resolve_genre_shows(genres, target_backends)
    except Exception:
        log.exception("genre discovery failed for playlist %s", row["name"])
        return

    # Build sets of existing IDs across both backends, so we don't re-add a
    # show that's already represented via either id (incl. excluded ones).
    existing_plex = {c.plex_rating_key for c in current_configs if c.plex_rating_key}
    existing_jf = {c.jellyfin_rating_key for c in current_configs if c.jellyfin_rating_key}
    existing_pks = {c.rating_key for c in current_configs}

    new_configs: list[ShowConfig] = []
    for cand in candidates:
        if cand.rating_key in existing_pks:
            continue
        if cand.plex_rating_key and cand.plex_rating_key in existing_plex:
            continue
        if cand.jellyfin_rating_key and cand.jellyfin_rating_key in existing_jf:
            continue
        new_configs.append(cand)

    if not new_configs:
        return
    db.add_shows(playlist_id, [_config_to_db_dict(c) for c in new_configs])
    log.info(
        "Genre sync added %d new show(s) to '%s' matching %s",
        len(new_configs), row["name"], ",".join(genres),
    )


def sync_all() -> None:
    for row in db.list_playlists():
        try:
            sync_playlist(row["id"])
        except Exception:
            log.exception("Sync failed for playlist id=%s", row["id"])


# --------------------------------------------------------------------------- #
# Delete a managed playlist
# --------------------------------------------------------------------------- #


def delete_managed_playlist(playlist_id: int) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        return
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            if client.playlist_exists(pl_id):
                client.delete_playlist(pl_id)
        except Exception:
            log.warning("Failed to delete %s playlist for '%s' (continuing)", tb, row["name"])
    db.delete_playlist(playlist_id)
    log.info("Deleted managed playlist '%s'", row["name"])


# --------------------------------------------------------------------------- #
# Prune watched items
# --------------------------------------------------------------------------- #


def prune_playlist(playlist_id: int, keep_last_n: int | None = None) -> int:
    if keep_last_n is None:
        keep_last_n = _watched_keep()
    row = db.get_playlist(playlist_id)
    if not row:
        return 0

    total = 0
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            items = client.get_playlist_items(pl_id)
            indices = rotation.prune_indices(items, keep_last_n)
            if not indices:
                continue
            remove_keys = [items[i].rating_key for i in indices]
            client.remove_items_from_playlist(pl_id, remove_keys)
            log.info("Pruned %d watched item(s) from '%s' on %s",
                     len(remove_keys), row["name"], tb)
            total += len(remove_keys)
        except Exception:
            log.exception("prune failed on %s for '%s'", tb, row["name"])
    return total


def prune_all() -> None:
    for row in db.list_playlists():
        try:
            prune_playlist(row["id"])
        except Exception:
            log.exception("Prune failed for playlist id=%s", row["id"])


# --------------------------------------------------------------------------- #
# Views for the web UI
# --------------------------------------------------------------------------- #


def get_playlist_view(playlist_id: int) -> PlaylistView | None:
    row = db.get_playlist(playlist_id)
    if not row:
        return None
    all_rows = [dict(s) for s in db.list_shows(playlist_id)]
    shows = [s for s in all_rows if not s.get("is_excluded")]
    excluded_shows = [s for s in all_rows if s.get("is_excluded")]
    item_count = 0
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            c = client.playlist_item_count(pl_id)
            if c > item_count:
                item_count = c
        except Exception:
            log.debug("item count failed on %s for '%s'", tb, row["name"])
    return PlaylistView(
        id=row["id"],
        name=row["name"],
        plex_rating_key=row["plex_rating_key"],
        jellyfin_playlist_id=(
            row["jellyfin_playlist_id"] if "jellyfin_playlist_id" in row.keys() else None
        ),
        backend=row["backend"] if "backend" in row.keys() else "plex",
        shows=shows,
        item_count=item_count,
        sort_mode=row["sort_mode"] or "rotation",
        unwatched_only=bool(row["unwatched_only"]),
        auto_sync=bool(row["auto_sync"]) if "auto_sync" in row.keys() else True,
        block_size=int(_row_get(row, "block_size", 1) or 1),
        shuffle_seed=_row_get(row, "shuffle_seed", None),
        playlist_type=_row_get(row, "playlist_type", "manual") or "manual",
        genre_filter=_row_get(row, "genre_filter", None),
        excluded_shows=excluded_shows,
    )


def list_playlist_views() -> list[PlaylistView]:
    out: list[PlaylistView] = []
    for row in db.list_playlists():
        view = get_playlist_view(row["id"])
        if view:
            out.append(view)
    return out
