"""Unit tests for the pure rotation/sort logic in rotation.py.

These tests don't need Plex, network, or any installed deps beyond stdlib +
the rotation module itself. Run with:

    python tests.py
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

import rotation
from rotation import PlaylistItem


# --------------------------------------------------------------------------- #
# Tiny helpers
# --------------------------------------------------------------------------- #


@dataclass
class Ep:
    rating_key: str
    show_rating_key: str
    season: int
    episode: int
    title: str = ""
    air_date: str | None = None
    view_count: int = 0
    view_offset_ms: int = 0


def mk(show: str, s: int, e: int, vc: int = 0, title: str = "", date: str | None = None) -> Ep:
    return Ep(
        rating_key=f"{show}-{s}-{e}",
        show_rating_key=show,
        season=s,
        episode=e,
        view_count=vc,
        title=title or f"{show} S{s:02d}E{e:02d}",
        air_date=date,
    )


_results: list[tuple[bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    _results.append((cond, name + ("" if cond or not detail else f"  ({detail})")))


# --------------------------------------------------------------------------- #
# Rotation interleave
# --------------------------------------------------------------------------- #


def test_interleave_two_equal():
    A = [mk("A", 1, i) for i in range(1, 4)]
    B = [mk("B", 1, i) for i in range(1, 4)]
    result = rotation.interleave([A, B])
    keys = [r.rating_key for r in result]
    expected = ["A-1-1", "B-1-1", "A-1-2", "B-1-2", "A-1-3", "B-1-3"]
    check("interleave: 2 shows, equal length", keys == expected, f"got {keys}")


def test_interleave_uneven():
    A = [mk("A", 1, 1), mk("A", 1, 2)]
    B = [mk("B", 1, 1), mk("B", 1, 2), mk("B", 1, 3), mk("B", 1, 4)]
    result = rotation.interleave([A, B])
    keys = [r.rating_key for r in result]
    expected = ["A-1-1", "B-1-1", "A-1-2", "B-1-2", "B-1-3", "B-1-4"]
    check("interleave: uneven lengths drop the short show", keys == expected, f"got {keys}")


def test_interleave_three_uneven():
    A = [mk("A", 1, 1), mk("A", 1, 2), mk("A", 2, 1)]
    B = [mk("B", 1, 1), mk("B", 1, 2)]
    C = [mk("C", 1, 1), mk("C", 1, 2), mk("C", 1, 3)]
    result = rotation.interleave([A, B, C])
    keys = [r.rating_key for r in result]
    expected = ["A-1-1", "B-1-1", "C-1-1", "A-1-2", "B-1-2", "C-1-2", "A-2-1", "C-1-3"]
    check("interleave: 3 shows uneven", keys == expected, f"got {keys}")


def test_interleave_empty():
    check("interleave: empty input -> empty output", rotation.interleave([]) == [])
    check("interleave: all empty shows -> empty", rotation.interleave([[], []]) == [])


# --------------------------------------------------------------------------- #
# Splice index
# --------------------------------------------------------------------------- #


def test_splice_index_after_last_touched():
    items = [
        PlaylistItem("a1", "A", 1, 1, view_count=1),
        PlaylistItem("b1", "B", 1, 1, view_count=1),
        PlaylistItem("a2", "A", 1, 2, view_offset_ms=500),  # in progress
        PlaylistItem("b2", "B", 1, 2),
        PlaylistItem("a3", "A", 1, 3),
    ]
    check("splice_index: returns last_touched + 1", rotation.splice_index(items) == 3)


def test_splice_index_nothing_touched():
    check(
        "splice_index: 0 when no item is watched/in-progress",
        rotation.splice_index([PlaylistItem("a1", "A", 1, 1)]) == 0,
    )


# --------------------------------------------------------------------------- #
# Rebuild tail — rotation mode
# --------------------------------------------------------------------------- #


def test_rebuild_tail_rotation_with_new_show():
    kept = [
        PlaylistItem("A-1-1", "A", 1, 1, view_count=1),
        PlaylistItem("B-1-1", "B", 1, 1, view_count=1),
        PlaylistItem("A-1-2", "A", 1, 2, view_count=1),
    ]
    A_full = [mk("A", 1, 1), mk("A", 1, 2), mk("A", 1, 3), mk("A", 1, 4)]
    B_full = [mk("B", 1, 1), mk("B", 1, 2), mk("B", 1, 3)]
    C_full = [mk("C", 1, 1), mk("C", 1, 2)]
    tail = rotation.rebuild_tail(kept, [A_full, B_full, C_full], mode="rotation")
    keys = [t.rating_key for t in tail]
    # A: skip A1,A2 -> A3,A4 ; B: skip B1 -> B2,B3 ; C: full
    expected = ["A-1-3", "B-1-2", "C-1-1", "A-1-4", "B-1-3", "C-1-2"]
    check("rebuild_tail rotation: splices new show", keys == expected, f"got {keys}")


# --------------------------------------------------------------------------- #
# Prune indices
# --------------------------------------------------------------------------- #


def test_prune_keeps_last_n_watched():
    items = [
        PlaylistItem("a", "A", 1, 1, view_count=1),
        PlaylistItem("b", "B", 1, 1, view_count=1),
        PlaylistItem("c", "A", 1, 2, view_count=0),
        PlaylistItem("d", "B", 1, 2, view_count=1),
        PlaylistItem("e", "A", 1, 3, view_count=1),
        PlaylistItem("f", "B", 1, 3, view_count=0),
    ]
    idx = rotation.prune_indices(items, keep_last_n=2)
    check("prune_indices: keeps last 2 watched (3,4), removes (0,1)", idx == [0, 1])


def test_prune_fewer_than_n():
    items = [PlaylistItem("a", "A", 1, 1, view_count=1), PlaylistItem("b", "A", 1, 2)]
    check("prune_indices: noop when fewer watched than N", rotation.prune_indices(items, 5) == [])


def test_prune_zero_keep():
    items = [
        PlaylistItem("a", "A", 1, 1, view_count=1),
        PlaylistItem("b", "B", 1, 1, view_count=1),
        PlaylistItem("c", "A", 1, 2),
        PlaylistItem("d", "B", 1, 2, view_count=1),
        PlaylistItem("e", "A", 1, 3, view_count=1),
        PlaylistItem("f", "B", 1, 3),
    ]
    check("prune_indices: keep_last_n=0 removes all watched", rotation.prune_indices(items, 0) == [0, 1, 3, 4])


# --------------------------------------------------------------------------- #
# Part N detection
# --------------------------------------------------------------------------- #


def test_part_number():
    cases = [
        ("Part 1", 1),
        ("The Crossover, Part 2", 2),
        ("Final Showdown (Pt. 3)", 3),
        ("The Big One (1)", 1),
        ("Ordinary Episode", 0),
        ("", 0),
        (None, 0),
        ("PART 4: The Reckoning", 4),
        ("Pt 5", 5),
        ("(7)", 7),
        ("Episode (2) extra text", 0),  # only end-of-string (N)
    ]
    for title, expected in cases:
        got = rotation.part_number(title)
        check(f"part_number({title!r}) -> {expected}", got == expected, f"got {got}")


# --------------------------------------------------------------------------- #
# Air-date sequence
# --------------------------------------------------------------------------- #


def test_air_date_crossover_alignment():
    A, B, C = "showA", "showB", "showC"
    eps = [
        mk(A, 1, 5, title="Standalone A1", date="2008-04-15"),
        mk(B, 2, 3, title="Crossover, Part 2", date="2008-04-15"),
        mk(C, 3, 8, title="Crossover, Part 1", date="2008-04-15"),
        mk(A, 1, 6, title="Next Week A", date="2008-04-22"),
        mk(B, 2, 4, title="Next Week B", date="2008-04-22"),
        mk(A, 1, 4, title="Earlier A", date="2008-04-08"),
    ]
    result = rotation.air_date_sequence([eps], show_order=[A, B, C])
    keys = [r.rating_key for r in result]
    expected = [
        "showA-1-4",     # 04-08
        "showA-1-5",     # 04-15 standalone (part 0)
        "showC-3-8",     # 04-15 Part 1
        "showB-2-3",     # 04-15 Part 2
        "showA-1-6",     # 04-22 (A before B by show order)
        "showB-2-4",
    ]
    check("air_date_sequence: crossover Part 1/2 aligned same day", keys == expected, f"got {keys}")


def test_air_date_show_order_tiebreak():
    eps = [
        Ep("x", "S1", 1, 1, "Pilot", "2010-01-01"),
        Ep("y", "S2", 1, 1, "Pilot", "2010-01-01"),
        Ep("z", "S3", 1, 1, "Pilot", "2010-01-01"),
    ]
    result = rotation.air_date_sequence([[e] for e in eps], show_order=["S2", "S3", "S1"])
    keys = [r.rating_key for r in result]
    check("air_date_sequence: ties break by user-defined show order",
          keys == ["y", "z", "x"], f"got {keys}")


def test_air_date_no_date_sorts_first():
    eps = [
        mk("A", 1, 1, date="2010-01-01"),
        mk("A", 1, 2, date=None),
        mk("A", 1, 3, date="2010-01-15"),
    ]
    result = rotation.air_date_sequence([eps], show_order=["A"])
    keys = [r.rating_key for r in result]
    # Episodes without date have air_date = "0000-00-00" which sorts before "2010-*"
    check("air_date_sequence: missing date sorts before dated", keys == ["A-1-2", "A-1-1", "A-1-3"], f"got {keys}")


def test_compose_branches():
    A = [mk("A", 1, 1, date="2010-01-01"), mk("A", 1, 2, date="2010-01-08")]
    B = [mk("B", 1, 1, date="2008-12-01"), mk("B", 1, 2, date="2012-06-01")]
    rot = rotation.compose([A, B], mode="rotation")
    air = rotation.compose([A, B], mode="air_date", show_order=["A", "B"])
    check("compose rotation: equivalent to interleave",
          [r.rating_key for r in rot] == ["A-1-1", "B-1-1", "A-1-2", "B-1-2"])
    check("compose air_date: chronological",
          [r.rating_key for r in air] == ["B-1-1", "A-1-1", "A-1-2", "B-1-2"])


# --------------------------------------------------------------------------- #
# Rebuild tail — air-date mode
# --------------------------------------------------------------------------- #


def test_rebuild_tail_with_movies():
    """Movies (kind='movie') are identified by rating_key only, so the
    standard (season, episode) tuple doesn't accidentally collide."""
    @dataclass
    class M:
        rating_key: str
        show_rating_key: str
        season: int
        episode: int
        title: str
        air_date: str | None = None
        view_count: int = 0
        view_offset_ms: int = 0
        kind: str = "movie"

    A_eps = [mk("A", 1, 1, date="2002-07-12"), mk("A", 1, 2, date="2002-07-19")]
    A_movies = [M("monk-movie", "A", 999, 1, "Mr. Monk's Last Case: A Monk Movie", "2023-12-08")]
    # Kept: only the first episode (S01E01)
    kept = [PlaylistItem("A-1-1", "A", 1, 1, view_count=1)]
    tail = rotation.rebuild_tail(kept, [A_eps + A_movies], mode="rotation")
    keys = [t.rating_key for t in tail]
    # A1 is kept; remaining is A2 then the movie (movies have season=999 so they
    # sort after S01E02 in canonical order)
    check("rebuild_tail with movies: movie appears after episodes", keys == ["A-1-2", "monk-movie"], f"got {keys}")

    # If the movie is already kept, it doesn't reappear
    kept2 = [
        PlaylistItem("A-1-1", "A", 1, 1, view_count=1),
        PlaylistItem("A-1-2", "A", 1, 2, view_count=1),
        PlaylistItem("monk-movie", "A", 999, 1, view_count=1, kind="movie"),
    ]
    tail2 = rotation.rebuild_tail(kept2, [A_eps + A_movies], mode="rotation")
    check("rebuild_tail with movies: kept movie is dropped", len(tail2) == 0, f"got {tail2}")


