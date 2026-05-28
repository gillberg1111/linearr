"""Trakt.tv API client for franchise watch-order list fetching.

Read-only. Uses a bundled application Client ID — no user OAuth required.
Override with TRAKT_CLIENT_ID env var for custom deployments.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os

import requests

log = logging.getLogger(__name__)

# Linearr's registered Trakt application Client ID.
# Public by design — it identifies the app, not a user.
# Override with TRAKT_CLIENT_ID env var if needed.
TRAKT_CLIENT_ID = "5892e921d0b58f40f017422a546ce91638dec39419daaf1db57064c317385c19"
TRAKT_BASE_URL = "https://api.trakt.tv"


class TraktClient:
    def __init__(self) -> None:
        self._client_id = os.environ.get("TRAKT_CLIENT_ID", TRAKT_CLIENT_ID)
        self._session = requests.Session()
        self._session.headers.update({
            "Content-Type": "application/json",
            "trakt-api-version": "2",
            "trakt-api-key": self._client_id,
        })

    def fetch_list_items(self, trakt_user: str, trakt_slug: str) -> list[dict]:
        """Fetch all items from a public Trakt list. Returns list sorted by rank.

        Handles Trakt pagination automatically (default page size is 100).
        Raises requests.HTTPError on non-2xx responses (caller handles).
        """
        url = f"{TRAKT_BASE_URL}/users/{trakt_user}/lists/{trakt_slug}/items"
        raw_items: list[dict] = []
        page = 1
        while True:
            resp = self._session.get(
                url,
                params={"extended": "full", "limit": 1000, "page": page},
                timeout=15,
            )
            resp.raise_for_status()
            page_items = resp.json()
            if not page_items:
                break
            raw_items.extend(page_items)
            page_count = int(resp.headers.get("X-Pagination-Page-Count", 1))
            if page >= page_count:
                break
            page += 1
        parsed = [self._parse_item(item) for item in raw_items]
        items = [p for p in parsed if p is not None]
        return sorted(items, key=lambda x: x["rank"])

    def content_hash(self, items: list[dict]) -> str:
        """SHA-256 of the normalized item list. Used to detect list changes."""
        canonical = json.dumps(items, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode()).hexdigest()

    def _parse_item(self, raw: dict) -> dict | None:
        """Normalize one raw Trakt list item into a canonical dict.

        Returns None for unknown types (skip silently).

        Canonical shape (all fields always present, None when not applicable):
          rank          int    — position in the list (1-based)
          item_type     str    — 'movie' | 'episode' | 'show' | 'season'
          title         str    — display title
          year          int|None
          tmdb_id       int|None
          tvdb_id       int|None  — for movies: None; for episodes: episode-level TVDB id
          imdb_id       str|None
          season_number int|None  — for episode/season items
          episode_number int|None — for episode items only
          show_title    str|None  — for episode/season items: parent show title
          show_tvdb_id  int|None  — for episode/season items: parent show TVDB id
        """
        item_type = raw.get("type")
        rank = raw.get("rank", 0)

        if item_type == "movie":
            m = raw["movie"]
            ids = m.get("ids", {})
            return {
                "rank": rank,
                "item_type": "movie",
                "title": m["title"],
                "year": m.get("year"),
                "tmdb_id": ids.get("tmdb"),
                "tvdb_id": None,
                "imdb_id": ids.get("imdb"),
                "season_number": None,
                "episode_number": None,
                "show_title": None,
                "show_tvdb_id": None,
            }

        elif item_type == "episode":
            ep = raw["episode"]
            show = raw["show"]
            ep_ids = ep.get("ids", {})
            show_ids = show.get("ids", {})
            return {
                "rank": rank,
                "item_type": "episode",
                "title": ep.get("title") or f"S{ep['season']:02d}E{ep['number']:02d}",
                "year": show.get("year"),
                "tmdb_id": None,
                "tvdb_id": ep_ids.get("tvdb"),
                "imdb_id": None,
                "season_number": ep["season"],
                "episode_number": ep["number"],
                "show_title": show["title"],
                "show_tvdb_id": show_ids.get("tvdb"),
            }

        elif item_type == "show":
            s = raw["show"]
            ids = s.get("ids", {})
            return {
                "rank": rank,
                "item_type": "show",
                "title": s["title"],
                "year": s.get("year"),
                "tmdb_id": ids.get("tmdb"),
                "tvdb_id": ids.get("tvdb"),
                "imdb_id": None,
                "season_number": None,
                "episode_number": None,
                "show_title": None,
                "show_tvdb_id": None,
            }

        elif item_type == "season":
            s = raw["show"]
            season = raw["season"]
            show_ids = s.get("ids", {})
            return {
                "rank": rank,
                "item_type": "season",
                "title": f"{s['title']} — Season {season['number']}",
                "year": s.get("year"),
                "tmdb_id": None,
                "tvdb_id": show_ids.get("tvdb"),
                "imdb_id": None,
                "season_number": season["number"],
                "episode_number": None,
                "show_title": s["title"],
                "show_tvdb_id": show_ids.get("tvdb"),
            }

        log.debug("Skipping unknown Trakt item type: %s", item_type)
        return None


# Module-level singleton
_client: TraktClient | None = None


def get_trakt_client() -> TraktClient:
    global _client
    if _client is None:
        _client = TraktClient()
    return _client
