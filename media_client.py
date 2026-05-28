"""Backend-agnostic interface for media servers (Plex, Jellyfin, ...).

This module defines:

  - The `MediaClient` abstract base class. Each backend (Plex, Jellyfin)
    implements it. `service.py` and `app.py` only talk to media servers
    through this interface — never directly to a specific backend.

  - The shared dataclasses (`ShowSummary`, `SeasonSummary`, `EpisodeRef`,
    `MovieSummary`) that all backends return. Field names use the legacy
    Plex term `rating_key`, but in this interface they're treated as
    opaque backend item IDs — they work for Jellyfin GUIDs just as well.

  - A `get_client(backend)` factory for picking a backend by name.
    Currently hard-wired to Plex; in v1.1.0 it will dispatch on env vars.

The `rotation.py` module is and remains pure: it doesn't import from here
and doesn't know which backend produced its inputs.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from functools import lru_cache

from rotation import PlaylistItem  # re-exported for callers; kept generic

__all__ = [
    "MediaClient",
    "ShowSummary",
    "SeasonSummary",
    "EpisodeRef",
    "MovieSummary",
    "PlaylistItem",
    "get_client",
]


# --------------------------------------------------------------------------- #
# Shared item shapes (backend-agnostic)
# --------------------------------------------------------------------------- #


@dataclass
class ShowSummary:
    rating_key: str
    title: str
    year: int | None
    library: str
    thumb: str | None  # backend-specific image reference (path for Plex, item-id for Jellyfin)
    tvdb_id: str | None = None  # TVDB numeric ID for cross-backend matching
    status: str | None = None
    content_rating: str | None = None
    season_count: int | None = None
    community_rating: float | None = None


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
    air_date: str | None = None  # ISO YYYY-MM-DD
    kind: str = "episode"  # 'episode' or 'movie' (when attached to a show)


@dataclass
class MovieSummary:
    rating_key: str
    title: str
    year: int | None
    thumb: str | None
    air_date: str | None
    view_count: int
    tmdb_id: int | None = None


# --------------------------------------------------------------------------- #
# Abstract client
# --------------------------------------------------------------------------- #


class MediaClient(ABC):
    """Contract every backend must satisfy. All identifiers are opaque strings."""

    backend: str  # subclasses set this to "plex" or "jellyfin"

    # ----- Library / show discovery -----------------------------------------

    @abstractmethod
    def list_all_shows(self) -> list[ShowSummary]:
        """Every TV show across configured libraries, sorted by title."""

    @abstractmethod
    def list_shows_by_genres(self, genres: list[str]) -> list[ShowSummary]:
        """Shows whose genre metadata matches at least one of the given genres
        (case-insensitive). Returns the union across libraries, deduplicated
        by id, sorted by title. Empty `genres` list returns []."""

    @abstractmethod
    def get_show_summary(self, rating_key: str) -> ShowSummary:
        """A single show by id."""

    @abstractmethod
    def season_summaries(self, rating_key: str) -> list[SeasonSummary]:
        """All seasons of a show that contain at least one episode."""

    @abstractmethod
    def episodes_for_show(
        self,
        rating_key: str,
        start_season: int = 1,
        end_season: int | None = None,
        include_specials: bool = False,
    ) -> list[EpisodeRef]:
        """Episodes for a show in playback order.

        Same contract for every backend:
        - Regular episodes sorted by (season, episode), filtered by [start, end]
        - Specials (season 0) included only when include_specials=True, placed
          chronologically by air date relative to the regular episodes.
        """

    # ----- Movies -----------------------------------------------------------

    @abstractmethod
    def list_all_genres(self) -> list[str]:
        """All genre tags present in the TV library, sorted alphabetically.

        Backends should query their native genre-list endpoint rather than
        iterating every show. Returns an empty list on error rather than
        raising — callers treat an empty list as "cache unavailable".
        Results are expected to be cached by the scheduler (see db.py
        get_genre_cache / set_genre_cache).
        """

    @abstractmethod
    def list_tv_sections(self) -> list[str]:
        """Return a list of TV library section names (lightweight health probe)."""

    @abstractmethod
    def find_associated_movies(self, show_title: str) -> list[MovieSummary]:
        """Movies whose title contains the show's name as a word boundary."""

    @abstractmethod
    def get_movie_summary(self, rating_key: str) -> MovieSummary | None:
        """A single movie by id, or None if not found."""

    def movie_as_episode_ref(
        self,
        movie: MovieSummary,
        show_rating_key: str,
        show_title: str,
    ) -> EpisodeRef:
        """Wrap a movie as an EpisodeRef so rotation.py can handle it
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

    # ----- Playlist read ----------------------------------------------------

    @abstractmethod
    def playlist_exists(self, rating_key: str | None) -> bool:
        """True if the playlist still exists on the backend."""

    @abstractmethod
    def get_playlist_items(self, rating_key: str) -> list[PlaylistItem]:
        """Playlist contents as generic PlaylistItem rows, in order.

        This is what rotation.py needs to make decisions about which items to
        keep, splice, or remove. Backend-specific quirks (Plex Episode vs
        Movie type detection, Jellyfin's PlaylistItemId vs Id distinction)
        are handled internally.
        """

    @abstractmethod
    def playlist_item_count(self, rating_key: str) -> int:
        """Cheap count of items in a playlist (for the index page)."""

    # ----- Playlist write ---------------------------------------------------

    @abstractmethod
    def create_playlist(self, title: str, ordered_rating_keys: list[str]) -> str:
        """Create a playlist with the given items in order. Returns the new
        playlist's id (the rating_key/Id on the backend)."""

    @abstractmethod
    def delete_playlist(self, rating_key: str) -> None:
        """Delete the playlist itself. Must NOT touch the underlying items."""

    @abstractmethod
    def add_items_to_playlist(
        self, rating_key: str, item_rating_keys: list[str]
    ) -> None:
        """Append items to the end of the playlist."""

    @abstractmethod
    def remove_items_from_playlist(
        self, rating_key: str, item_rating_keys: list[str]
    ) -> None:
        """Remove items from the playlist (does NOT delete the items themselves)."""

    def replace_playlist_items(
        self, rating_key: str, ordered_rating_keys: list[str]
    ) -> None:
        """Replace the playlist's entire contents with the given ordered list.

        Default impl: remove all current items, then add the new list. Backends
        with a native atomic-replace endpoint (Jellyfin's UpdatePlaylist) should
        override this with a single-call impl.
        """
        current = self.get_playlist_items(rating_key)
        if current:
            self.remove_items_from_playlist(
                rating_key, [it.rating_key for it in current]
            )
        if ordered_rating_keys:
            self.add_items_to_playlist(rating_key, ordered_rating_keys)

    # ----- Images -----------------------------------------------------------

    @abstractmethod
    def fetch_image(
        self,
        image_ref: str,
        width: int | None = None,
        height: int | None = None,
    ) -> tuple[bytes, str]:
        """Fetch a poster/thumb by the backend-specific reference returned in
        ShowSummary.thumb etc. Returns (bytes, content_type)."""

    @abstractmethod
    def list_all_movies(self) -> list[MovieSummary]:
        """All movies across configured movie libraries.

        `MovieSummary.tmdb_id` must be populated where available — it is used
        for franchise matching. Returns empty list on error rather than raising.
        """

    @abstractmethod
    def find_show_by_tvdb_id(self, tvdb_id: int) -> ShowSummary | None:
        """Find a show in the library by its TVDB numeric ID.

        Returns None if not found. Must NOT raise on miss.
        Implementations should use the existing list_all_shows() cache where
        possible rather than making a new API call per invocation.
        """

    @abstractmethod
    def refresh_show_metadata(self, rating_key: str) -> None:
        """Ask the backend to refresh this show's metadata from upstream sources.
        Fire-and-forget: raise on connection error, swallow 404 gracefully."""

    @abstractmethod
    def list_playlist_episodes(self, playlist_id: str) -> list:
        """Return raw episode objects (with view_count) for an existing playlist."""


# --------------------------------------------------------------------------- #
# Factory
# --------------------------------------------------------------------------- #


@lru_cache(maxsize=4)
def get_client(backend: str = "plex") -> MediaClient:
    """Return a singleton client for the named backend.

    Phase 4 will let callers ask for whichever backends are configured per
    playlist. For now, callers explicitly name "plex" or "jellyfin".
    """
    if backend == "plex":
        from plex_client import PlexClient  # local import to avoid cycle on cold paths

        return PlexClient()
    if backend == "jellyfin":
        from jellyfin_client import JellyfinClient  # local import; needs JELLYFIN_* env vars on first call

        return JellyfinClient()
    raise ValueError(f"Unknown backend: {backend!r}")


def available_backends() -> list[str]:
    """Names of backends with sufficient env vars to instantiate a client.

    Used by app.py to decide whether to show the backend picker. Does NOT
    instantiate the clients (no network calls) — only checks env vars.
    """
    import os as _os
    out: list[str] = []
    if _os.environ.get("PLEX_URL") and _os.environ.get("PLEX_TOKEN"):
        out.append("plex")
    if (
        _os.environ.get("JELLYFIN_URL")
        and _os.environ.get("JELLYFIN_USERNAME")
        and _os.environ.get("JELLYFIN_PASSWORD")
    ):
        out.append("jellyfin")
    return out


# --------------------------------------------------------------------------- #
# Cross-backend title matching (for "Both"-mode show bridging)
# --------------------------------------------------------------------------- #


def normalize_title(title: str | None) -> str:
    """Lowercase, strip punctuation, collapse whitespace.

    Also strips trailing disambiguation suffixes Plex appends but Jellyfin
    often omits — country codes like (US), (UK), (AU) and premiere years
    like (2018), or combinations in any order.  Two shows with the same
    stripped title and the same year are considered the same show; year
    disagreement (when both years are known) distinguishes e.g. US vs UK
    versions of the same franchise.
    """
    import re as _re
    if not title:
        return ""
    # Strip any trailing disambiguation suffixes before lowercasing.
    # Handles country codes (US), (UK), (AU), etc. and years (2018), in any
    # combination/order — e.g. "Whose Line Is It Anyway? (US)" → "Whose Line
    # Is It Anyway?" or "Yellowstone (2018)" → "Yellowstone".
    s = _re.sub(r'(\s*\(\d{4}\)|\s*\([A-Z]{2,3}\))+\s*$', '', title)
    s = _re.sub(r"[^\w\s]", " ", s.lower())
    s = _re.sub(r"\s+", " ", s).strip()
    return s


def titles_match(
    a: str | None,
    b: str | None,
    year_a: int | None = None,
    year_b: int | None = None,
) -> bool:
    """True if `a` and `b` normalize to the same string AND years agree (when
    both are known). Year mismatch with both known = not a match."""
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb or na != nb:
        return False
    if year_a is not None and year_b is not None and year_a != year_b:
        return False
    return True