def test_air_date_movie_slots_chronologically():
    @dataclass
    class M:
        rating_key: str
        show_rating_key: str
        season: int
        episode: int
        title: str
        air_date: str | None
        view_count: int = 0
        view_offset_ms: int = 0
        kind: str = "movie"

    A_eps = [mk("A", 1, 1, date="2002-07-12"), mk("A", 8, 16, date="2009-12-04")]
    B_eps = [mk("B", 1, 1, date="2006-07-07"), mk("B", 1, 2, date="2006-07-14")]
    A_movie = M("monk-movie", "A", 999, 1, "Monk Movie", "2023-12-08")
    result = rotation.air_date_sequence([A_eps + [A_movie], B_eps], show_order=["A", "B"])
    keys = [r.rating_key for r in result]
    expected = ["A-1-1", "B-1-1", "B-1-2", "A-8-16", "monk-movie"]
    check("air_date with movie: movie slots in by its 2023 date",
          keys == expected, f"got {keys}")


def test_rebuild_tail_air_date_drops_kept():
    A = [mk("A", 1, 4, date="2008-04-08"), mk("A", 1, 5, date="2008-04-15"), mk("A", 1, 6, date="2008-04-22")]
    B = [mk("B", 2, 3, title="Crossover, Part 2", date="2008-04-15"),
         mk("B", 2, 4, date="2008-04-22")]
    C = [mk("C", 3, 8, title="Crossover, Part 1", date="2008-04-15")]
    kept = [PlaylistItem("A-1-4", "A", 1, 4, view_count=1, air_date="2008-04-08")]
    tail = rotation.rebuild_tail(
        kept, [A, B, C], mode="air_date", show_order=["A", "B", "C"]
    )
    keys = [t.rating_key for t in tail]
    expected = ["A-1-5", "C-3-8", "B-2-3", "A-1-6", "B-2-4"]
    check("rebuild_tail air_date: kept episode dropped, crossover stays aligned",
          keys == expected, f"got {keys}")


