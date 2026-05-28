"""TMDB API client for the Franchise Maker.

Supports both v3 API Key and v4 Read Access Token (JWT). Auto-detects by
checking if the token starts with 'eyJ'.

The key is read from db.get_setting('tmdb_api_key'); if absent, falls back
to env var TMDB_API_KEY. The Maker checks for availability at request
time and shows a prompt linking to /settings if missing.
"""

from __future__ import annotations

import logging
import os

import requests

import db

log = logging.getLogger(__name__)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_IMAGE_BASE = "https://image.tmdb.org/t/p/w92"


def get_tmdb_key() -> str:
    key = (db.get_setting("tmdb_api_key") or "").strip()
    if not key:
        key = os.environ.get("TMDB_API_KEY", "").strip()
    return key


def _tmdb_get(path: str, **params) -> dict:
    key = get_tmdb_key()
    if not key:
        raise ValueError("TMDB API key is not configured")

    if key.startswith("eyJ"):
        resp = requests.get(
            f"{TMDB_BASE}{path}",
            params=params,
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
            timeout=10,
        )
    else:
        resp = requests.get(
            f"{TMDB_BASE}{path}",
            params={"api_key": key, **params},
            timeout=10,
        )
    resp.raise_for_status()
    return resp.json()


def search(query: str, media_type: str = "movie") -> list[dict]:
    if not query.strip():
        return []
    path = "/search/movie" if media_type == "movie" else "/search/tv"
    data = _tmdb_get(path, query=query, include_adult=False)
    out = []
    for r in data.get("results", [])[:10]:
        if media_type == "movie":
            out.append({
                "tmdb_id": r["id"],
                "title": r.get("title", ""),
                "year": (r.get("release_date") or "")[:4] or None,
                "poster": (TMDB_IMAGE_BASE + r["poster_path"]) if r.get("poster_path") else None,
                "type": "movie",
            })
        else:
            out.append({
                "tmdb_id": r["id"],
                "title": r.get("name", ""),
                "year": (r.get("first_air_date") or "")[:4] or None,
                "poster": (TMDB_IMAGE_BASE + r["poster_path"]) if r.get("poster_path") else None,
                "type": "tv",
            })
    return out


def get_movie(tmdb_id: int) -> dict:
    data = _tmdb_get(f"/movie/{tmdb_id}")
    return {
        "tmdb_id": data["id"],
        "title": data.get("title", ""),
        "year": (data.get("release_date") or "")[:4] or None,
        "imdb_id": data.get("imdb_id"),
        "poster": (TMDB_IMAGE_BASE + data["poster_path"]) if data.get("poster_path") else None,
    }


def get_tv(tmdb_id: int) -> dict:
    data = _tmdb_get(f"/tv/{tmdb_id}", append_to_response="external_ids")
    ext = data.get("external_ids", {})
    return {
        "tmdb_id": data["id"],
        "title": data.get("name", ""),
        "year": (data.get("first_air_date") or "")[:4] or None,
        "tvdb_id": ext.get("tvdb_id"),
        "poster": (TMDB_IMAGE_BASE + data["poster_path"]) if data.get("poster_path") else None,
        "seasons": [
            {
                "season_number": s["season_number"],
                "name": s.get("name", f"Season {s['season_number']}"),
                "episode_count": s.get("episode_count", 0),
                "air_date": s.get("air_date"),
            }
            for s in data.get("seasons", [])
            if s["season_number"] > 0
        ],
    }


def get_season(tmdb_id: int, season_number: int) -> dict:
    show = get_tv(tmdb_id)
    data = _tmdb_get(f"/tv/{tmdb_id}/season/{season_number}")
    episodes = [
        {
            "season_number": ep["season_number"],
            "episode_number": ep["episode_number"],
            "title": ep.get("name", ""),
            "air_date": ep.get("air_date"),
            "overview": (ep.get("overview") or "")[:120],
            "show_title": show["title"],
            "show_tvdb_id": show["tvdb_id"],
        }
        for ep in data.get("episodes", [])
    ]
    return {
        "season_number": season_number,
        "show_title": show["title"],
        "show_tvdb_id": show["tvdb_id"],
        "episodes": episodes,
    }
