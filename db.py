"""SQLite persistence for managed rotating playlists.

Schema:
  managed_playlists
    id              INTEGER PK
    name            TEXT     — display name (and the Plex playlist title)
    plex_rating_key TEXT     — ratingKey of the Plex-side playlist (nullable until first sync)
    created_at      TEXT
  playlist_shows
    playlist_id      INTEGER FK -> managed_playlists.id
    show_rating_key  TEXT     — Plex ratingKey of the TV show
    show_title       TEXT     — cached for UI when Plex is unreachable
    show_thumb       TEXT     — cached thumb path
    position         INTEGER  — user-defined order in the rotation
    start_season     INTEGER  — lowest season to include (default 1)
    end_season       INTEGER  — highest season to include (NULL = no cap)
    include_specials INTEGER  — 0/1: include Season 0 in the rotation
    PRIMARY KEY (playlist_id, show_rating_key)
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

DB_PATH = os.environ.get("DB_PATH", "rotator.db")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS managed_playlists (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL UNIQUE,
                plex_rating_key TEXT,
                created_at      TEXT NOT NULL,
                sort_mode       TEXT NOT NULL DEFAULT 'rotation',
                unwatched_only  INTEGER NOT NULL DEFAULT 0,
                auto_sync       INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS playlist_shows (
                playlist_id        INTEGER NOT NULL,
                show_rating_key    TEXT    NOT NULL,
                show_title         TEXT    NOT NULL,
                show_thumb         TEXT,
                position           INTEGER NOT NULL,
                start_season       INTEGER NOT NULL DEFAULT 1,
                end_season         INTEGER,
                include_specials   INTEGER NOT NULL DEFAULT 0,
                include_movies     INTEGER NOT NULL DEFAULT 0,
                movie_rating_keys  TEXT    NOT NULL DEFAULT '',
                PRIMARY KEY (playlist_id, show_rating_key),
                FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id) ON DELETE CASCADE
            );
            """
        )
        # Lightweight migration for older schemas
        cols = _columns(conn, "playlist_shows")
        if "show_thumb" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN show_thumb TEXT")
        if "start_season" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN start_season INTEGER NOT NULL DEFAULT 1")
        if "end_season" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN end_season INTEGER")
        if "include_specials" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN include_specials INTEGER NOT NULL DEFAULT 0")
        if "include_movies" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN include_movies INTEGER NOT NULL DEFAULT 0")
        if "movie_rating_keys" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN movie_rating_keys TEXT NOT NULL DEFAULT ''")

        pl_cols = _columns(conn, "managed_playlists")
        if "sort_mode" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN sort_mode TEXT NOT NULL DEFAULT 'rotation'")
        if "unwatched_only" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN unwatched_only INTEGER NOT NULL DEFAULT 0")
        if "auto_sync" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN auto_sync INTEGER NOT NULL DEFAULT 1")


def create_playlist(name: str, sort_mode: str = "rotation", unwatched_only: bool = False) -> int:
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO managed_playlists (name, created_at, sort_mode, unwatched_only)
               VALUES (?, ?, ?, ?)""",
            (name, datetime.now(timezone.utc).isoformat(), sort_mode, 1 if unwatched_only else 0),
        )
        return int(cur.lastrowid)


def set_sort_mode(playlist_id: int, sort_mode: str) -> None:
    if sort_mode not in ("rotation", "air_date"):
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET sort_mode = ? WHERE id = ?",
            (sort_mode, playlist_id),
        )


def set_unwatched_only(playlist_id: int, unwatched_only: bool) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET unwatched_only = ? WHERE id = ?",
            (1 if unwatched_only else 0, playlist_id),
        )


def set_auto_sync(playlist_id: int, auto_sync: bool) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET auto_sync = ? WHERE id = ?",
            (1 if auto_sync else 0, playlist_id),
        )


def set_plex_rating_key(playlist_id: int, rating_key: str) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET plex_rating_key = ? WHERE id = ?",
            (rating_key, playlist_id),
        )


def list_playlists() -> list[sqlite3.Row]:
    with connection() as conn:
        return list(
            conn.execute("SELECT * FROM managed_playlists ORDER BY name").fetchall()
        )


def get_playlist(playlist_id: int) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM managed_playlists WHERE id = ?", (playlist_id,)
        ).fetchone()


def delete_playlist(playlist_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM managed_playlists WHERE id = ?", (playlist_id,))


def list_shows(playlist_id: int) -> list[sqlite3.Row]:
    with connection() as conn:
        return list(
            conn.execute(
                "SELECT * FROM playlist_shows WHERE playlist_id = ? ORDER BY position",
                (playlist_id,),
            ).fetchall()
        )


def get_show(playlist_id: int, show_rating_key: str) -> sqlite3.Row | None:
    with connection() as conn:
        return conn.execute(
            "SELECT * FROM playlist_shows WHERE playlist_id = ? AND show_rating_key = ?",
            (playlist_id, show_rating_key),
        ).fetchone()


def add_shows(playlist_id: int, configs: list[dict]) -> None:
    """configs: list of dicts with keys: rating_key, title, thumb,
    start_season, end_season, include_specials, include_movies,
    movie_rating_keys (list of strings). Appended after existing positions."""
    if not configs:
        return
    with connection() as conn:
        row = conn.execute(
            "SELECT COALESCE(MAX(position), -1) AS m FROM playlist_shows WHERE playlist_id = ?",
            (playlist_id,),
        ).fetchone()
        next_pos = int(row["m"]) + 1
        for cfg in configs:
            movie_keys = cfg.get("movie_rating_keys") or []
            if isinstance(movie_keys, str):
                movie_keys_str = movie_keys
            else:
                movie_keys_str = ",".join(str(k) for k in movie_keys)
            conn.execute(
                """INSERT OR IGNORE INTO playlist_shows
                   (playlist_id, show_rating_key, show_title, show_thumb,
                    position, start_season, end_season, include_specials,
                    include_movies, movie_rating_keys)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    playlist_id,
                    cfg["rating_key"],
                    cfg["title"],
                    cfg.get("thumb"),
                    next_pos,
                    int(cfg.get("start_season", 1)),
                    cfg.get("end_season"),
                    1 if cfg.get("include_specials") else 0,
                    1 if cfg.get("include_movies") else 0,
                    movie_keys_str,
                ),
            )
            next_pos += 1


def remove_show(playlist_id: int, show_rating_key: str) -> None:
    with connection() as conn:
        conn.execute(
            "DELETE FROM playlist_shows WHERE playlist_id = ? AND show_rating_key = ?",
            (playlist_id, show_rating_key),
        )


def set_positions(playlist_id: int, ordered_keys: list[str]) -> None:
    """Rewrite the position column to match the given order."""
    with connection() as conn:
        for i, key in enumerate(ordered_keys):
            conn.execute(
                "UPDATE playlist_shows SET position = ? WHERE playlist_id = ? AND show_rating_key = ?",
                (i, playlist_id, key),
            )