# --------------------------------------------------------------------------- #
# Backend safety guards — defense-in-depth
# --------------------------------------------------------------------------- #


def test_jellyfin_safety_blocks_library_item_deletion():
    """Defense-in-depth: no caller should ever talk DELETE /Items through the
    standard request path — that endpoint mass-deletes files from disk."""
    from jellyfin_client import _check_delete_safety, JellyfinSafetyError
    forbidden_paths = [
        "/Items",                                      # mass library delete
        "/Items/abc-123",                              # single library delete
        "/Items/abc/Images/Primary",                   # delete primary image
        "/Items/abc/Images/Primary/0",                 # delete image by index
        "/Library/VirtualFolders",                     # remove a library
        "/Library/VirtualFolders/Paths",               # remove a media path
        "/Collections/c1/Items",                       # modify user collection
        "/Users/u-1",                                  # delete user account
        "/Devices",                                    # delete device
        "/Videos/v1/AlternateSources",                 # remove alt video src
        "/Videos/v1/Subtitles/0",                      # remove subtitle file
        "/Audio/a1/Lyrics",                            # remove lyrics
        "/Auth/Keys/somekey",                          # remove an api key
        "/LiveTv/Recordings/r1",                       # remove DVR recording
        "/Plugins/p1",                                 # uninstall plugin
        "/Branding/Splashscreen",                      # remove splashscreen
        "/UserFavoriteItems/x",                        # untoggle favorite
        "/UserPlayedItems/x",                          # mark unplayed
    ]
    for path in forbidden_paths:
        try:
            _check_delete_safety(path)
            check(f"safety: DELETE {path} refused", False, "guard did not raise")
        except JellyfinSafetyError:
            check(f"safety: DELETE {path} refused", True)
        except Exception as e:
            check(f"safety: DELETE {path} refused", False, f"wrong exception {e!r}")


