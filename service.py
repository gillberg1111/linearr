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

import json
import logging
import os
from dataclasses import dataclass, field

import db
import rotation
import webhooks as _webhooks
from media_client import (
    EpisodeRef,
    MediaClient,
    MovieSummary,
    ShowSummary,
    get_client,
    normalize_title,
    titles_match,
)
from rotation import PlaylistItem
from trakt_client import get_trakt_client

log = logging.getLogger(__name__)


def _compute_playlist_stats(
    playlist_id: int,
    backend: str,
    client: MediaClient,
    pl_id: str,
    configs: list[ShowConfig],
) -> dict:
    import datetime as _dt
    try:
        episodes = client.list_playlist_episodes(pl_id)
    except Exception:
        return {}
    total = len(episodes)
    watched = sum(1 for ep in episodes if getattr(ep, "view_count", 0) or 0 > 0)
    return {
        "synced_at": _dt.datetime.utcnow().isoformat(timespec="seconds"),
        "backend": backend,
        "total_episodes": total,
        "watched_episodes": watched,
    }


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


def _find_match_by_tvdb(candidates: list[ShowSummary], tvdb_id: str) -> str | None:
    """Find a show in candidates by TVDB ID."""
    for s in candidates:
        if s.tvdb_id == tvdb_id:
            return s.rating_key
    return None


def _tvdb_id_for_cfg(cfg: ShowConfig) -> str | None:
    """Look up the TVDB id for a config by asking whichever backend already
    has an id for this show. Returns None if the show can't be found or has
    no TVDB id."""
    for backend in ("plex", "jellyfin"):
        target_id = cfg.id_for(backend)
        if not target_id:
            continue
        try:
            summary = _client_for(backend).get_show_summary(target_id)
            if summary.tvdb_id:
                return summary.tvdb_id
        except Exception:
            continue
    return None


def _find_match_by_tvdb_for_cfg(
    cfg: ShowConfig, candidates: list[ShowSummary], source_backend: str
) -> str | None:
    """If the config already has an id on `source_backend`, look up its TVDB id
    and search `candidates` for a show with the same TVDB id."""
    tvdb_id = _tvdb_id_for_cfg(cfg)
    if not tvdb_id:
        return None
    return _find_match_by_tvdb(candidates, tvdb_id)


