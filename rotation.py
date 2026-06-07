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


def interleave_weighted(
    shows_episodes: list[list[_Ep]],
    weights: list[int] | None = None,
) -> list[_Ep]:
    """Weighted round-robin: take `weights[i]` episodes from show i per cycle.

    `weights` defaults to all-1 (equivalent to `interleave`). Weight values
    < 1 are clamped to 1 — a 0-weight show would never be taken from, which
    is what removing the show entirely is for.

    When a show has fewer episodes left than its weight asks for, we take
    what's available and advance — no carry-over to the next cycle. This
    keeps the algorithm O(n) and matches the user's expectation that a
    weight is a maximum-per-cycle, not a strict ratio.
    """
    if not shows_episodes:
        return []
    n = len(shows_episodes)
    if weights is None:
        weights = [1] * n
    else:
        weights = [max(1, int(w)) for w in weights]
        # Pad / trim to match n so callers can be a bit sloppy.
        if len(weights) < n:
            weights = weights + [1] * (n - len(weights))
        elif len(weights) > n:
            weights = weights[:n]

    indices = [0] * n
    out: list[_Ep] = []
    while True:
        progressed = False
        for i, eps in enumerate(shows_episodes):
            taken = 0
            while indices[i] < len(eps) and taken < weights[i]:
                out.append(eps[indices[i]])
                indices[i] += 1
                taken += 1
                progressed = True
        if not progressed:
            return out


def interleave_blocks(
    shows_episodes: list[list[_Ep]],
    block_size: int = 1,
) -> list[_Ep]:
    """Block scheduling: take `block_size` from show 0, then `block_size`
    from show 1, etc., then back to show 0. Equivalent to
    `interleave_weighted(shows_episodes, [block_size] * n)`."""
    bs = max(1, int(block_size))
    return interleave_weighted(shows_episodes, [bs] * len(shows_episodes))


def shuffle_chronological(
    shows_episodes: list[list[_Ep]],
    seed: int | None = None,
) -> list[_Ep]:
    """Random sequence with two constraints:

    1. Each show's own episodes stay in their input (chronological) order.
       We never reorder within a show.
    2. No two episodes from the same show play back-to-back, when avoidable.

    The algorithm: at each step, pick uniformly at random among the shows
    whose head episode is allowed (i.e. not the show we just took from). If
    that filter leaves no candidates, fall back to any show with episodes
    remaining — i.e. when one show has many more episodes than the rest,
    eventually we have no choice but to take consecutive episodes from it.

    `seed` makes the output deterministic for the same input + seed. None
    seeds from system time (effectively random per call). Callers that
    want a stable result across syncs (e.g. for a "shuffle once and lock
    it" UX) should persist the seed and pass it back.
    """
    import random as _random
    rng = _random.Random(seed)

    indices = [0] * len(shows_episodes)
    remaining_lengths = [len(eps) for eps in shows_episodes]
    out: list[_Ep] = []
    last_show = -1

    while True:
        candidates = [i for i, rem in enumerate(remaining_lengths) if rem > 0 and i != last_show]
        if not candidates:
            # Forced fall-back: any show still has episodes? Take from it.
            candidates = [i for i, rem in enumerate(remaining_lengths) if rem > 0]
            if not candidates:
                return out
        pick = rng.choice(candidates)
        out.append(shows_episodes[pick][indices[pick]])
        indices[pick] += 1
        remaining_lengths[pick] -= 1
        last_show = pick


