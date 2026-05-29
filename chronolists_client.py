"""Chronolists API client for franchise watch-order list fetching.

Read-only public API. No key required. Override base URL with
CHRONOLISTS_BASE_URL env var for self-hosted mirrors / testing.
"""

from __future__ import annotations
import logging
import os
import requests

log = logging.getLogger(__name__)
CHRONOLISTS_BASE_URL = "https://chronolists.com/api"


class ChronolistsClient:
    def __init__(self) -> None:
        self._base = os.environ.get("CHRONOLISTS_BASE_URL", CHRONOLISTS_BASE_URL).rstrip("/")
        self._session = requests.Session()
        self._session.headers.update({"Accept": "application/json"})

    def fetch_index(self) -> dict[str, dict]:
        resp = self._session.get(f"{self._base}/list", timeout=15)
        resp.raise_for_status()
        return resp.json().get("list", {}) or {}

    def list_hash(self, list_id: str) -> str | None:
        return (self.fetch_index().get(list_id) or {}).get("hash")

    def fetch_list_items(self, list_id: str) -> list[dict]:
        resp = self._session.get(f"{self._base}/list/{list_id}", timeout=15)
        resp.raise_for_status()
        payload = resp.json()
        return parse_chronolists_items(payload.get("items", []))

    def fetch_list(self, list_id: str) -> dict:
        resp = self._session.get(f"{self._base}/list/{list_id}", timeout=15)
        resp.raise_for_status()
        return resp.json()


def parse_chronolists_items(raw_items: list[dict]) -> list[dict]:
    out: list[dict] = []
    for i, raw in enumerate(raw_items):
        t = raw.get("type")
        if t == "movie":
            out.append({
                "rank": i + 1,
                "item_type": "movie",
                "title": raw["name"],
                "year": None,
                "tmdb_id": raw.get("tmdbId"),
                "tvdb_id": None,
                "imdb_id": raw.get("imdbId"),
                "season_number": None,
                "episode_number": None,
                "show_title": None,
                "show_tvdb_id": None,
                "show_tmdb_id": None,
            })
        elif t == "tv":
            out.append({
                "rank": i + 1,
                "item_type": "episode",
                "title": f'{raw["name"]} S{raw["season"]:02d}E{raw["episode"]:02d}',
                "year": None,
                "tmdb_id": None,
                "tvdb_id": None,
                "imdb_id": raw.get("imdbId"),
                "season_number": raw.get("season"),
                "episode_number": raw.get("episode"),
                "show_title": raw["name"],
                "show_tvdb_id": None,
                "show_tmdb_id": raw.get("tmdbId"),
            })
        else:
            log.debug("Skipping unknown Chronolists item type: %s", t)
    return out


_client: ChronolistsClient | None = None

def get_chronolists_client() -> ChronolistsClient:
    global _client
    if _client is None:
        _client = ChronolistsClient()
    return _client