def test_jellyfin_safety_allows_playlist_item_removal():
    """Only DELETE we let through the standard path: removing items FROM a
    playlist (does NOT touch the underlying library items)."""
    from jellyfin_client import _check_delete_safety
    try:
        _check_delete_safety("/Playlists/abc-playlist-id/Items")
        check("safety: DELETE /Playlists/{id}/Items allowed", True)
    except Exception as e:
        check("safety: DELETE /Playlists/{id}/Items allowed", False, f"raised {e!r}")


def test_jellyfin_safety_rejects_lookalike_paths():
    """Sub-paths and variations on the allowed pattern must still be refused —
    no fuzzy matching that could let a sibling endpoint through."""
    from jellyfin_client import _check_delete_safety, JellyfinSafetyError
    suspect_paths = [
        "/Playlists/abc/Items/some-entry",   # entry sub-path, not the bulk allowed
        "/PlaylistsX/abc/Items",             # different segment
        "/Playlists",                        # parent
        "/Playlists/abc",                    # the playlist itself (goes through delete_playlist bypass)
    ]
    for path in suspect_paths:
        try:
            _check_delete_safety(path)
            check(f"safety: lookalike {path} refused", False, "guard did not raise")
        except JellyfinSafetyError:
            check(f"safety: lookalike {path} refused", True)


