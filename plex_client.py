"""Thin wrapper around python-plexapi for the operations this app needs.

============================================================================
SAFETY GUARANTEE — this app NEVER deletes media files from Plex.
============================================================================
The only Plex destructive operations this app performs are:
  * Playlist.delete()      — removes the playlist (metadata only)
  * Playlist.removeItem()  — removes an item FROM a playlist (does NOT touch
                             the underlying Episode/Show or its file on disk)

Below, on import, we monkey-patch Episode/Show/Season/Movie.delete() to raise
RuntimeError. Even if a future bug accidentally tries to delete library items
or media files, it will fail loudly instead of removing anything.
============================================================================
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, date
from functools import lru_cache

from plexapi.exceptions import NotFound
from plexapi.playlist import Playlist
from plexapi.server import PlexServer
from plexapi.video import Episode, Movie, Season, Show

_log = logging.getLogger(__name__)


def _refuse_delete(self, *_args, **_kwargs):  # pragma: no cover - safety guard
    cls = type(self).__name__
    name = getattr(self, "title", repr(self))
    raise RuntimeError(
        f"Refused {cls}.delete() on '{name}'. This app is configured to never "
        f"delete media files or library items from Plex."
    )


# Disable file/item destructive operations the moment this module loads.
# Playlist.delete is intentionally left intact (playlists are metadata only).
for _cls in (Episode, Show, Season, Movie):
    _cls.delete = _refuse_delete  # type: ignore[method-assign]
_log.info("Plex safety guard installed: Episode/Show/Season/Movie.delete() disabled")


@dataclass
class ShowSummary:
    rating_key: str
    title: str
    year: int | None
    library: str
    thumb: str | None  # Plex thumb path, e.g. "/library/metadata/123/thumb/1700000000"


@dataclass
class SeasonSummary:
    index: int  # season number (0 = specials)
    title: str
    episode_count: int
    thumb: str | None
    year: int | None


@dataclass
class EpisodeRef:
    rating_key: str
    show_rating_key: str
    show_title: str
    season: int
    episode: int
    title: str
    view_count: int
    view_offset_ms: int
    air_date: str | None = None  # ISO YYYY-MM-DD (originallyAvailableAt)
    kind: str = "episode"  # 'episode' or 'movie' (when attached to a show)


@dataclass
class MovieSummary:
    rating_key: str
    title: str
    year: int | None
    thumb: str | None
    air_date: str | None
    view_count: int


@lru_cache(maxsize=1)
def server() -> PlexServer:
    url = os.environ["PLEX_URL"]
    token = os.environ["PLEX_TOKEN"]
    return PlexServer(url, token)


def _tv_sections():
    libs = [s.strip() for s in os.environ.get("TV_LIBRARIES", "").split(",") if s.strip()]
    sections = [s for s in server().library.sections() if s.type == "show"]
    if libs:
        sections = [s for s in sections if s.title in libs]
    return sections


def list_all_shows() -> list[ShowSummary]:
    out: list[ShowSummary] = []
    for section in _tv_sections():
        for show in section.all():
            out.append(
                ShowSummary(
                    rating_key=str(show.ratingKey),
                    title=show.title,
                    year=show.year,
                    library=section.title,
                    thumb=getattr(show, "thumb", None),
                )
            )
    out.sort(key=lambda s: s.title.lower())
    return out


def get_show(rating_key: str) -> Show:
    return server().fetchItem(int(rating_key))


def get_show_summary(rating_key: str) -> ShowSummary:
    show = get_show(rating_key)
    return ShowSummary(
        rating_key=str(show.ratingKey),
        title=show.title,
        year=show.year,
        library=show.librarySectionTitle if hasattr(show, "librarySectionTitle") else "",
        thumb=getattr(show, "thumb", None),
    )


def season_summaries(rating_key: str) -> list[SeasonSummary]:
    """All seasons for a show, including S0 if it has any episodes."""
    show = get_show(rating_key)
    out: list[SeasonSummary] = []
    for season in show.seasons():
        idx = int(getattr(season, "index", 0) or 0)
        count = int(getattr(season, "leafCount", 0) or 0)
        if count == 0:
            try:
                count = len(season.episodes())
            except Exception:
                count = 0
        if count == 0:
            continue
        out.append(
            SeasonSummary(
                index=idx,
                title=season.title or (f"Season {idx}" if idx > 0 else "Specials"),
                episode_count=count,
                thumb=getattr(season, "thumb", None) or getattr(show, "thumb", None),
                year=getattr(season, "year", None),
            )
        )
    out.sort(key=lambda s: s.index)
    return out


def _air_date(ep) -> datetime | None:
    val = getattr(ep, "originallyAvailableAt", None)
    if isinstance(val, datetime):
        return val
    if isinstance(val, date):
        return datetime(val.year, val.month, val.day)
    return None


def episodes_for_show(
    rating_key: str,
    start_season: int = 1,
    end_season: int | None = None,
    include_specials: bool = False,
) -> list[EpisodeRef]:
    """Episodes for a show in playback order.

    Regular episodes: sorted by (season, episode), filtered by [start_season, end_season].
    Specials (S0): included only when include_specials=True. Placed by air date
    immediately after the latest regular episode that aired on or before each
    special. Specials without an air date are placed at the very beginning.
    """
    show = get_show(rating_key)

    regulars = []
    specials = []
    for ep in show.episodes():
        season = int(ep.seasonNumber or 0)
        if season == 0:
            if include_specials:
                specials.append(ep)
            continue
        if season < start_season:
            continue
        if end_season is not None and season > end_season:
            continue
        regulars.append(ep)

    regulars.sort(key=lambda e: (int(e.seasonNumber or 0), int(e.index or 0)))

    # Build the merged order. Use float keys to slot specials between regulars.
    ordered_with_keys: list[tuple[float, int, object]] = []
    for i, ep in enumerate(regulars):
        ordered_with_keys.append((float(i), 0, ep))

    if specials:
        reg_dates = [_air_date(r) for r in regulars]
        for sp in specials:
            sp_date = _air_date(sp)
            if sp_date is None or not reg_dates:
                slot = -1.0  # at the very start
            else:
                slot = -1.0
                for i, rd in enumerate(reg_dates):
                    if rd is not None and rd <= sp_date:
                        slot = float(i)
                # slot is the index of the latest regular before/equal to sp's air date
                slot += 0.5  # between regulars[slot] and regulars[slot+1]
            sp_secondary = int((sp_date or datetime.min).timestamp()) if sp_date else 0
            ordered_with_keys.append((slot, sp_secondary, sp))

    ordered_with_keys.sort(key=lambda t: (t[0], t[1]))

    out: list[EpisodeRef] = []
    for _, _, ep in ordered_with_keys:
        ad = _air_date(ep)
        out.append(
            EpisodeRef(
                rating_key=str(ep.ratingKey),
                show_rating_key=str(show.ratingKey),
                show_title=show.title,
                season=int(ep.seasonNumber or 0),
                episode=int(ep.index or 0),
                title=ep.title or "",
                view_count=int(ep.viewCount or 0),
                view_offset_ms=int(ep.viewOffset or 0),
                air_date=ad.date().isoformat() if ad else None,
            )
        )
    return out


def fetch_episode_items(rating_keys: list[str]) -> list[Episode]:
    """Resolve a list of episode ratingKeys back to Plex Episode objects, in input order."""
    if not rating_keys:
        return []
    items = []
    for rk in rating_keys:
        try:
            items.append(server().fetchItem(int(rk)))
        except NotFound:
            continue
    return items


def get_playlist(rating_key: str | None) -> Playlist | None:
    if not rating_key:
        return None
    try:
        item = server().fetchItem(int(rating_key))
        return item if isinstance(item, Playlist) else None
    except NotFound:
        return None


def find_playlist_by_title(title: str) -> Playlist | None:
    for pl in server().playlists():
        if pl.title == title:
            return pl
    return None


def create_playlist(title: str, episodes: list[Episode]) -> Playlist:
    """Create a Plex video playlist. Plex requires at least one item to create."""
    if not episodes:
        raise ValueError("Cannot create a Plex playlist with zero items")
    return server().createPlaylist(title, items=episodes)


def playlist_items_in_order(playlist: Playlist) -> list[Episode]:
    return list(playlist.items())


def add_items(playlist: Playlist, episodes: list[Episode]) -> None:
    if episodes:
        playlist.addItems(episodes)


def remove_items(playlist: Playlist, episodes: list[Episode]) -> None:
    for ep in episodes:
        try:
            playlist.removeItem(ep)
        except Exception:
            # If a single removal fails (e.g. item already gone), keep going.
            continue


def _movie_sections():
    sections = [s for s in server().library.sections() if s.type == "movie"]
    return sections


import re as _re


def _title_match(movie_title: str, show_title: str) -> bool:
    """Word-boundary match for movie titles against a show name.

    Matches 'Psych: The Movie', 'Mr. Monk's Last Case: A Monk Movie',
    'Psych 3: This is Gus', etc. Doesn't match 'Psychic Detective' (no boundary).
    """
    if not movie_title or not show_title:
        return False
    pattern = r"\b" + _re.escape(show_title.lower()) + r"\b"
    return bool(_re.search(pattern, movie_title.lower()))


def find_associated_movies(show_title: str) -> list[MovieSummary]:
    """Return movies whose title contains the show's name as a word.

    Searches every movie library on the server. Caller can deselect false
    positives in the UI.
    """
    out: list[MovieSummary] = []
    seen: set[str] = set()
    for section in _movie_sections():
        try:
            results = section.search(title=show_title)
        except Exception:
            results = []
        for m in results:
            rk = str(getattr(m, "ratingKey", "") or "")
            if not rk or rk in seen:
                continue
            if not _title_match(getattr(m, "title", "") or "", show_title):
                continue
            oad = getattr(m, "originallyAvailableAt", None)
            air = None
            if oad is not None:
                try:
                    air = oad.date().isoformat() if hasattr(oad, "date") else oad.isoformat()
                except Exception:
                    air = None
            out.append(
                MovieSummary(
                    rating_key=rk,
                    title=m.title or "",
                    year=getattr(m, "year", None),
                    thumb=getattr(m, "thumb", None),
                    air_date=air,
                    view_count=int(getattr(m, "viewCount", 0) or 0),
                )
            )
            seen.add(rk)
    out.sort(key=lambda x: (x.air_date or "", x.title.lower()))
    return out


def movie_as_episode_ref(movie: MovieSummary, show_rating_key: str, show_title: str) -> EpisodeRef:
    """Wrap a movie as an EpisodeRef so the rotation logic can handle it
    alongside episodes. Movies always go to season=999 so they sort after
    every regular season in rotation mode."""
    return EpisodeRef(
        rating_key=movie.rating_key,
        show_rating_key=show_rating_key,
        show_title=show_title,
        season=999,
        episode=1,  # placeholder; movies are identified by rating_key
        title=movie.title,
        view_count=movie.view_count,
        view_offset_ms=0,
        air_date=movie.air_date,
        kind="movie",
    )


def fetch_image(path: str, width: int | None = None, height: int | None = None) -> tuple[bytes, str]:
    """Fetch an image from Plex by path (e.g. /library/metadata/123/thumb/123).

    When width/height are given, asks Plex to transcode the image to that size,
    which is much faster to render in a poster grid.

    Returns (bytes, content_type).
    """
    srv = server()
    if width and height:
        full_path = srv.url(path)
        url = srv.transcodeImage(full_path, height, width)
    else:
        url = srv.url(path)
    resp = srv._session.get(url, timeout=10)
    resp.raise_for_status()
    return resp.content, resp.headers.get("Content-Type", "image/jpeg")
