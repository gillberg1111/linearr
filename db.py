"""SQLite persistence for managed rotating playlists.

For sort_mode validation, the source of truth is `rotation.VALID_SORT_MODES`.

Schema:
  managed_playlists
    id                   INTEGER PK
    name                 TEXT     — display name (and the playlist title on every backend)
    plex_rating_key      TEXT     — ratingKey of the Plex playlist (nullable; NULL when backend=jellyfin)
    jellyfin_playlist_id TEXT     — Id of the Jellyfin playlist (nullable; NULL when backend=plex)
    backend              TEXT     — 'plex' | 'jellyfin' | 'both' (default 'plex' for legacy rows)
    playlist_type        TEXT     — 'manual' | 'genre' (default 'manual')
    genre_filter         TEXT     — CSV of genre names for genre playlists (nullable)
    created_at           TEXT
    sort_mode            TEXT     — see rotation.VALID_SORT_MODES
    block_size           INTEGER  — episodes per block in rotation_blocks (default 1)
    shuffle_seed         INTEGER  — seed for shuffle_chronological (nullable)
    unwatched_only       INTEGER  — 0/1
    auto_sync            INTEGER  — 0/1, per-playlist sync opt-out
  playlist_shows
    playlist_id              INTEGER FK -> managed_playlists.id
    show_rating_key          TEXT     — primary key part; for legacy Plex rows equals plex_show_item_id;
                                        for Jellyfin-originated rows it's the Jellyfin Id (opaque, just a PK)
    plex_show_item_id        TEXT     — Plex ratingKey for this show, when present (nullable)
    jellyfin_show_item_id    TEXT     — Jellyfin Id for this show, when present (nullable)
    show_title               TEXT     — cached for UI when the backend is unreachable
    show_thumb               TEXT     — cached thumb reference
    position                 INTEGER  — user-defined order in the rotation
    weight                   INTEGER  — per-show weight for rotation_weighted (default 1)
    start_season             INTEGER  — lowest season to include (default 1)
    end_season               INTEGER  — highest season to include (NULL = no cap)
    include_specials         INTEGER  — 0/1: include Season 0 in the rotation
    include_movies           INTEGER  — 0/1
    movie_rating_keys        TEXT     — comma-separated Plex movie ratingKeys
    jellyfin_movie_item_ids  TEXT     — comma-separated Jellyfin movie Ids (parallel to movie_rating_keys)
    excluded_episode_keys    TEXT     — comma-separated "S:E" pairs to skip (e.g. "1:1,3:14")
    is_excluded              INTEGER  — 0/1, soft-delete for genre playlists (default 0)
    PRIMARY KEY (playlist_id, show_rating_key)

The Plex columns and the Jellyfin columns are each nullable — a playlist with
backend='jellyfin' only populates the jellyfin_* columns, a playlist with
backend='both' tries to populate both via title+year matching at show-add time.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator

from rotation import VALID_SORT_MODES

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


VALID_BACKENDS = ("plex", "jellyfin", "both")
VALID_PLAYLIST_TYPES = ("manual", "genre")


def init_db() -> None:
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    with connection() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS managed_playlists (
                id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                name                 TEXT NOT NULL UNIQUE,
                plex_rating_key      TEXT,
                jellyfin_playlist_id TEXT,
                backend              TEXT NOT NULL DEFAULT 'plex'
                    CHECK(backend IN ('plex','jellyfin','both')),
                playlist_type        TEXT NOT NULL DEFAULT 'manual'
                    CHECK(playlist_type IN ('manual','genre')),
                genre_filter         TEXT,
                created_at           TEXT NOT NULL,
                sort_mode            TEXT NOT NULL DEFAULT 'rotation',
                block_size           INTEGER NOT NULL DEFAULT 1,
                shuffle_seed         INTEGER,
                unwatched_only       INTEGER NOT NULL DEFAULT 0,
                auto_sync            INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS crossover_groups (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                playlist_id  INTEGER NOT NULL,
                label        TEXT    NOT NULL DEFAULT '',
                sort_index   INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS crossover_links (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id        INTEGER NOT NULL,
                show_rating_key TEXT    NOT NULL,
                season          INTEGER NOT NULL,
                episode         INTEGER NOT NULL,
                sort_index      INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (group_id) REFERENCES crossover_groups(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS playlist_shows (
                playlist_id              INTEGER NOT NULL,
                show_rating_key          TEXT    NOT NULL,
                plex_show_item_id        TEXT,
                jellyfin_show_item_id    TEXT,
                show_title               TEXT    NOT NULL,
                show_thumb               TEXT,
                position                 INTEGER NOT NULL,
                weight                   INTEGER NOT NULL DEFAULT 1,
                start_season             INTEGER NOT NULL DEFAULT 1,
                end_season               INTEGER,
                include_specials         INTEGER NOT NULL DEFAULT 0,
                include_movies           INTEGER NOT NULL DEFAULT 0,
                movie_rating_keys        TEXT    NOT NULL DEFAULT '',
                jellyfin_movie_item_ids  TEXT    NOT NULL DEFAULT '',
                excluded_episode_keys    TEXT    NOT NULL DEFAULT '',
                is_excluded              INTEGER NOT NULL DEFAULT 0,
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
        # v1.1.0 — Jellyfin columns
        if "plex_show_item_id" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN plex_show_item_id TEXT")
            # One-time backfill: every legacy row was Plex-originated, so the
            # PK show_rating_key IS the Plex ratingKey. Future rows set this
            # explicitly via add_shows().
            conn.execute(
                "UPDATE playlist_shows SET plex_show_item_id = show_rating_key "
                "WHERE plex_show_item_id IS NULL"
            )
        if "jellyfin_show_item_id" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN jellyfin_show_item_id TEXT")
        if "jellyfin_movie_item_ids" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN jellyfin_movie_item_ids TEXT NOT NULL DEFAULT ''")
        # v1.2.0 — per-episode exclusions
        if "excluded_episode_keys" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN excluded_episode_keys TEXT NOT NULL DEFAULT ''")
        # v1.3.0 — weighted rotation
        if "weight" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN weight INTEGER NOT NULL DEFAULT 1")
        # v1.4.0 — genre playlist soft-delete flag
        if "is_excluded" not in cols:
            conn.execute("ALTER TABLE playlist_shows ADD COLUMN is_excluded INTEGER NOT NULL DEFAULT 0")

        pl_cols = _columns(conn, "managed_playlists")
        if "sort_mode" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN sort_mode TEXT NOT NULL DEFAULT 'rotation'")
        if "unwatched_only" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN unwatched_only INTEGER NOT NULL DEFAULT 0")
        if "auto_sync" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN auto_sync INTEGER NOT NULL DEFAULT 1")
        # v1.1.0 — Jellyfin columns
        if "jellyfin_playlist_id" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN jellyfin_playlist_id TEXT")
        if "backend" not in pl_cols:
            # SQLite ALTER TABLE can't add a CHECK constraint; the helpers below
            # validate writes. Existing rows default to 'plex' as intended.
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN backend TEXT NOT NULL DEFAULT 'plex'"
            )
        # v1.3.0 — block scheduling + shuffle
        if "block_size" not in pl_cols:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN block_size INTEGER NOT NULL DEFAULT 1"
            )
        if "shuffle_seed" not in pl_cols:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN shuffle_seed INTEGER"
            )
        # v1.4.0 — genre playlists
        if "playlist_type" not in pl_cols:
            conn.execute(
                "ALTER TABLE managed_playlists ADD COLUMN playlist_type TEXT NOT NULL DEFAULT 'manual'"
            )
        if "genre_filter" not in pl_cols:
            conn.execute("ALTER TABLE managed_playlists ADD COLUMN genre_filter TEXT")

        # v1.5.0 — crossover groups (migration for pre-existing DBs)
        existing_tables = {
            r["name"]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if "crossover_groups" not in existing_tables:
            conn.execute(
                """CREATE TABLE crossover_groups (
                    id           INTEGER PRIMARY KEY AUTOINCREMENT,
                    playlist_id  INTEGER NOT NULL,
                    label        TEXT    NOT NULL DEFAULT '',
                    sort_index   INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (playlist_id) REFERENCES managed_playlists(id) ON DELETE CASCADE
                )"""
            )
        if "crossover_links" not in existing_tables:
            conn.execute(
                """CREATE TABLE crossover_links (
                    id              INTEGER PRIMARY KEY AUTOINCREMENT,
                    group_id        INTEGER NOT NULL,
                    show_rating_key TEXT    NOT NULL,
                    season          INTEGER NOT NULL,
                    episode         INTEGER NOT NULL,
                    sort_index      INTEGER NOT NULL DEFAULT 0,
                    FOREIGN KEY (group_id) REFERENCES crossover_groups(id) ON DELETE CASCADE
                )"""
            )


def create_playlist(
    name: str,
    sort_mode: str = "rotation",
    unwatched_only: bool = False,
    auto_sync: bool = True,
    backend: str = "plex",
    playlist_type: str = "manual",
    genre_filter: str | None = None,
) -> int:
    if backend not in VALID_BACKENDS:
        raise ValueError(f"Invalid backend: {backend!r}. Must be one of {VALID_BACKENDS}")
    if playlist_type not in VALID_PLAYLIST_TYPES:
        raise ValueError(f"Invalid playlist_type: {playlist_type!r}. Must be one of {VALID_PLAYLIST_TYPES}")
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO managed_playlists
               (name, created_at, sort_mode, unwatched_only, auto_sync, backend,
                playlist_type, genre_filter)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                name,
                datetime.now(timezone.utc).isoformat(),
                sort_mode,
                1 if unwatched_only else 0,
                1 if auto_sync else 0,
                backend,
                playlist_type,
                genre_filter,
            ),
        )
        return int(cur.lastrowid)


def set_genre_filter(playlist_id: int, genre_filter: str | None) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET genre_filter = ? WHERE id = ?",
            (genre_filter, playlist_id),
        )


def set_show_excluded(
    playlist_id: int, show_rating_key: str, excluded: bool
) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET is_excluded = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (1 if excluded else 0, playlist_id, show_rating_key),
        )


def set_sort_mode(playlist_id: int, sort_mode: str) -> None:
    if sort_mode not in VALID_SORT_MODES:
        raise ValueError(f"Invalid sort_mode: {sort_mode!r}")
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET sort_mode = ? WHERE id = ?",
            (sort_mode, playlist_id),
        )


def set_block_size(playlist_id: int, block_size: int) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET block_size = ? WHERE id = ?",
            (max(1, int(block_size)), playlist_id),
        )


def set_shuffle_seed(playlist_id: int, seed: int | None) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET shuffle_seed = ? WHERE id = ?",
            (seed, playlist_id),
        )


def set_show_weight(playlist_id: int, show_rating_key: str, weight: int) -> None:
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET weight = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (max(1, int(weight)), playlist_id, show_rating_key),
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


def set_plex_rating_key(playlist_id: int, rating_key: str | None) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET plex_rating_key = ? WHERE id = ?",
            (rating_key, playlist_id),
        )


def set_jellyfin_playlist_id(playlist_id: int, jellyfin_id: str | None) -> None:
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET jellyfin_playlist_id = ? WHERE id = ?",
            (jellyfin_id, playlist_id),
        )


def set_backend(playlist_id: int, backend: str) -> None:
    if backend not in VALID_BACKENDS:
        raise ValueError(f"Invalid backend: {backend!r}. Must be one of {VALID_BACKENDS}")
    with connection() as conn:
        conn.execute(
            "UPDATE managed_playlists SET backend = ? WHERE id = ?",
            (backend, playlist_id),
        )


def set_plex_show_item_id(
    playlist_id: int, show_rating_key: str, plex_show_item_id: str | None
) -> None:
    """Persist the Plex show ratingKey matched at add-time or healed at sync-time."""
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET plex_show_item_id = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (plex_show_item_id, playlist_id, show_rating_key),
        )


def set_jellyfin_show_item_id(
    playlist_id: int, show_rating_key: str, jellyfin_show_item_id: str | None
) -> None:
    """Persist the Jellyfin show id matched at add-time (or healed at sync-time).

    `show_rating_key` is the row's PK component — for legacy Plex-originated
    rows it equals the Plex ratingKey; for Jellyfin-originated rows it's the
    Jellyfin Id.
    """
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET jellyfin_show_item_id = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (jellyfin_show_item_id, playlist_id, show_rating_key),
        )


def set_excluded_episodes(
    playlist_id: int,
    show_rating_key: str,
    excluded_episodes: set[tuple[int, int]] | list[tuple[int, int]] | str,
) -> None:
    """Persist the set of (season, episode) pairs to skip for one show."""
    if isinstance(excluded_episodes, str):
        s = excluded_episodes
    else:
        s = ",".join(f"{int(se):d}:{int(ep):d}" for se, ep in sorted(excluded_episodes))
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET excluded_episode_keys = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (s, playlist_id, show_rating_key),
        )


def set_jellyfin_movie_item_ids(
    playlist_id: int, show_rating_key: str, jellyfin_movie_item_ids: list[str] | str
) -> None:
    if isinstance(jellyfin_movie_item_ids, str):
        s = jellyfin_movie_item_ids
    else:
        s = ",".join(str(k) for k in jellyfin_movie_item_ids if k)
    with connection() as conn:
        conn.execute(
            """UPDATE playlist_shows SET jellyfin_movie_item_ids = ?
               WHERE playlist_id = ? AND show_rating_key = ?""",
            (s, playlist_id, show_rating_key),
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
    movie_rating_keys (list of strings). Appended after existing positions.

    v1.1.0 — optional Jellyfin fields (all default to None / empty):
      jellyfin_show_item_id    — opaque Jellyfin Id matched at add-time
      jellyfin_movie_item_ids  — list[str] parallel to movie_rating_keys
    """
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
            jf_movie_keys = cfg.get("jellyfin_movie_item_ids") or []
            if isinstance(jf_movie_keys, str):
                jf_movie_keys_str = jf_movie_keys
            else:
                jf_movie_keys_str = ",".join(str(k) for k in jf_movie_keys if k)
            # Default plex_show_item_id: if not supplied AND the PK looks
            # like a Plex ratingKey (digits only), assume Plex. Otherwise
            # caller must set it explicitly. This keeps single-backend
            # callers backward-compatible.
            plex_show_id = cfg.get("plex_show_item_id")
            if plex_show_id is None and str(cfg["rating_key"]).isdigit():
                plex_show_id = str(cfg["rating_key"])
            # Serialize excluded-episode set into "S:E,S:E,..." form.
            excl_raw = cfg.get("excluded_episodes") or cfg.get("excluded_episode_keys") or ""
            if isinstance(excl_raw, str):
                excl_str = excl_raw
            else:
                excl_str = ",".join(f"{int(s):d}:{int(e):d}" for s, e in sorted(excl_raw))
            weight_val = max(1, int(cfg.get("weight", 1) or 1))
            conn.execute(
                """INSERT OR IGNORE INTO playlist_shows
                   (playlist_id, show_rating_key, plex_show_item_id, jellyfin_show_item_id,
                    show_title, show_thumb, position, weight,
                    start_season, end_season, include_specials,
                    include_movies, movie_rating_keys, jellyfin_movie_item_ids,
                    excluded_episode_keys)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    playlist_id,
                    cfg["rating_key"],
                    plex_show_id,
                    cfg.get("jellyfin_show_item_id"),
                    cfg["title"],
                    cfg.get("thumb"),
                    next_pos,
                    weight_val,
                    int(cfg.get("start_season", 1)),
                    cfg.get("end_season"),
                    1 if cfg.get("include_specials") else 0,
                    1 if cfg.get("include_movies") else 0,
                    movie_keys_str,
                    jf_movie_keys_str,
                    excl_str,
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


# --------------------------------------------------------------------------- #
# v1.5.0 — Crossover groups
# --------------------------------------------------------------------------- #


def list_crossover_groups(playlist_id: int) -> list[dict]:
    """Return crossover groups with nested links for a playlist."""
    with connection() as conn:
        groups = conn.execute(
            "SELECT * FROM crossover_groups WHERE playlist_id = ? ORDER BY sort_index, id",
            (playlist_id,),
        ).fetchall()
        out: list[dict] = []
        for g in groups:
            links = conn.execute(
                "SELECT * FROM crossover_links WHERE group_id = ? ORDER BY sort_index, id",
                (g["id"],),
            ).fetchall()
            out.append({
                "id": g["id"],
                "playlist_id": g["playlist_id"],
                "label": g["label"],
                "sort_index": g["sort_index"],
                "links": [dict(li) for li in links],
            })
        return out


def create_crossover_group(playlist_id: int, label: str = "") -> int:
    with connection() as conn:
        cur = conn.execute(
            "INSERT INTO crossover_groups (playlist_id, label) VALUES (?, ?)",
            (playlist_id, label),
        )
        return int(cur.lastrowid)


def delete_crossover_group(group_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM crossover_groups WHERE id = ?", (group_id,))


def add_crossover_link(
    group_id: int, show_rating_key: str, season: int, episode: int, sort_index: int = 0
) -> int:
    with connection() as conn:
        cur = conn.execute(
            """INSERT INTO crossover_links (group_id, show_rating_key, season, episode, sort_index)
               VALUES (?, ?, ?, ?, ?)""",
            (group_id, show_rating_key, season, episode, sort_index),
        )
        return int(cur.lastrowid)


def remove_crossover_link(link_id: int) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM crossover_links WHERE id = ?", (link_id,))