def test_plex_safety_patches_destructive_methods():
    """Plex's safety guard monkey-patches the python-plexapi item classes on
    import. Verify the patch is actually applied — same defense-in-depth
    contract as Jellyfin's HTTP guard."""
    import plex_client
    from plexapi.video import Episode, Movie, Season, Show
    for cls in (Episode, Movie, Season, Show):
        check(
            f"plex safety: {cls.__name__}.delete is the refuse function",
            cls.delete is plex_client._refuse_delete,
            f"got {cls.delete!r}",
        )


# --------------------------------------------------------------------------- #
# Cross-backend title matching (for "Both"-mode show bridging)
# --------------------------------------------------------------------------- #


def test_normalize_title_basic():
    from media_client import normalize_title
    check("normalize: lowercases", normalize_title("Breaking Bad") == "breaking bad")
    check("normalize: strips punctuation", normalize_title("Mr. Robot!") == "mr robot")
    check("normalize: collapses whitespace", normalize_title("  Foo   Bar  ") == "foo bar")
    check("normalize: empty -> empty", normalize_title("") == "")
    check("normalize: None -> empty", normalize_title(None) == "")


def test_titles_match_case_and_punctuation():
    from media_client import titles_match
    check("match: case-insensitive", titles_match("Breaking Bad", "breaking bad"))
    check("match: punctuation ignored", titles_match("Mr. Robot", "Mr Robot"))
    check("match: different shows reject", not titles_match("The Office", "The Office (US)"))


def test_titles_match_year_disambiguation():
    from media_client import titles_match
    # Same title, both years known + disagree → not a match
    check("match: year disagreement rejects", not titles_match("The Office", "The Office", 2001, 2005))
    # Same title, both years known + agree → match
    check("match: year agreement accepts", titles_match("The Office", "The Office", 2005, 2005))
    # Same title, one year unknown → still a match (don't punish missing data)
    check("match: missing year is permissive", titles_match("Breaking Bad", "Breaking Bad", 2008, None))
    check("match: both years unknown is permissive", titles_match("Breaking Bad", "Breaking Bad", None, None))


def test_titles_match_handles_none():
    from media_client import titles_match
    check("match: None title rejects", not titles_match(None, "Anything"))
    check("match: empty title rejects", not titles_match("", "Anything"))


# --------------------------------------------------------------------------- #
# Service-layer dispatch (ShowConfig + backend routing helpers)
# --------------------------------------------------------------------------- #


def test_show_config_back_compat_plex_id():
    """Legacy callers pass only rating_key for a Plex show. __post_init__
    should mirror it into plex_rating_key so the new dispatch code finds it."""
    import service
    cfg = service.ShowConfig(rating_key="12345", title="Test")
    check("ShowConfig back-compat: numeric rating_key -> plex_rating_key",
          cfg.plex_rating_key == "12345" and cfg.jellyfin_rating_key is None)


def test_show_config_back_compat_non_numeric():
    """Non-numeric rating_key (e.g. a Jellyfin GUID) should NOT auto-fill plex_rating_key."""
    import service
    cfg = service.ShowConfig(rating_key="abc-def-1234", title="Test")
    check("ShowConfig: non-numeric rating_key doesn't auto-fill plex_rating_key",
          cfg.plex_rating_key is None and cfg.jellyfin_rating_key is None)


def test_show_config_explicit_jellyfin():
    """Explicit jellyfin_rating_key passes through."""
    import service
    cfg = service.ShowConfig(
        rating_key="abc-def",
        title="Test",
        jellyfin_rating_key="abc-def",
        jellyfin_movie_rating_keys=["m-1", "m-2"],
    )
    check("ShowConfig: explicit jellyfin fields preserved",
          cfg.jellyfin_rating_key == "abc-def" and cfg.jellyfin_movie_rating_keys == ["m-1", "m-2"])


def test_show_config_id_for():
    """id_for(backend) dispatches to the right field."""
    import service
    cfg = service.ShowConfig(
        rating_key="12345",
        title="Test",
        plex_rating_key="12345",
        jellyfin_rating_key="abc-def",
        movie_rating_keys=["m-px-1"],
        jellyfin_movie_rating_keys=["m-jf-1"],
    )
    check("id_for('plex')", cfg.id_for("plex") == "12345")
    check("id_for('jellyfin')", cfg.id_for("jellyfin") == "abc-def")
    check("movie_ids_for('plex')", cfg.movie_ids_for("plex") == ["m-px-1"])
    check("movie_ids_for('jellyfin')", cfg.movie_ids_for("jellyfin") == ["m-jf-1"])


