"""Pure rotation/sort logic. No Plex calls in here — just lists.

Two compose modes:

  interleave(shows_episodes)
      Round-robin across shows in given order, taking episodes in
      season/episode order until every show is exhausted.

  air_date_sequence(shows_episodes, show_order)
      Combine episodes across shows, sort by:
        (air_date, part_number, show_order_position, season, episode)
      so same-day crossovers line up and "Part 1 / Part 2" episodes
      respect their declared order even when they're on different shows.

Shared helpers:

  splice_index(items)              boundary between watched/in-progress and future
  rebuild_tail(...)                future portion after current playback point
  prune_indices(items, keep_last_n) indices to remove (older watched)
"""

from __future__ import annotations

import re
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Iterable, Protocol


class _Ep(Protocol):
    rating_key: str
    show_rating_key: str
    season: int
    episode: int
    view_count: int
    view_offset_ms: int
    title: str
    air_date: str | None
    kind: str  # 'episode' or 'movie'


@dataclass
class PlaylistItem:
    """Minimal view of a playlist item for rotation logic."""

    rating_key: str
    show_rating_key: str
    season: int
    episode: int
    view_count: int = 0
    view_offset_ms: int = 0
    title: str = ""
    air_date: str | None = None
    kind: str = "episode"  # 'episode' or 'movie' (movies identify by rating_key only)


# --------------------------------------------------------------------------- #
# Part-N detection (for crossover alignment in air-date mode)
# --------------------------------------------------------------------------- #

_PART_PATTERNS = [
    re.compile(r"\b(?:Part|Pt\.?)\s+(\d+)\b", re.IGNORECASE),
    re.compile(r"\((\d+)\)\s*$"),
]


def part_number(title: str | None) -> int:
    """Return the Part N indicated by an episode title (0 if none).

    Recognizes 'Part 1', 'Pt 2', 'Pt. 3', 'The Big Case (1)' etc.
    Episodes without a Part marker return 0, which sorts before Part 1.
    """
    if not title:
        return 0
    for pat in _PART_PATTERNS:
        m = pat.search(title)
        if m:
            try:
                return int(m.group(1))
            except (ValueError, IndexError):
                continue
    return 0


# --------------------------------------------------------------------------- #
# Compose modes
# --------------------------------------------------------------------------- #


def interleave(shows_episodes: list[list[_Ep]]) -> list[_Ep]:
    """Round-robin episodes across shows.

    `shows_episodes` is a list (one entry per show, in rotation order). Each
    entry is that show's episodes already sorted oldest-first. When a show
    runs out, remaining shows continue without it.
    """
    if not shows_episodes:
        return []
    indices = [0] * len(shows_episodes)
    out: list[_Ep] = []
    while True:
        progressed = False
        for i, eps in enumerate(shows_episodes):
            if indices[i] < len(eps):
                out.append(eps[indices[i]])
                indices[i] += 1
                progressed = True
        if not progressed:
            return out


def air_date_sequence(
    shows_episodes: list[list[_Ep]],
    show_order: list[str] | None = None,
) -> list[_Ep]:
    """Combine episodes across shows and sort by air date.

    Sort key: (air_date, part_number, show_order_index, season, episode).
    - Same-day episodes naturally end up adjacent
    - Within the same day, lower Part N (or 'no Part') comes first, so a
      multi-part crossover plays in order across whatever shows it spans
    - Then user-defined show order as a tie-break, so a "headliner" show
      can still dominate when episodes truly tied
    """
    if not shows_episodes:
        return []
    pos = {key: i for i, key in enumerate(show_order or [])}

    def key(ep: _Ep):
        return (
            ep.air_date or "0000-00-00",
            part_number(ep.title),
            pos.get(ep.show_rating_key, 1 << 30),
            ep.season,
            ep.episode,
        )

    combined: list[_Ep] = []
    for eps in shows_episodes:
        combined.extend(eps)
    combined.sort(key=key)
    return combined


def compose(
    shows_episodes: list[list[_Ep]],
    mode: str = "rotation",
    show_order: list[str] | None = None,
) -> list[_Ep]:
    if mode == "air_date":
        return air_date_sequence(shows_episodes, show_order)
    return interleave(shows_episodes)


# --------------------------------------------------------------------------- #
# Splice + rebuild
# --------------------------------------------------------------------------- #


def splice_index(items: list[PlaylistItem]) -> int:
    """Index just past the last item the user has played or started."""
    last_touched = -1
    for i, item in enumerate(items):
        if item.view_count > 0 or item.view_offset_ms > 0:
            last_touched = i
    return last_touched + 1


def rebuild_tail(
    kept_items: list[PlaylistItem],
    shows_episodes_in_order: list[list[_Ep]],
    mode: str = "rotation",
    show_order: list[str] | None = None,
) -> list[_Ep]:
    """Compute the new "future" portion of a playlist.

    Mode differences:
      - rotation: for each show, skip episodes already in kept_items, then
        round-robin the remainders. This preserves the show's position in the
        rotation when an episode of it has already played.
      - air_date: compose the full canonical air-date sequence (with crossover
        Part N alignment), then drop kept episodes.
    """
    kept_by_show: dict[str, set[tuple[int, int]]] = {}
    kept_movie_keys: set[str] = set()
    for it in kept_items:
        if getattr(it, "kind", "episode") == "movie":
            kept_movie_keys.add(it.rating_key)
        else:
            kept_by_show.setdefault(it.show_rating_key, set()).add((it.season, it.episode))

    def _is_kept(e: _Ep) -> bool:
        if getattr(e, "kind", "episode") == "movie":
            return e.rating_key in kept_movie_keys
        return (e.season, e.episode) in kept_by_show.get(e.show_rating_key, set())

    if mode == "rotation":
        remainders: list[list[_Ep]] = []
        for eps in shows_episodes_in_order:
            if not eps:
                remainders.append([])
                continue
            remainders.append([e for e in eps if not _is_kept(e)])
        return interleave(remainders)

    # air_date mode
    full = compose(shows_episodes_in_order, mode=mode, show_order=show_order)
    return [e for e in full if not _is_kept(e)]


def prune_indices(items: list[PlaylistItem], keep_last_n: int) -> list[int]:
    """Return playlist indices that should be removed.

    Rule: keep all unwatched items; keep the last `keep_last_n` watched items;
    remove watched items older than that.
    """
    if keep_last_n < 0:
        keep_last_n = 0
    watched_positions = [i for i, it in enumerate(items) if it.view_count > 0]
    if len(watched_positions) <= keep_last_n:
        return []
    cutoff = watched_positions[-keep_last_n] if keep_last_n > 0 else len(items)
    return [i for i in watched_positions if i < cutoff]


def dedupe_preserving_order(items: Iterable[_Ep]) -> list[_Ep]:
    """Drop duplicates by rating_key, keeping the first occurrence."""
    seen: OrderedDict[str, _Ep] = OrderedDict()
    for it in items:
        if it.rating_key not in seen:
            seen[it.rating_key] = it
    return list(seen.values())