def _enrich_configs_with_matches(configs: list[ShowConfig], target_backends: list[str]) -> None:
    """For each config missing an id on a target backend, attempt to find it
    by TVDB ID first, then by title+year. Populates the appropriate *_rating_key
    field in-place.

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
            mid = _find_match_by_tvdb_for_cfg(cfg, cands("plex"), "jellyfin")
            if mid:
                cfg.plex_rating_key = mid
            else:
                mid = _find_match(cands("plex"), cfg.title or "", year)
                if mid:
                    cfg.plex_rating_key = mid
        if "jellyfin" in target_backends and cfg.jellyfin_rating_key is None:
            mid = _find_match_by_tvdb_for_cfg(cfg, cands("jellyfin"), "plex")
            if mid:
                cfg.jellyfin_rating_key = mid
            else:
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


def _build_crossover_map(playlist_id: int) -> dict[tuple[str, int, int], tuple[int, int]]:
    """Build a lookup map for crossover group membership.
    Returns dict[(show_rating_key, season, episode), (group_id, sort_index)].
    """
    cmap: dict[tuple[str, int, int], tuple[int, int]] = {}
    for g in db.list_crossover_groups(playlist_id):
        for link in g["links"]:
            cmap[(link["show_rating_key"], link["season"], link["episode"])] = (
                g["id"], link["sort_index"],
            )
    return cmap


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
    crossover_map: dict[tuple[str, int, int], tuple[int, int]] | None = None,
) -> tuple[int, int]:
    """Recompute the future portion of a playlist on a single backend.

    Returns (added_count, removed_count). Configs without an id on this
    backend are silently filtered out (they don't contribute on this side).

    `block_size` and `shuffle_seed` are only consulted when `sort_mode` is
    'rotation_blocks' or 'shuffle_chronological' respectively. Per-show
    weights for 'rotation_weighted' come from each ShowConfig.weight.
    `crossover_map` is only used in 'air_date' mode.
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
        crossover_map=crossover_map,
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
    crossover_map = _build_crossover_map(row["id"]) if sm == "air_date" else None
    total_added = total_removed = 0
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            added, removed = _rebuild_tail_on(
                tb, client, pl_id, active_configs, sm, uw,
                block_size=bs, shuffle_seed=ss, crossover_map=crossover_map,
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
    relevant = [c for c in configs if c.id_for(backend)]
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
    # v1.5.0 — manual crossover groups
    crossover_groups: list[dict] = field(default_factory=list)
    # v1.8.0 — smart playlist rules
    rule_mode: str = "genre"
    # v2.0.0 — analytics
    last_stats: dict | None = None
    # v2.2.0 — per-playlist pruning toggle
    pruning_enabled: int = 1
    # v2.3.0 — counts for index card stats
    movies_count: int = 0
    episodes_count: int = 0


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
    pruning_enabled: int = 1,
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
    if not pruning_enabled:
        with db.connection() as conn:
            conn.execute(
                "UPDATE managed_playlists SET pruning_enabled=0 WHERE id=?",
                (playlist_id,),
            )
    log.info(
        "Created playlist '%s' (backend=%s, sides created: %s)",
        name, backend, ",".join(sorted(created_ids.keys())) or "none",
    )
    try:
        _row = db.get_playlist(playlist_id)
        if _row:
            _webhooks.fire("playlist.created", playlist=_webhooks._playlist_info(dict(_row)))
    except Exception:
        pass
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

    existing = {
        s["show_rating_key"]: s
        for s in db.list_shows(playlist_id)
        if not s["is_excluded"]
    }
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


def _dedup_show_summaries_to_configs(
    per_backend: dict[str, list[ShowSummary]],
    target_backends: list[str],
) -> list[ShowConfig]:
    """Deduplicate ShowSummary lists from multiple backends into a single
    list of ShowConfigs. Matches via normalized title+year first, then
    TVDB ID fallback. Cross-backend IDs are enriched at the end for 'both'
    setups."""
    out: list[ShowConfig] = []
    seen_keys: dict[str, list[int]] = {}
    tvdb_seen: dict[str, int] = {}
    _years: dict[int, int | None] = {}

    for tb in target_backends:
        for s in per_backend.get(tb, []):
            nk = normalize_title(s.title)

            match_idx: int | None = None
            for idx in seen_keys.get(nk, []):
                existing_year = _years.get(idx)
                if (
                    existing_year is not None
                    and s.year is not None
                    and existing_year != s.year
                ):
                    continue
                match_idx = idx
                break

            if match_idx is None and s.tvdb_id:
                match_idx = tvdb_seen.get(s.tvdb_id)

            if match_idx is not None:
                if tb == "plex" and out[match_idx].plex_rating_key is None:
                    out[match_idx].plex_rating_key = s.rating_key
                if tb == "jellyfin" and out[match_idx].jellyfin_rating_key is None:
                    out[match_idx].jellyfin_rating_key = s.rating_key
                if _years.get(match_idx) is None and s.year is not None:
                    _years[match_idx] = s.year
                if not out[match_idx].thumb and s.thumb:
                    out[match_idx].thumb = s.thumb
                if nk not in seen_keys or match_idx not in seen_keys[nk]:
                    seen_keys.setdefault(nk, []).append(match_idx)
                if s.tvdb_id and s.tvdb_id not in tvdb_seen:
                    tvdb_seen[s.tvdb_id] = match_idx
                continue

            cfg = ShowConfig(
                rating_key=s.rating_key,
                title=s.title,
                thumb=s.thumb,
                plex_rating_key=s.rating_key if tb == "plex" else None,
                jellyfin_rating_key=s.rating_key if tb == "jellyfin" else None,
            )
            new_idx = len(out)
            seen_keys.setdefault(nk, []).append(new_idx)
            _years[new_idx] = s.year
            if s.tvdb_id:
                tvdb_seen[s.tvdb_id] = new_idx
            out.append(cfg)

    if "plex" in target_backends and "jellyfin" in target_backends:
        _enrich_configs_with_matches(out, target_backends)
    out.sort(key=lambda c: (c.title or "").lower())
    return out


def _resolve_genre_shows(
    genres: list[str],
    target_backends: list[str],
) -> list[ShowConfig]:
    if not genres:
        return []
    per_backend: dict[str, list] = {}
    for tb in target_backends:
        try:
            per_backend[tb] = _client_for(tb).list_shows_by_genres(genres)
        except Exception:
            log.exception("genre resolve failed on %s", tb)
            per_backend[tb] = []
    return _dedup_show_summaries_to_configs(per_backend, target_backends)


def _apply_rules(shows: list[ShowSummary], rules: list[dict]) -> list[ShowSummary]:
    result = list(shows)
    for rule in rules:
        rt = rule["rule_type"]
        op = rule["operator"]
        val = rule["value"]

        if rt == "genre":
            continue
        elif rt == "year_min":
            try:
                y = int(val)
                if op == "include":
                    result = [s for s in result if s.year is None or s.year >= y]
            except ValueError:
                pass
        elif rt == "year_max":
            try:
                y = int(val)
                if op == "include":
                    result = [s for s in result if s.year is None or s.year <= y]
            except ValueError:
                pass
        elif rt == "status":
            v_lower = val.lower()
            if op == "include":
                result = [s for s in result
                          if s.status is None or s.status.lower() == v_lower]
            elif op == "exclude":
                result = [s for s in result
                          if s.status is None or s.status.lower() != v_lower]
        elif rt == "content_rating":
            v_lower = val.lower()
            if op == "include":
                result = [s for s in result
                          if s.content_rating is None or s.content_rating.lower() == v_lower]
            elif op == "exclude":
                result = [s for s in result
                          if s.content_rating is None or s.content_rating.lower() != v_lower]
        elif rt == "season_max":
            try:
                n = int(val)
                if op == "include":
                    result = [s for s in result
                              if s.season_count is None or s.season_count <= n]
            except ValueError:
                pass
        elif rt == "season_min":
            try:
                n = int(val)
                if op == "include":
                    result = [s for s in result
                              if s.season_count is None or s.season_count >= n]
            except ValueError:
                pass
        elif rt == "rating_min":
            try:
                r = float(val)
                if op == "include":
                    result = [s for s in result
                              if s.community_rating is None or s.community_rating >= r]
            except ValueError:
                pass

    return result


def _resolve_smart_shows(
    rules: list[dict],
    target_backends: list[str],
) -> list[ShowConfig]:
    genre_includes = [r["value"] for r in rules
                      if r["rule_type"] == "genre" and r["operator"] == "include"]
    non_genre_rules = [r for r in rules if r["rule_type"] != "genre"]

    per_backend: dict[str, list] = {}
    for tb in target_backends:
        try:
            client = _client_for(tb)
            if genre_includes:
                shows = client.list_shows_by_genres(genre_includes)
            else:
                shows = client.list_all_shows()
            shows = _apply_rules(shows, non_genre_rules)
            per_backend[tb] = shows
        except Exception:
            log.exception("smart rule query failed on %s", tb)
            per_backend[tb] = []

    return _dedup_show_summaries_to_configs(per_backend, target_backends)


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
    weights: dict[str, int] | None = None,
    pruning_enabled: int = 1,
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
    # Apply per-show weights supplied from the creation form.
    if weights:
        for cfg in configs:
            if cfg.rating_key in weights:
                cfg.weight = max(1, weights[cfg.rating_key])
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
        pruning_enabled=pruning_enabled,
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
    try:
        _row = db.get_playlist(playlist_id)
        if _row:
            _webhooks.fire("playlist.created", playlist=_webhooks._playlist_info(dict(_row)))
    except Exception:
        pass
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
        matched = (
            _find_match_by_tvdb_for_cfg(cfg, plex_cands, "jellyfin")
            or _find_match(plex_cands, cfg.title or "", _year_hint(cfg))
        )
        if matched:
            cfg.plex_rating_key = matched
            db.set_plex_show_item_id(playlist_id, cfg.rating_key, matched)
            log.info("Healed Plex id for '%s' on playlist %s: %s",
                     cfg.title, playlist_id, matched)
    for cfg in needs_jf:
        matched = (
            _find_match_by_tvdb_for_cfg(cfg, jf_cands, "plex")
            or _find_match(jf_cands, cfg.title or "", _year_hint(cfg))
        )
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

    # v2.2.0: franchise playlists have their own sync logic.
    if _row_get(row, "playlist_type", "manual") == "franchise":
        return sync_franchise_playlist(playlist_id, force=force)

    if not full_configs:
        return (0, 0)

    _heal_missing_ids(playlist_id, row, full_configs)

    added, removed = _rebuild_playlist_tails(row, full_configs, op_label="sync")
    if added or removed:
        log.info("Synced '%s': +%d, -%d", row["name"], added, removed)
        try:
            _webhooks.fire(
                "playlist.synced",
                playlist=_webhooks._playlist_info(dict(row)),
                data={"added": added, "removed": removed},
            )
        except Exception:
            pass

    # v2.0.0: collect stats from the first available backend side.
    for _be, _cl, _pl_id in _clients_for_playlist(row):
        if not _pl_id:
            continue
        stats = _compute_playlist_stats(playlist_id, _be, _cl, _pl_id, full_configs)
        if stats:
            db.update_playlist_stats(playlist_id, stats)
        break

    return (added, removed)


def refresh_playlist_metadata(playlist_id: int) -> dict[str, int]:
    """Trigger metadata refresh on every backend for every non-excluded show
    in the playlist. Returns {"ok": N, "errors": M} — always returns, never
    raises."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No playlist with id={playlist_id}")
    ok = 0
    errors = 0
    for backend, client, _pl_id in _clients_for_playlist(row):
        for show_row in db.list_shows(playlist_id):
            if show_row["is_excluded"]:
                continue
            key = (
                show_row["plex_show_item_id"] if backend == "plex"
                else show_row["jellyfin_show_item_id"]
            )
            if not key:
                continue
            try:
                client.refresh_show_metadata(key)
                ok += 1
            except Exception:
                log.warning("refresh failed for show %s on %s", key, backend)
                errors += 1
    return {"ok": ok, "errors": errors}


def link_show_backend(
    playlist_id: int,
    show_rating_key: str,
    backend: str,
    target_key: str,
) -> None:
    """Manually link a show's ID on one backend to the existing playlist row.
    Triggers a tail rebuild on every enabled backend."""
    if backend == "plex":
        db.set_plex_show_item_id(playlist_id, show_rating_key, target_key)
    elif backend == "jellyfin":
        db.set_jellyfin_show_item_id(playlist_id, show_rating_key, target_key)
    else:
        raise ValueError(f"Unknown backend: {backend!r}")
    row = db.get_playlist(playlist_id)
    full_configs = [_config_from_row(r) for r in db.list_shows(playlist_id)]
    _rebuild_playlist_tails(row, full_configs, op_label="manual link")


def _genre_sync_discover(playlist_id: int, row, current_configs: list[ShowConfig]) -> None:
    rule_mode = _row_get(row, "rule_mode", "genre")
    target_backends = _backends_for(row)

    if rule_mode == "rules":
        rules = [dict(r) for r in db.list_rules(playlist_id)]
        if not rules:
            return
        try:
            candidates = _resolve_smart_shows(rules, target_backends)
        except Exception:
            log.exception("smart rule discovery failed for playlist %s", row["name"])
            return
    else:
        genres = _parse_genre_csv(_row_get(row, "genre_filter", None))
        if not genres:
            return
        try:
            candidates = _resolve_genre_shows(genres, target_backends)
        except Exception:
            log.exception("genre discovery failed for playlist %s", row["name"])
            return

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
        "Sync added %d new show(s) to '%s'",
        len(new_configs), row["name"],
    )


# ── v2.2.0 — Franchise playlists ──────────────────────────────────────────


def _load_prebaked_franchises() -> list[dict]:
    path = os.path.join(os.path.dirname(__file__), "defaults", "franchises.json")
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def _normalize_for_match(title: str) -> str:
    import re as _re
    t = title.lower()
    t = _re.sub(r"[^\w\s]", " ", t)
    t = _re.sub(r"\s+", " ", t).strip()
    return t


def _build_backend_cache(backend: str, client: MediaClient) -> dict:
    all_movies = client.list_all_movies()
    all_shows = client.list_all_shows()
    return {
        "client": client,
        "movie_by_tmdb": {m.tmdb_id: m for m in all_movies if m.tmdb_id},
        "movie_by_title_year": {
            (_normalize_for_match(m.title), m.year): m for m in all_movies
        },
        "show_by_tvdb": {
            int(s.tvdb_id): s for s in all_shows if s.tvdb_id
        },
        "show_by_title_year": {
            (_normalize_for_match(s.title), s.year): s for s in all_shows
        },
        "episode_cache": {},
    }


def _fetch_and_store_franchise(
    key: str,
    name: str,
    source: str = "trakt",
    trakt_user: str | None = None,
    trakt_slug: str | None = None,
) -> int:
    if source == "local":
        return _fetch_and_store_franchise_local(key, name)

    if source == "user":
        defn = db.get_franchise_definition(key)
        if defn:
            return defn["id"]
        raise ValueError(f"User franchise '{key}' not found in DB")

    if source != "trakt":
        raise ValueError(f"Unknown franchise source: {source}")

    if not trakt_user or not trakt_slug:
        raise ValueError("trakt_user and trakt_slug are required for source='trakt'")

    from datetime import datetime, timezone

    trakt = get_trakt_client()
    items = trakt.fetch_list_items(trakt_user, trakt_slug)
    new_hash = trakt.content_hash(items)

    existing = db.get_franchise_definition(key)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    definition_id = db.upsert_franchise_definition(
        key=key,
        name=name,
        source="trakt",
        trakt_user=trakt_user,
        trakt_slug=trakt_slug,
        fetched_at=fetched_at,
        content_hash=new_hash,
        item_count=len(items),
    )

    if existing is None or existing.get("content_hash") != new_hash:
        db.replace_franchise_items(definition_id, items)
        log.info("Franchise '%s': stored %d items (hash changed)", name, len(items))
    else:
        log.debug("Franchise '%s': no changes (hash match)", name)

    return definition_id


def _fetch_and_store_franchise_local(key: str, name: str) -> int:
    from datetime import datetime, timezone

    path = os.path.join(
        os.path.dirname(__file__), "defaults", "franchise_data", f"{key}.json"
    )
    try:
        with open(path) as f:
            raw = json.load(f)
    except FileNotFoundError:
        raise FileNotFoundError(f"Local franchise file not found: {path}")
    except json.JSONDecodeError as e:
        raise ValueError(f"Invalid JSON in franchise file {path}: {e}")

    items = raw.get("items", [])
    if not items:
        raise ValueError(f"Franchise file {path} has no 'items' array")

    normalised = []
    for i, item in enumerate(items):
        item = dict(item)
        item.setdefault("rank", i + 1)
        item.setdefault("tmdb_id", None)
        item.setdefault("tvdb_id", None)
        item.setdefault("imdb_id", None)
        item.setdefault("year", None)
        item.setdefault("season_number", None)
        item.setdefault("episode_number", None)
        item.setdefault("show_title", None)
        item.setdefault("show_tvdb_id", None)
        normalised.append(item)

    new_hash = get_trakt_client().content_hash(normalised)
    existing = db.get_franchise_definition(key)
    fetched_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    definition_id = db.upsert_franchise_definition(
        key=key,
        name=name,
        source="local",
        trakt_user=None,
        trakt_slug=None,
        fetched_at=fetched_at,
        content_hash=new_hash,
        item_count=len(normalised),
    )

    if existing is None or existing.get("content_hash") != new_hash:
        db.replace_franchise_items(definition_id, normalised)
        log.info("Franchise '%s' (local): stored %d items", name, len(normalised))
    else:
        log.debug("Franchise '%s' (local): no changes", name)

    return definition_id


def _resolve_franchise_item(fi: dict, cache: dict) -> str | None:
    item_type = fi["item_type"]
    client: MediaClient = cache["client"]

    if item_type == "movie":
        if fi.get("tmdb_id"):
            m = cache["movie_by_tmdb"].get(fi["tmdb_id"])
            if m:
                return m.rating_key
        key = (_normalize_for_match(fi["title"]), fi.get("year"))
        m = cache["movie_by_title_year"].get(key)
        return m.rating_key if m else None

    elif item_type == "episode":
        show_tvdb = fi.get("show_tvdb_id")
        if not show_tvdb:
            return None
        ep_cache = cache["episode_cache"]
        if show_tvdb not in ep_cache:
            show = cache["show_by_tvdb"].get(show_tvdb)
            if show:
                try:
                    eps = client.episodes_for_show(
                        show.rating_key, include_specials=True
                    )
                    ep_cache[show_tvdb] = {
                        (e.season, e.episode): e for e in eps
                    }
                except Exception:
                    ep_cache[show_tvdb] = {}
            else:
                ep_cache[show_tvdb] = {}
        ep = ep_cache[show_tvdb].get(
            (fi["season_number"], fi["episode_number"])
        )
        return ep.rating_key if ep else None

    elif item_type in ("show", "season"):
        return None

    return None


def _expand_franchise_show_item(fi: dict, cache: dict) -> list[str]:
    client: MediaClient = cache["client"]
    show_tvdb = fi.get("tvdb_id") or fi.get("show_tvdb_id")
    if not show_tvdb:
        return []

    show = cache["show_by_tvdb"].get(show_tvdb)
    if not show:
        key = (_normalize_for_match(fi.get("show_title") or fi["title"]), fi.get("year"))
        show = cache["show_by_title_year"].get(key)
    if not show:
        return []

    try:
        if fi["item_type"] == "show":
            eps = client.episodes_for_show(show.rating_key)
        else:
            season_num = fi["season_number"]
            eps = client.episodes_for_show(
                show.rating_key,
                start_season=season_num,
                end_season=season_num,
            )
        return [e.rating_key for e in eps]
    except Exception:
        log.warning("Failed to expand show/season item '%s'", fi.get("title"), exc_info=True)
        return []


def _match_franchise_to_library(
    definition_id: int,
    playlist_id: int,
    row: dict,
) -> tuple[list[str], list[str], int]:
    franchise_items = db.list_franchise_items(definition_id)
    if not franchise_items:
        return [], [], 0

    backend_caches: dict[str, dict] = {}
    for backend, client_q, _pl_id in _clients_for_playlist(row):
        try:
            backend_caches[backend] = _build_backend_cache(backend, client_q)
        except Exception:
            log.warning("Failed to build library cache for backend=%s", backend, exc_info=True)

    plex_keys: list[str] = []
    jellyfin_keys: list[str] = []
    missing_count = 0

    for fi in franchise_items:
        plex_found = False
        plex_item_id: str | None = None
        jellyfin_found = False
        jellyfin_item_id: str | None = None

        item_type = fi["item_type"]
        if item_type in ("show", "season"):
            for backend, cache in backend_caches.items():
                keys = _expand_franchise_show_item(fi, cache)
                if keys:
                    if backend == "plex":
                        plex_found = True
                        plex_item_id = keys[0]
                        plex_keys.extend(keys)
                    else:
                        jellyfin_found = True
                        jellyfin_item_id = keys[0]
                        jellyfin_keys.extend(keys)

            db.upsert_franchise_match_state(
                franchise_item_id=fi["id"],
                playlist_id=playlist_id,
                plex_found=plex_found,
                plex_item_id=plex_item_id,
                jellyfin_found=jellyfin_found,
                jellyfin_item_id=jellyfin_item_id,
            )

            if not plex_found and not jellyfin_found:
                missing_count += 1
        else:
            for backend, cache in backend_caches.items():
                found_key = _resolve_franchise_item(fi, cache)
                if found_key:
                    if backend == "plex":
                        plex_found = True
                        plex_item_id = found_key
                        plex_keys.append(found_key)
                    else:
                        jellyfin_found = True
                        jellyfin_item_id = found_key
                        jellyfin_keys.append(found_key)

            db.upsert_franchise_match_state(
                franchise_item_id=fi["id"],
                playlist_id=playlist_id,
                plex_found=plex_found,
                plex_item_id=plex_item_id,
                jellyfin_found=jellyfin_found,
                jellyfin_item_id=jellyfin_item_id,
            )

            if not plex_found and not jellyfin_found:
                missing_count += 1

    return plex_keys, jellyfin_keys, missing_count


def _build_backend_ordered_keys(
    backend: str,
    client: MediaClient,
    franchise_items: list[dict],
    match_state: dict[int, dict],
) -> list[str]:
    keys = []
    cache = _build_backend_cache(backend, client)
    for fi in franchise_items:
        ms = match_state.get(fi["id"], {})
        found = ms.get(f"{backend}_found", 0)
        if not found:
            continue
        if fi["item_type"] in ("show", "season"):
            keys.extend(_expand_franchise_show_item(fi, cache))
        else:
            k = ms.get(f"{backend}_item_id")
            if k:
                keys.append(k)
    return keys


def create_franchise_playlist(
    name: str,
    backend: str,
    franchise_key: str,
    source: str = "trakt",
    trakt_user: str | None = None,
    trakt_slug: str | None = None,
    franchise_name: str = "",
) -> int:
    from datetime import datetime, timezone

    if backend not in db.VALID_BACKENDS:
        raise ValueError(f"Invalid backend: {backend}")

    definition_id = _fetch_and_store_franchise(
        key=franchise_key,
        name=franchise_name,
        source=source,
        trakt_user=trakt_user,
        trakt_slug=trakt_slug,
    )

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with db.connection() as conn:
        cur = conn.execute(
            """INSERT INTO managed_playlists
               (name, backend, playlist_type, franchise_definition_id,
                sort_mode, block_size, unwatched_only, auto_sync,
                pruning_enabled, created_at)
               VALUES (?, ?, 'franchise', ?, 'franchise', 1, 0, 1, 0, ?)""",
            (name, backend, definition_id, now),
        )
        playlist_id = cur.lastrowid

    row = db.get_playlist(playlist_id)
    plex_keys, jellyfin_keys, missing_count = _match_franchise_to_library(
        definition_id, playlist_id, row
    )

    log.info(
        "Creating franchise playlist '%s': %d plex keys, %d jellyfin keys, %d missing",
        name, len(plex_keys), len(jellyfin_keys), missing_count,
    )

    for tb, client, pl_id in _clients_for_playlist(row):
        backend_keys = plex_keys if tb == "plex" else jellyfin_keys
        if backend_keys:
            try:
                new_pl_id = client.create_playlist(name, backend_keys)
                if tb == "plex":
                    with db.connection() as conn2:
                        conn2.execute(
                            "UPDATE managed_playlists SET plex_rating_key=? WHERE id=?",
                            (new_pl_id, playlist_id),
                        )
                else:
                    with db.connection() as conn2:
                        conn2.execute(
                            "UPDATE managed_playlists SET jellyfin_playlist_id=? WHERE id=?",
                            (new_pl_id, playlist_id),
                        )
            except Exception:
                log.warning("Failed to create franchise playlist on %s", tb, exc_info=True)

    try:
        _webhooks.fire(
            "playlist.created",
            playlist=_webhooks._playlist_info(dict(db.get_playlist(playlist_id))),
        )
    except Exception:
        pass

    return playlist_id


def sync_franchise_playlist(playlist_id: int, force: bool = False) -> tuple[int, int]:
    row = db.get_playlist(playlist_id)
    if not row:
        return (0, 0)
    if not force and not bool(row["auto_sync"]):
        return (0, 0)
    row_d = dict(row)

    definition_id = row_d.get("franchise_definition_id")
    if not definition_id:
        log.warning("Franchise playlist %d has no definition_id", playlist_id)
        return (0, 0)

    plex_keys, jellyfin_keys, missing_count = _match_franchise_to_library(
        definition_id, playlist_id, row
    )
    log.debug(
        "Franchise sync '%s': %d plex, %d jf, %d missing",
        row["name"], len(plex_keys), len(jellyfin_keys), missing_count,
    )

    added = removed = 0
    for tb, client, pl_id in _clients_for_playlist(row):
        if not pl_id:
            continue
        try:
            current = client.get_playlist_items(pl_id)
            current_keys = [it.rating_key for it in current]
            be_items = db.list_franchise_items(definition_id)
            be_match = db.list_franchise_match_state(playlist_id)
            be_keys = _build_backend_ordered_keys(tb, client, be_items, be_match)

            added_keys = [k for k in be_keys if k not in set(current_keys)]
            removed_keys = [k for k in current_keys if k not in set(be_keys)]
            if added_keys or removed_keys:
                client.replace_playlist_items(pl_id, be_keys)
                added += len(added_keys)
                removed += len(removed_keys)
        except Exception:
            log.warning(
                "Franchise sync failed for backend=%s playlist=%d",
                tb, playlist_id, exc_info=True
            )

    if added or removed:
        try:
            _webhooks.fire(
                "playlist.synced",
                playlist=_webhooks._playlist_info(dict(row)),
                data={"added": added, "removed": removed},
            )
        except Exception:
            pass

    return added, removed


def franchise_items_for_maker(definition_id: int) -> list[dict]:
    rows = db.list_franchise_items(definition_id)
    out = []
    for r in rows:
        out.append({
            "rank": r["rank"],
            "item_type": r["item_type"],
            "title": r["title"],
            "year": r.get("year"),
            "tmdb_id": r.get("tmdb_id"),
            "imdb_id": r.get("imdb_id"),
            "tvdb_id": r.get("tvdb_id"),
            "season_number": r.get("season_number"),
            "episode_number": r.get("episode_number"),
            "show_title": r.get("show_title"),
            "show_tvdb_id": r.get("show_tvdb_id"),
        })
    return out


def save_user_franchise_playlist(
    *,
    playlist_id: int | None,
    name: str,
    backend: str,
    items: list[dict],
    description: str = "",
    forked_from_key: str | None = None,
) -> int:
    import time as _time
    from datetime import datetime, timezone

    if not name.strip():
        raise ValueError("Playlist name is required")
    if backend not in db.VALID_BACKENDS:
        raise ValueError(f"Invalid backend: {backend!r}")
    if not items:
        raise ValueError("At least one franchise item is required")

    normalised = []
    for i, item in enumerate(items):
        item = dict(item)
        item.setdefault("rank", i + 1)
        item.setdefault("item_type", "movie")
        item.setdefault("title", "")
        item.setdefault("year", None)
        item.setdefault("tmdb_id", None)
        item.setdefault("imdb_id", None)
        item.setdefault("tvdb_id", None)
        item.setdefault("season_number", None)
        item.setdefault("episode_number", None)
        item.setdefault("show_title", None)
        item.setdefault("show_tvdb_id", None)
        normalised.append(item)

    new_hash = get_trakt_client().content_hash(normalised)

    if playlist_id is not None:
        row = db.get_playlist(playlist_id)
        if not row:
            raise ValueError(f"Playlist {playlist_id} not found")
        row_d = dict(row)

        old_defn_id = row_d.get("franchise_definition_id")
        old_defn = db.get_franchise_definition_by_id(old_defn_id) if old_defn_id else None

        if old_defn and old_defn.get("source") in ("trakt", "local"):
            old_key = old_defn["key"]
            new_key = f"user_{playlist_id}_{int(_time.time())}"
            defn_id = db.insert_franchise_definition(
                key=new_key,
                name=name,
                source="user",
                forked_from_key=old_key,
                content_hash=new_hash,
                item_count=len(normalised),
            )
            db.replace_franchise_items(defn_id, normalised)
            db.rebind_playlist_franchise(playlist_id, defn_id)
            with db.connection() as conn:
                conn.execute(
                    "UPDATE managed_playlists SET name = ? WHERE id = ?",
                    (name, playlist_id),
                )
            log.info(
                "Forked franchise '%s' (from '%s') for playlist %d",
                name, old_key, playlist_id,
            )

        elif old_defn and old_defn.get("source") == "user":
            defn_id = old_defn_id
            db.replace_franchise_items(defn_id, normalised)
            db.update_franchise_definition_metadata(
                defn_id, content_hash=new_hash, item_count=len(normalised)
            )
            with db.connection() as conn:
                conn.execute(
                    "UPDATE managed_playlists SET name = ? WHERE id = ?",
                    (name, playlist_id),
                )
            log.info("Updated user franchise '%s' for playlist %d", name, playlist_id)

        else:
            raise ValueError("Playlist is not a franchise playlist")

        row2 = db.get_playlist(playlist_id)
        sync_franchise_playlist(playlist_id, force=True)
        try:
            _webhooks.fire(
                "playlist.synced",
                playlist=_webhooks._playlist_info(dict(row2)),
            )
        except Exception:
            pass
        return playlist_id

    else:
        # Pre-check name uniqueness for a clean user-facing error before SQLite raises.
        with db.connection() as conn:
            existing = conn.execute(
                "SELECT 1 FROM managed_playlists WHERE name = ?", (name,),
            ).fetchone()
        if existing:
            raise ValueError(
                f"A playlist named {name!r} already exists. Pick a different name."
            )

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        with db.connection() as conn:
            cur = conn.execute(
                """INSERT INTO managed_playlists
                   (name, backend, playlist_type, sort_mode,
                    block_size, unwatched_only, auto_sync, pruning_enabled, created_at)
                   VALUES (?, ?, 'franchise', 'franchise', 1, 0, 1, 0, ?)""",
                (name, backend, now),
            )
            playlist_id = cur.lastrowid

        defn_key = f"user_{playlist_id}"
        defn_id = db.insert_franchise_definition(
            key=defn_key,
            name=name,
            source="user",
            forked_from_key=forked_from_key,
            content_hash=new_hash,
            item_count=len(normalised),
        )
        db.replace_franchise_items(defn_id, normalised)

        with db.connection() as conn:
            conn.execute(
                "UPDATE managed_playlists SET franchise_definition_id = ? WHERE id = ?",
                (defn_id, playlist_id),
            )

        row = db.get_playlist(playlist_id)
        plex_keys, jellyfin_keys, missing_count = _match_franchise_to_library(
            defn_id, playlist_id, row
        )

        log.info(
            "Creating user franchise playlist '%s': %d plex keys, %d jellyfin keys, %d missing",
            name, len(plex_keys), len(jellyfin_keys), missing_count,
        )

        for tb, client, pl_id in _clients_for_playlist(row):
            backend_keys = plex_keys if tb == "plex" else jellyfin_keys
            if backend_keys:
                try:
                    new_pl_id = client.create_playlist(name, backend_keys)
                    if tb == "plex":
                        with db.connection() as conn2:
                            conn2.execute(
                                "UPDATE managed_playlists SET plex_rating_key=? WHERE id=?",
                                (new_pl_id, playlist_id),
                            )
                    else:
                        with db.connection() as conn2:
                            conn2.execute(
                                "UPDATE managed_playlists SET jellyfin_playlist_id=? WHERE id=?",
                                (new_pl_id, playlist_id),
                            )
                except Exception:
                    log.warning("Failed to create franchise playlist on %s", tb, exc_info=True)

        try:
            _webhooks.fire(
                "playlist.created",
                playlist=_webhooks._playlist_info(dict(db.get_playlist(playlist_id))),
            )
        except Exception:
            pass

        return playlist_id


def restore_bundled_franchise(playlist_id: int) -> bool:
    row = db.get_playlist(playlist_id)
    if not row:
        return False
    row_d = dict(row)

    defn_id = row_d.get("franchise_definition_id")
    if not defn_id:
        return False

    defn = db.get_franchise_definition_by_id(defn_id)
    if not defn or defn.get("source") != "user":
        return False

    fork_key = defn.get("forked_from_key")
    if not fork_key:
        return False

    bundled = db.get_franchise_definition(fork_key)
    if not bundled:
        return False

    db.rebind_playlist_franchise(playlist_id, bundled["id"])

    ref_count = db.count_playlists_by_franchise_definition(defn_id)
    if ref_count <= 0:
        db.delete_franchise_definition(defn_id)

    sync_franchise_playlist(playlist_id, force=True)
    return True


def refresh_franchise_definitions() -> None:
    definitions = db.list_franchise_definitions()
    for defn in definitions:
        if defn["source"] in ("local", "user"):
            continue
        if defn["source"] != "trakt":
            continue
        try:
            old_hash = defn.get("content_hash")
            definition_id = _fetch_and_store_franchise(
                key=defn["key"],
                name=defn["name"],
                trakt_user=defn["trakt_user"],
                trakt_slug=defn["trakt_slug"],
            )
            new_defn = db.get_franchise_definition(defn["key"])
            if new_defn and new_defn.get("content_hash") != old_hash:
                log.info(
                    "Franchise '%s' changed, re-syncing dependent playlists",
                    defn["name"],
                )
                for pl_row in db.get_playlists_by_franchise_definition(definition_id):
                    try:
                        sync_franchise_playlist(pl_row["id"], force=True)
                    except Exception:
                        log.warning(
                            "Re-sync failed for franchise playlist %d",
                            pl_row["id"], exc_info=True,
                        )
        except Exception:
            log.warning(
                "refresh_franchise_definitions failed for '%s'",
                defn.get("key"), exc_info=True,
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
    try:
        _webhooks.fire("playlist.deleted", playlist=_webhooks._playlist_info(dict(row)))
    except Exception:
        pass
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
            if _row_get(row, "playlist_type", "manual") == "franchise":
                continue
            if not _row_get(row, "pruning_enabled", 1):
                continue
            prune_playlist(row["id"])
        except Exception:
            log.exception("Prune failed for playlist id=%s", row["id"])


# --------------------------------------------------------------------------- #
# Views for the web UI
# --------------------------------------------------------------------------- #


def _annotate_crossover_titles(groups: list[dict], show_rows: list[dict]) -> list[dict]:
    """Add show_title to each crossover link dict, falling back to the raw key."""
    title_by_key = {s["show_rating_key"]: s["show_title"] for s in show_rows}
    for group in groups:
        for link in group["links"]:
            link["show_title"] = title_by_key.get(
                link["show_rating_key"], link["show_rating_key"]
            )
    return groups


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

    # v2.3.0 — movie + episode counts for index card stats
    playlist_type_val = _row_get(row, "playlist_type", "manual") or "manual"
    movies_count = 0
    if playlist_type_val == "franchise":
        defn_id = _row_get(row, "franchise_definition_id", None)
        if defn_id:
            try:
                for fi in db.list_franchise_items(defn_id):
                    if fi.get("item_type") == "movie":
                        movies_count += 1
            except Exception:
                pass
    else:
        for s in shows:
            for csv_col in ("movie_rating_keys", "jellyfin_movie_item_ids"):
                csv = (s.get(csv_col) or "").strip()
                if csv:
                    movies_count = max(
                        movies_count,
                        sum(1 for x in csv.split(",") if x.strip()),
                    )
            # Per-show movie counts add up across the playlist
        # Simpler: count total non-empty CSV entries across shows on the
        # primary backend's column (Plex's movie_rating_keys by default;
        # jellyfin column mirrors it).
        movies_count = 0
        for s in shows:
            csv = (s.get("movie_rating_keys") or "").strip()
            if not csv:
                csv = (s.get("jellyfin_movie_item_ids") or "").strip()
            if csv:
                movies_count += sum(1 for x in csv.split(",") if x.strip())

    episodes_count = max(0, item_count - movies_count)

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
        crossover_groups=_annotate_crossover_titles(
            db.list_crossover_groups(playlist_id), all_rows
        ),
        rule_mode=_row_get(row, "rule_mode", "genre") or "genre",
        last_stats=(json.loads(_row_get(row, "last_stats", None) or ""
                              ) if _row_get(row, "last_stats", None) else None),
        pruning_enabled=int(_row_get(row, "pruning_enabled", 1) or 1),
        movies_count=movies_count,
        episodes_count=episodes_count,
    )


def list_playlist_views() -> list[PlaylistView]:
    out: list[PlaylistView] = []
    for row in db.list_playlists():
        view = get_playlist_view(row["id"])
        if view:
            out.append(view)
    return out