def test_show_config_id_for_missing_side():
    """id_for returns None when the show isn't matched on that backend."""
    import service
    cfg = service.ShowConfig(rating_key="12345", title="Test", plex_rating_key="12345")
    check("id_for missing-side returns None", cfg.id_for("jellyfin") is None)
    check("movie_ids_for missing-side returns []", cfg.movie_ids_for("jellyfin") == [])


def test_backends_for_dispatch():
    """_backends_for expands 'both' into a list, single-backend stays single."""
    import service

    class FakeRow:
        def __init__(self, backend):
            self._b = backend
        def __getitem__(self, k):
            if k == "backend":
                return self._b
            raise KeyError(k)
        def keys(self):
            return ("backend",)

    check("_backends_for('plex')", service._backends_for(FakeRow("plex")) == ["plex"])
    check("_backends_for('jellyfin')", service._backends_for(FakeRow("jellyfin")) == ["jellyfin"])
    check("_backends_for('both')", service._backends_for(FakeRow("both")) == ["plex", "jellyfin"])


def test_find_match_uses_titles_match():
    """_find_match should respect titles_match's year disambiguation."""
    import service
    from media_client import ShowSummary

    cands = [
        ShowSummary("rk-1", "The Office", 2001, "BBC", None),       # UK version
        ShowSummary("rk-2", "The Office", 2005, "US Lib", None),    # US version
        ShowSummary("rk-3", "Breaking Bad", 2008, "Lib", None),
    ]
    check("_find_match: year disambiguates",
          service._find_match(cands, "The Office", 2005) == "rk-2")
    check("_find_match: different show -> None",
          service._find_match(cands, "Better Call Saul", None) is None)
    check("_find_match: title-only match when year unknown",
          # When asker has no year, first candidate with matching title wins
          service._find_match(cands, "The Office", None) == "rk-1")


# --------------------------------------------------------------------------- #
# v1.2.0 — per-episode exclusions (parse/serialize round-trips)
# --------------------------------------------------------------------------- #


def test_parse_excluded_episodes_basic():
    from service import _parse_excluded_episodes
    check("parse: empty -> empty set", _parse_excluded_episodes("") == set())
    check("parse: None -> empty set", _parse_excluded_episodes(None) == set())
    check("parse: single 'S:E'",
          _parse_excluded_episodes("1:1") == {(1, 1)})
    check("parse: multiple comma-separated",
          _parse_excluded_episodes("1:1,3:14,5:6") == {(1, 1), (3, 14), (5, 6)})


def test_parse_excluded_episodes_tolerant():
    from service import _parse_excluded_episodes
    check("parse: handles whitespace",
          _parse_excluded_episodes(" 1:1 , 2:3 ") == {(1, 1), (2, 3)})
    check("parse: handles trailing comma",
          _parse_excluded_episodes("1:1,") == {(1, 1)})
    check("parse: skips malformed tokens",
          _parse_excluded_episodes("1:1,bogus,2:3") == {(1, 1), (2, 3)})


def test_show_config_excluded_csv_roundtrip():
    """ShowConfig.excluded_csv -> _parse_excluded_episodes round-trip."""
    import service
    cfg = service.ShowConfig(
        rating_key="12345", title="Test",
        excluded_episodes={(1, 1), (1, 2), (3, 14)},
    )
    csv = cfg.excluded_csv
    # Sorted output keeps the CSV stable for diff/version control friendliness.
    check("excluded_csv: sorted", csv == "1:1,1:2,3:14", f"got {csv!r}")
    check("excluded_csv: round-trips through parser",
          service._parse_excluded_episodes(csv) == cfg.excluded_episodes)


def test_show_config_excluded_default_empty():
    import service
    cfg = service.ShowConfig(rating_key="12345", title="Test")
    check("ShowConfig: excluded_episodes defaults to empty set",
          cfg.excluded_episodes == set())
    check("ShowConfig: excluded_csv on empty is ''",
          cfg.excluded_csv == "")


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #


def main() -> int:
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        try:
            t()
        except Exception as e:
            _results.append((False, f"{t.__name__} raised: {e!r}"))

    pass_count = sum(1 for ok, _ in _results if ok)
    fail_count = sum(1 for ok, _ in _results if not ok)

    for ok, name in _results:
        print(f"{'PASS' if ok else 'FAIL'}  {name}")

    print()
    print(f"  {pass_count} passed, {fail_count} failed, {len(_results)} total")
    return 0 if fail_count == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