def air_date_sequence(
    shows_episodes: list[list[_Ep]],
    show_order: list[str] | None = None,
    crossover_map: dict[tuple[str, int, int], tuple[int, int]] | None = None,
) -> list[_Ep]:
    """Combine episodes across shows and sort by air date.

    Sort key: (air_date, crossover_info, part_number, show_order_index,
    season, episode).

    - Same-day episodes naturally end up adjacent.
    - Manually defined crossover groups sort before auto-detected Part N on
      the same day, with group members in user-defined sort_index order.
    - Then part_number captures Part 1/Part 2/etc. for non-grouped episodes.
    - Then user-defined show order as a tie-break.
    """
    if not shows_episodes:
        return []
    pos = {key: i for i, key in enumerate(show_order or [])}

    def key(ep: _Ep):
        g = (
            crossover_map.get((ep.show_rating_key, ep.season, ep.episode))
            if crossover_map
            else None
        )
        return (
            ep.air_date or "0000-00-00",
            (0, g[0], g[1]) if g else (1, 0, 0),
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
    *,
    weights: list[int] | None = None,
    block_size: int = 1,
    shuffle_seed: int | None = None,
    crossover_map: dict[tuple[str, int, int], tuple[int, int]] | None = None,
) -> list[_Ep]:
    """Pick a compose strategy by mode name.

    Modes:
      - 'rotation'              → interleave (round-robin)
      - 'rotation_weighted'     → interleave_weighted(weights=...)
      - 'rotation_blocks'       → interleave_blocks(block_size=...)
      - 'air_date'              → air_date_sequence(show_order=...)
      - 'shuffle_chronological' → shuffle_chronological(seed=shuffle_seed)
    """
    if mode == "air_date":
        return air_date_sequence(shows_episodes, show_order, crossover_map)
    if mode == "rotation_weighted":
        return interleave_weighted(shows_episodes, weights)
    if mode == "rotation_blocks":
        return interleave_blocks(shows_episodes, block_size)
    if mode == "shuffle_chronological":
        return shuffle_chronological(shows_episodes, shuffle_seed)
    return interleave(shows_episodes)


# Tuple of every accepted sort_mode value. Single source of truth.
VALID_SORT_MODES = (
    "rotation",
    "rotation_weighted",
    "rotation_blocks",
    "air_date",
    "shuffle_chronological",
)


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
    *,
    weights: list[int] | None = None,
    block_size: int = 1,
    shuffle_seed: int | None = None,
    crossover_map: dict[tuple[str, int, int], tuple[int, int]] | None = None,
) -> list[_Ep]:
    """Compute the new "future" portion of a playlist.

    Modes:
      - rotation / rotation_weighted / rotation_blocks: drop kept episodes
        from each show's remainder list, then interleave the remainders with
        the appropriate algorithm. Preserves each show's position in the
        rotation when one of its episodes has already played.
      - air_date / shuffle_chronological: compose the FULL canonical sequence
        using all input episodes, then drop kept ones. Preserves the
        sequence's original ordering for the unplayed tail.
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

    if mode in ("rotation", "rotation_weighted", "rotation_blocks"):
        remainders: list[list[_Ep]] = []
        for eps in shows_episodes_in_order:
            if not eps:
                remainders.append([])
                continue
            remainders.append([e for e in eps if not _is_kept(e)])
        if mode == "rotation_weighted":
            return interleave_weighted(remainders, weights)
        if mode == "rotation_blocks":
            return interleave_blocks(remainders, block_size)
        return interleave(remainders)

    # air_date and shuffle_chronological: compose full, then drop kept.
    full = compose(
        shows_episodes_in_order, mode=mode, show_order=show_order,
        weights=weights, block_size=block_size, shuffle_seed=shuffle_seed,
        crossover_map=crossover_map,
    )
    return [e for e in full if not _is_kept(e)]


def prune_indices_for_counts(view_counts: list[int], keep_last_n: int) -> list[int]:
    """Indices (into an ordered list) of WATCHED items that should be removed.

    Rule: keep all unwatched items; keep the last `keep_last_n` watched items;
    remove watched items older than that. `view_counts[i] > 0` means watched.
    Pure: takes only the ordered play counts, so any caller (playlist items OR
    a franchise's resolved library keys) can use it.
    """
    if keep_last_n < 0:
        keep_last_n = 0
    watched_positions = [i for i, c in enumerate(view_counts) if c > 0]
    if len(watched_positions) <= keep_last_n:
        return []
    cutoff = watched_positions[-keep_last_n] if keep_last_n > 0 else len(view_counts)
    return [i for i in watched_positions if i < cutoff]


def prune_indices(items: list[PlaylistItem], keep_last_n: int) -> list[int]:
    """Return playlist indices that should be removed. See prune_indices_for_counts."""
    return prune_indices_for_counts([it.view_count for it in items], keep_last_n)


def dedupe_preserving_order(items: Iterable[_Ep]) -> list[_Ep]:
    """Drop duplicates by rating_key, keeping the first occurrence."""
    seen: OrderedDict[str, _Ep] = OrderedDict()
    for it in items:
        if it.rating_key not in seen:
            seen[it.rating_key] = it
    return list(seen.values())
