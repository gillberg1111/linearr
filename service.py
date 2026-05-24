"""High-level operations: create / edit / prune managed rotating playlists.

Bridges db (managed state), plex_client (server I/O), and rotation (pure logic).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

import db
import plex_client as plex
import rotation
from rotation import PlaylistItem

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# Config struct
# --------------------------------------------------------------------------- #


@dataclass
class ShowConfig:
    rating_key: str
    title: str = ""
    thumb: str | None = None
    start_season: int = 1
    end_season: int | None = None
    include_specials: bool = False
    include_movies: bool = False
    movie_rating_keys: list[str] = field(default_factory=list)


def _watched_keep() -> int:
    try:
        return max(0, int(os.environ.get("WATCHED_KEEP", "2")))
    except ValueError:
        return 2


def _playlist_items_for_rotation(playlist) -> list[PlaylistItem]:
    items: list[PlaylistItem] = []
    for ep in playlist.items():
        oad = getattr(ep, "originallyAvailableAt", None)
        air = None
        if oad is not None:
            try:
                air = oad.date().isoformat() if hasattr(oad, "date") else oad.isoformat()
            except Exception:
                air = None
        is_movie = getattr(ep, "type", None) == "movie" or not getattr(ep, "grandparentRatingKey", None)
        items.append(
            PlaylistItem(
                rating_key=str(ep.ratingKey),
                show_rating_key=str(getattr(ep, "grandparentRatingKey", "") or ""),
                season=int(getattr(ep, "seasonNumber", 0) or 0) if not is_movie else 999,
                episode=int(getattr(ep, "index", 0) or 0) if not is_movie else 1,
                view_count=int(getattr(ep, "viewCount", 0) or 0),
                view_offset_ms=int(getattr(ep, "viewOffset", 0) or 0),
                title=getattr(ep, "title", "") or "",
                air_date=air,
                kind="movie" if is_movie else "episode",
            )
        )
    return items


def _hydrate_configs(configs: list[ShowConfig]) -> None:
    """Fill in missing title/thumb from Plex."""
    for cfg in configs:
        if not cfg.title or cfg.thumb is None:
            summary = plex.get_show_summary(cfg.rating_key)
            cfg.title = cfg.title or summary.title
            if cfg.thumb is None:
                cfg.thumb = summary.thumb


def _episodes_for_config(cfg: ShowConfig, unwatched_only: bool = False) -> list[plex.EpisodeRef]:
    eps = plex.episodes_for_show(
        cfg.rating_key,
        start_season=cfg.start_season,
        end_season=cfg.end_season,
        include_specials=cfg.include_specials,
    )
    if unwatched_only:
        eps = [e for e in eps if e.view_count == 0]

    # Append associated movies (if any) as pseudo-episodes attached to this show
    if cfg.include_movies and cfg.movie_rating_keys:
        movie_refs: list[plex.EpisodeRef] = []
        for mrk in cfg.movie_rating_keys:
            try:
                m_item = plex.server().fetchItem(int(mrk))
            except Exception:
                continue
            oad = getattr(m_item, "originallyAvailableAt", None)
            air = None
            if oad is not None:
                try:
                    air = oad.date().isoformat() if hasattr(oad, "date") else oad.isoformat()
                except Exception:
                    air = None
            movie_summary = plex.MovieSummary(
                rating_key=str(m_item.ratingKey),
                title=m_item.title or "",
                year=getattr(m_item, "year", None),
                thumb=getattr(m_item, "thumb", None),
                air_date=air,
                view_count=int(getattr(m_item, "viewCount", 0) or 0),
            )
            if unwatched_only and movie_summary.view_count > 0:
                continue
            movie_refs.append(plex.movie_as_episode_ref(movie_summary, cfg.rating_key, cfg.title or ""))
        # Sort movies by air date so they slot into the show's chronology sensibly
        movie_refs.sort(key=lambda m: (m.air_date or "9999-99-99", m.title.lower()))
        eps = eps + movie_refs
    return eps


def _config_from_row(row) -> ShowConfig:
    raw_keys = row["movie_rating_keys"] if "movie_rating_keys" in row.keys() else ""
    keys = [k for k in (raw_keys or "").split(",") if k]
    return ShowConfig(
        rating_key=row["show_rating_key"],
        title=row["show_title"],
        thumb=row["show_thumb"],
        start_season=int(row["start_season"] or 1),
        end_season=row["end_season"],
        include_specials=bool(row["include_specials"]),
        include_movies=bool(row["include_movies"]) if "include_movies" in row.keys() else False,
        movie_rating_keys=keys,
    )


# --------------------------------------------------------------------------- #
# Preview
# --------------------------------------------------------------------------- #


def preview_playlist(
    configs: list[ShowConfig],
    limit: int = 2000,
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
) -> list[dict]:
    """Compute the first `limit` episodes of the resulting playlist
    without touching Plex. Used by the configure UI."""
    _hydrate_configs(configs)
    shows_eps = [_episodes_for_config(c, unwatched_only=unwatched_only) for c in configs]
    show_order = [c.rating_key for c in configs]
    composed = rotation.compose(shows_eps, mode=sort_mode, show_order=show_order)
    out: list[dict] = []
    for ep in composed[:limit]:
        out.append(
            {
                "show": ep.show_title,
                "season": ep.season,
                "episode": ep.episode,
                "title": ep.title,
                "air_date": ep.air_date,
                "is_special": ep.season == 0,
            }
        )
    return out


# --------------------------------------------------------------------------- #
# View models
# --------------------------------------------------------------------------- #


@dataclass
class PlaylistView:
    id: int
    name: str
    plex_rating_key: str | None
    shows: list[dict]
    item_count: int
    sort_mode: str = "rotation"
    unwatched_only: bool = False


# --------------------------------------------------------------------------- #
# Create
# --------------------------------------------------------------------------- #


def create_managed_playlist(
    name: str,
    configs: list[ShowConfig],
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
) -> int:
    if not configs:
        raise ValueError("Need at least one show to create a playlist")
    if sort_mode not in ("rotation", "air_date"):
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")

    _hydrate_configs(configs)
    shows_episodes = [_episodes_for_config(c, unwatched_only=unwatched_only) for c in configs]
    show_order = [c.rating_key for c in configs]
    composed = rotation.compose(shows_episodes, mode=sort_mode, show_order=show_order)
    if not composed:
        raise ValueError("Selected shows have no episodes for the chosen ranges")

    episode_items = plex.fetch_episode_items([e.rating_key for e in composed])
    plex_playlist = plex.create_playlist(name, episode_items)

    playlist_id = db.create_playlist(name, sort_mode=sort_mode, unwatched_only=unwatched_only)
    db.set_plex_rating_key(playlist_id, str(plex_playlist.ratingKey))

    db.add_shows(
        playlist_id,
        [
            {
                "rating_key": c.rating_key,
                "title": c.title,
                "thumb": c.thumb,
                "start_season": c.start_season,
                "end_season": c.end_season,
                "include_specials": c.include_specials,
                "include_movies": c.include_movies,
                "movie_rating_keys": c.movie_rating_keys,
            }
            for c in configs
        ],
    )
    log.info("Created playlist '%s' (%d items)", name, len(episode_items))
    return playlist_id


# --------------------------------------------------------------------------- #
# Add shows mid-rotation
# --------------------------------------------------------------------------- #


def add_shows_to_playlist(playlist_id: int, new_configs: list[ShowConfig]) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    plex_pl = plex.get_playlist(row["plex_rating_key"])
    if plex_pl is None:
        raise RuntimeError(f"Plex playlist for '{row['name']}' is missing")

    existing_rows = db.list_shows(playlist_id)
    existing_keys = {s["show_rating_key"] for s in existing_rows}
    new_configs = [c for c in new_configs if c.rating_key not in existing_keys]
    if not new_configs:
        return
    _hydrate_configs(new_configs)

    items = _playlist_items_for_rotation(plex_pl)
    splice = rotation.splice_index(items)
    kept = items[:splice]

    # Episodes per show (existing first preserving order, then new shows appended).
    full_configs: list[ShowConfig] = [_config_from_row(r) for r in existing_rows]
    full_configs.extend(new_configs)
    uw = bool(row["unwatched_only"])
    shows_episodes = [_episodes_for_config(c, unwatched_only=uw) for c in full_configs]
    show_order = [c.rating_key for c in full_configs]
    new_tail = rotation.rebuild_tail(
        kept, shows_episodes, mode=row["sort_mode"], show_order=show_order
    )

    current_tail_items = items[splice:]
    current_tail_keys = {it.rating_key for it in current_tail_items}
    new_tail_keys = [e.rating_key for e in new_tail]

    to_remove_keys = current_tail_keys - set(new_tail_keys)
    to_remove_episodes = plex.fetch_episode_items(list(to_remove_keys))
    plex.remove_items(plex_pl, to_remove_episodes)

    kept_keys = {it.rating_key for it in kept}
    to_add_keys = [k for k in new_tail_keys if k not in kept_keys]
    to_add_episodes = plex.fetch_episode_items(to_add_keys)
    plex.add_items(plex_pl, to_add_episodes)

    db.add_shows(
        playlist_id,
        [
            {
                "rating_key": c.rating_key,
                "title": c.title,
                "thumb": c.thumb,
                "start_season": c.start_season,
                "end_season": c.end_season,
                "include_specials": c.include_specials,
                "include_movies": c.include_movies,
                "movie_rating_keys": c.movie_rating_keys,
            }
            for c in new_configs
        ],
    )
    log.info(
        "Added %d show(s) to '%s'; removed %d tail items, added %d",
        len(new_configs), row["name"], len(to_remove_episodes), len(to_add_episodes),
    )


# --------------------------------------------------------------------------- #
# Remove a show entirely
# --------------------------------------------------------------------------- #


def remove_show_from_playlist(playlist_id: int, show_rating_key: str) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    plex_pl = plex.get_playlist(row["plex_rating_key"])
    if plex_pl is None:
        raise RuntimeError(f"Plex playlist for '{row['name']}' is missing")

    items = _playlist_items_for_rotation(plex_pl)
    to_remove_keys = [it.rating_key for it in items if it.show_rating_key == show_rating_key]
    to_remove_eps = plex.fetch_episode_items(to_remove_keys)
    plex.remove_items(plex_pl, to_remove_eps)

    db.remove_show(playlist_id, show_rating_key)
    log.info("Removed show %s from '%s' (%d items)", show_rating_key, row["name"], len(to_remove_eps))


# --------------------------------------------------------------------------- #
# Reorder rotation
# --------------------------------------------------------------------------- #


def reorder_shows(playlist_id: int, ordered_keys: list[str]) -> None:
    """Change the rotation order. Already-played items stay; the future portion
    of the playlist is rebuilt to follow the new order."""
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    plex_pl = plex.get_playlist(row["plex_rating_key"])
    if plex_pl is None:
        raise RuntimeError(f"Plex playlist for '{row['name']}' is missing")

    existing = {s["show_rating_key"]: s for s in db.list_shows(playlist_id)}
    if set(ordered_keys) != set(existing.keys()):
        raise ValueError("Reorder keys don't match the current show set")

    db.set_positions(playlist_id, ordered_keys)

    items = _playlist_items_for_rotation(plex_pl)
    splice = rotation.splice_index(items)
    kept = items[:splice]

    new_rows = db.list_shows(playlist_id)
    full_configs = [_config_from_row(r) for r in new_rows]
    uw = bool(row["unwatched_only"])
    shows_episodes = [_episodes_for_config(c, unwatched_only=uw) for c in full_configs]
    show_order = [c.rating_key for c in full_configs]
    new_tail = rotation.rebuild_tail(
        kept, shows_episodes, mode=row["sort_mode"], show_order=show_order
    )

    current_tail_keys = {it.rating_key for it in items[splice:]}
    new_tail_keys = [e.rating_key for e in new_tail]
    to_remove = current_tail_keys - set(new_tail_keys)
    plex.remove_items(plex_pl, plex.fetch_episode_items(list(to_remove)))

    kept_keys = {it.rating_key for it in kept}
    to_add = [k for k in new_tail_keys if k not in kept_keys]
    plex.add_items(plex_pl, plex.fetch_episode_items(to_add))
    log.info("Reordered '%s'", row["name"])


# --------------------------------------------------------------------------- #
# Change sort mode
# --------------------------------------------------------------------------- #


def set_playlist_sort_mode(playlist_id: int, sort_mode: str) -> None:
    """Switch a managed playlist between rotation and air_date sort.

    Already-played items stay; the future portion is rebuilt to match the
    new mode.
    """
    if sort_mode not in ("rotation", "air_date"):
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    if row["sort_mode"] == sort_mode:
        return
    plex_pl = plex.get_playlist(row["plex_rating_key"])
    if plex_pl is None:
        db.set_sort_mode(playlist_id, sort_mode)
        return

    db.set_sort_mode(playlist_id, sort_mode)

    items = _playlist_items_for_rotation(plex_pl)
    splice = rotation.splice_index(items)
    kept = items[:splice]

    show_rows = db.list_shows(playlist_id)
    full_configs = [_config_from_row(r) for r in show_rows]
    uw = bool(row["unwatched_only"])
    shows_episodes = [_episodes_for_config(c, unwatched_only=uw) for c in full_configs]
    show_order = [c.rating_key for c in full_configs]
    new_tail = rotation.rebuild_tail(
        kept, shows_episodes, mode=sort_mode, show_order=show_order
    )

    current_tail_keys = {it.rating_key for it in items[splice:]}
    new_tail_keys = [e.rating_key for e in new_tail]
    to_remove = current_tail_keys - set(new_tail_keys)
    plex.remove_items(plex_pl, plex.fetch_episode_items(list(to_remove)))

    kept_keys = {it.rating_key for it in kept}
    to_add = [k for k in new_tail_keys if k not in kept_keys]
    plex.add_items(plex_pl, plex.fetch_episode_items(to_add))
    log.info("Switched '%s' to sort_mode=%s", row["name"], sort_mode)


# --------------------------------------------------------------------------- #
# Toggle unwatched-only filter
# --------------------------------------------------------------------------- #


def set_playlist_unwatched_only(playlist_id: int, unwatched_only: bool) -> None:
    """Enable or disable the 'only unwatched' filter on a managed playlist.

    Kept (watched/in-progress) portion stays as a history; the future portion
    is rebuilt under the new filter. When turning the filter ON, the new tail
    will exclude any episode the user has watched outside the playlist too.
    """
    row = db.get_playlist(playlist_id)
    if not row:
        raise ValueError(f"No managed playlist with id={playlist_id}")
    if bool(row["unwatched_only"]) == unwatched_only:
        return
    plex_pl = plex.get_playlist(row["plex_rating_key"])
    if plex_pl is None:
        db.set_unwatched_only(playlist_id, unwatched_only)
        return

    db.set_unwatched_only(playlist_id, unwatched_only)

    items = _playlist_items_for_rotation(plex_pl)
    splice = rotation.splice_index(items)
    kept = items[:splice]

    show_rows = db.list_shows(playlist_id)
    full_configs = [_config_from_row(r) for r in show_rows]
    shows_episodes = [_episodes_for_config(c, unwatched_only=unwatched_only) for c in full_configs]
    show_order = [c.rating_key for c in full_configs]
    new_tail = rotation.rebuild_tail(
        kept, shows_episodes, mode=row["sort_mode"], show_order=show_order
    )

    current_tail_keys = {it.rating_key for it in items[splice:]}
    new_tail_keys = [e.rating_key for e in new_tail]
    to_remove = current_tail_keys - set(new_tail_keys)
    plex.remove_items(plex_pl, plex.fetch_episode_items(list(to_remove)))

    kept_keys = {it.rating_key for it in kept}
    to_add = [k for k in new_tail_keys if k not in kept_keys]
    plex.add_items(plex_pl, plex.fetch_episode_items(to_add))
    log.info("Switched '%s' unwatched_only=%s", row["name"], unwatched_only)


# --------------------------------------------------------------------------- #
# Delete a managed playlist entirely
# --------------------------------------------------------------------------- #


def delete_managed_playlist(playlist_id: int) -> None:
    row = db.get_playlist(playlist_id)
    if not row:
        return
    plex_pl = plex.get_playlist(row["plex_rating_key"])
    if plex_pl is not None:
        try:
            plex_pl.delete()
        except Exception:
            log.warning("Failed to delete Plex playlist for '%s' (continuing)", row["name"])
    db.delete_playlist(playlist_id)
    log.info("Deleted managed playlist '%s'", row["name"])


# --------------------------------------------------------------------------- #
# Prune watched items (scheduler entry point)
# --------------------------------------------------------------------------- #


def prune_playlist(playlist_id: int, keep_last_n: int | None = None) -> int:
    if keep_last_n is None:
        keep_last_n = _watched_keep()

    row = db.get_playlist(playlist_id)
    if not row:
        return 0
    plex_pl = plex.get_playlist(row["plex_rating_key"])
    if plex_pl is None:
        return 0

    items = _playlist_items_for_rotation(plex_pl)
    indices = rotation.prune_indices(items, keep_last_n)
    if not indices:
        return 0
    remove_keys = [items[i].rating_key for i in indices]
    eps = plex.fetch_episode_items(remove_keys)
    plex.remove_items(plex_pl, eps)
    log.info("Pruned %d watched item(s) from '%s'", len(eps), row["name"])
    return len(eps)


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
    shows = [dict(s) for s in db.list_shows(playlist_id)]
    item_count = 0
    plex_pl = plex.get_playlist(row["plex_rating_key"])
    if plex_pl is not None:
        try:
            item_count = len(plex_pl.items())
        except Exception:
            item_count = 0
    return PlaylistView(
        id=row["id"],
        name=row["name"],
        plex_rating_key=row["plex_rating_key"],
        shows=shows,
        item_count=item_count,
        sort_mode=row["sort_mode"] or "rotation",
        unwatched_only=bool(row["unwatched_only"]),
    )


def list_playlist_views() -> list[PlaylistView]:
    out: list[PlaylistView] = []
    for row in db.list_playlists():
        view = get_playlist_view(row["id"])
        if view:
            out.append(view)
    return out
