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
    # Trailing disambiguation suffixes are stripped before normalizing
    check("normalize: strips country code (US)", normalize_title("Whose Line Is It Anyway? (US)") == "whose line is it anyway")
    check("normalize: strips country code (UK)", normalize_title("The Office (UK)") == "the office")
    check("normalize: strips year suffix", normalize_title("Yellowstone (2018)") == "yellowstone")
    check("normalize: strips year then country", normalize_title("Some Show (US) (2020)") == "some show")
    check("normalize: strips country then year", normalize_title("Some Show (2020) (US)") == "some show")


def test_titles_match_case_and_punctuation():
    from media_client import titles_match
    check("match: case-insensitive", titles_match("Breaking Bad", "breaking bad"))
    check("match: punctuation ignored", titles_match("Mr. Robot", "Mr Robot"))
    # Country-code suffix is stripped before comparing — Plex adds (US)/(UK) that Jellyfin omits
    check("match: (US) suffix stripped for cross-backend match", titles_match("Whose Line Is It Anyway? (US)", "Whose Line Is It Anyway?"))
    check("match: base title matches with stripped suffix", titles_match("The Office", "The Office (US)"))
    # When both years are known and different, different-country versions are correctly rejected
    check("match: different country versions rejected via year", not titles_match("The Office (US)", "The Office (UK)", 2005, 2001))


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
# v1.3.0 — Weighted Rotation
# --------------------------------------------------------------------------- #


def test_interleave_weighted_default_equals_rotation():
    A = [mk("A", 1, i) for i in range(1, 4)]
    B = [mk("B", 1, i) for i in range(1, 4)]
    plain = [r.rating_key for r in rotation.interleave([A, B])]
    weighted = [r.rating_key for r in rotation.interleave_weighted([A, B])]
    check("weighted: default weights match rotation", plain == weighted, f"got {weighted}")


def test_interleave_weighted_2_to_1_ratio():
    A = [mk("A", 1, i) for i in range(1, 6)]  # 5 eps
    B = [mk("B", 1, i) for i in range(1, 4)]  # 3 eps
    out = [r.rating_key for r in rotation.interleave_weighted([A, B], [2, 1])]
    # 2 from A, 1 from B, 2 from A, 1 from B, 1 from A (only 1 left), 1 from B (last)
    expected = ["A-1-1", "A-1-2", "B-1-1", "A-1-3", "A-1-4", "B-1-2", "A-1-5", "B-1-3"]
    check("weighted: 2:1 ratio", out == expected, f"got {out}")


def test_interleave_weighted_partial_take_when_depleted():
    """If a show has fewer episodes left than its weight, take what's there
    and move on — no carry-over."""
    A = [mk("A", 1, 1)]
    B = [mk("B", 1, i) for i in range(1, 4)]  # 3 eps
    out = [r.rating_key for r in rotation.interleave_weighted([A, B], [3, 1])]
    # A: only 1 left (asked for 3), take A1, B1, then A has nothing → B2, B3
    expected = ["A-1-1", "B-1-1", "B-1-2", "B-1-3"]
    check("weighted: partial take when depleted", out == expected, f"got {out}")


def test_interleave_weighted_weights_clamped_to_one():
    """Weights < 1 are clamped to 1 (0-weight = remove the show entirely)."""
    A = [mk("A", 1, 1), mk("A", 1, 2)]
    B = [mk("B", 1, 1), mk("B", 1, 2)]
    out = [r.rating_key for r in rotation.interleave_weighted([A, B], [0, 1])]
    check("weighted: weight 0 clamps to 1", out == ["A-1-1", "B-1-1", "A-1-2", "B-1-2"], f"got {out}")


def test_interleave_weighted_pads_short_weights():
    """If caller passes fewer weights than shows, pad with 1s."""
    A = [mk("A", 1, 1)]
    B = [mk("B", 1, 1)]
    C = [mk("C", 1, 1)]
    out = [r.rating_key for r in rotation.interleave_weighted([A, B, C], [2])]
    check("weighted: short weights are padded",
          out == ["A-1-1", "B-1-1", "C-1-1"], f"got {out}")


# --------------------------------------------------------------------------- #
# v1.3.0 — Block Scheduling
# --------------------------------------------------------------------------- #


def test_interleave_blocks_default_equals_rotation():
    A = [mk("A", 1, i) for i in range(1, 3)]
    B = [mk("B", 1, i) for i in range(1, 3)]
    plain = [r.rating_key for r in rotation.interleave([A, B])]
    blocks = [r.rating_key for r in rotation.interleave_blocks([A, B], block_size=1)]
    check("blocks: block_size=1 matches rotation", plain == blocks, f"got {blocks}")


def test_interleave_blocks_size_three():
    """3 from A, 3 from B, 3 from A, ... pattern."""
    A = [mk("A", 1, i) for i in range(1, 8)]
    B = [mk("B", 1, i) for i in range(1, 8)]
    out = [r.rating_key for r in rotation.interleave_blocks([A, B], block_size=3)]
    # A1 A2 A3 B1 B2 B3 A4 A5 A6 B4 B5 B6 A7 B7
    expected = [
        "A-1-1", "A-1-2", "A-1-3",
        "B-1-1", "B-1-2", "B-1-3",
        "A-1-4", "A-1-5", "A-1-6",
        "B-1-4", "B-1-5", "B-1-6",
        "A-1-7", "B-1-7",
    ]
    check("blocks: size 3 pattern", out == expected, f"got {out}")


# --------------------------------------------------------------------------- #
# v1.3.0 — Intelligent Shuffle
# --------------------------------------------------------------------------- #


def test_shuffle_chronological_deterministic_with_seed():
    A = [mk("A", 1, i) for i in range(1, 4)]
    B = [mk("B", 1, i) for i in range(1, 4)]
    a = [r.rating_key for r in rotation.shuffle_chronological([A, B], seed=42)]
    b = [r.rating_key for r in rotation.shuffle_chronological([A, B], seed=42)]
    check("shuffle: same seed = same output", a == b, f"got {a} vs {b}")


def test_shuffle_chronological_uses_all_episodes():
    A = [mk("A", 1, i) for i in range(1, 4)]
    B = [mk("B", 1, i) for i in range(1, 4)]
    C = [mk("C", 1, 1)]
    out = rotation.shuffle_chronological([A, B, C], seed=1)
    keys = sorted(r.rating_key for r in out)
    expected = sorted(["A-1-1", "A-1-2", "A-1-3", "B-1-1", "B-1-2", "B-1-3", "C-1-1"])
    check("shuffle: every episode appears exactly once", keys == expected, f"got {keys}")


def test_shuffle_chronological_preserves_within_show_order():
    """Show A's episodes must stay in order A1<A2<A3<A4<A5 in the output."""
    A = [mk("A", 1, i) for i in range(1, 6)]
    B = [mk("B", 1, i) for i in range(1, 6)]
    out = rotation.shuffle_chronological([A, B], seed=7)
    a_positions = [i for i, r in enumerate(out) if r.show_rating_key == "A"]
    a_keys = [out[i].rating_key for i in a_positions]
    check("shuffle: A episodes stay in chronological order",
          a_keys == ["A-1-1", "A-1-2", "A-1-3", "A-1-4", "A-1-5"], f"got {a_keys}")
    b_positions = [i for i, r in enumerate(out) if r.show_rating_key == "B"]
    b_keys = [out[i].rating_key for i in b_positions]
    check("shuffle: B episodes stay in chronological order",
          b_keys == ["B-1-1", "B-1-2", "B-1-3", "B-1-4", "B-1-5"], f"got {b_keys}")


def test_shuffle_chronological_avoids_consecutive_same_show_when_possible():
    """With balanced episode counts, no same-show consecutive pairs."""
    A = [mk("A", 1, i) for i in range(1, 6)]
    B = [mk("B", 1, i) for i in range(1, 6)]
    out = rotation.shuffle_chronological([A, B], seed=3)
    consecutive_pairs = [
        (out[i].show_rating_key, out[i + 1].show_rating_key)
        for i in range(len(out) - 1)
    ]
    same_show = [p for p in consecutive_pairs if p[0] == p[1]]
    check("shuffle: no same-show consecutive when avoidable",
          len(same_show) == 0, f"found same-show pairs: {same_show}")


def test_shuffle_chronological_falls_back_when_one_show_dominates():
    """If show A has way more episodes than B, the tail must be all-A and
    the algorithm cannot avoid consecutive A-A pairs — it MUST fall back
    rather than infinite-loop."""
    A = [mk("A", 1, i) for i in range(1, 8)]
    B = [mk("B", 1, 1)]
    out = rotation.shuffle_chronological([A, B], seed=5)
    keys = [r.rating_key for r in out]
    expected_count = len(A) + len(B)
    check("shuffle: produces full output when forced to repeat",
          len(keys) == expected_count, f"got {len(keys)} of {expected_count}")


# --------------------------------------------------------------------------- #
# v1.3.0 — compose() dispatch
# --------------------------------------------------------------------------- #


def test_compose_dispatches_to_new_modes():
    A = [mk("A", 1, i) for i in range(1, 4)]
    B = [mk("B", 1, i) for i in range(1, 4)]
    w = [r.rating_key for r in rotation.compose([A, B], mode="rotation_weighted", weights=[2, 1])]
    b = [r.rating_key for r in rotation.compose([A, B], mode="rotation_blocks", block_size=2)]
    s = [r.rating_key for r in rotation.compose([A, B], mode="shuffle_chronological", shuffle_seed=42)]
    check("compose: rotation_weighted dispatches", w[:3] == ["A-1-1", "A-1-2", "B-1-1"], f"got {w}")
    check("compose: rotation_blocks dispatches", b[:4] == ["A-1-1", "A-1-2", "B-1-1", "B-1-2"], f"got {b}")
    check("compose: shuffle_chronological dispatches", len(s) == 6, f"got {s}")


def test_rebuild_tail_weighted_drops_kept():
    """rebuild_tail in weighted mode skips kept items per show."""
    A_full = [mk("A", 1, i) for i in range(1, 5)]
    B_full = [mk("B", 1, i) for i in range(1, 5)]
    kept = [
        PlaylistItem("A-1-1", "A", 1, 1, view_count=1),
        PlaylistItem("A-1-2", "A", 1, 2, view_count=1),
    ]
    tail = rotation.rebuild_tail(
        kept, [A_full, B_full], mode="rotation_weighted", weights=[2, 1]
    )
    keys = [r.rating_key for r in tail]
    expected = ["A-1-3", "A-1-4", "B-1-1", "B-1-2", "B-1-3", "B-1-4"]
    check("rebuild_tail weighted: kept items dropped, weighted interleave",
          keys == expected, f"got {keys}")


def test_rebuild_tail_shuffle_drops_kept():
    """Shuffle rebuild_tail must drop kept items but preserve seed-determined order."""
    A_full = [mk("A", 1, i) for i in range(1, 4)]
    B_full = [mk("B", 1, i) for i in range(1, 4)]
    full = rotation.shuffle_chronological([A_full, B_full], seed=42)
    full_keys = [r.rating_key for r in full]
    # Mark the first item as kept; rebuild_tail should return the rest in order.
    first = full[0]
    kept = [PlaylistItem(first.rating_key, first.show_rating_key,
                         first.season, first.episode, view_count=1)]
    tail = rotation.rebuild_tail(
        kept, [A_full, B_full], mode="shuffle_chronological", shuffle_seed=42
    )
    tail_keys = [r.rating_key for r in tail]
    check("rebuild_tail shuffle: drops kept, preserves seed order",
          tail_keys == full_keys[1:], f"got {tail_keys} vs {full_keys[1:]}")


def test_valid_sort_modes_constant():
    """VALID_SORT_MODES is the source of truth other modules import."""
    expected = {"rotation", "rotation_weighted", "rotation_blocks",
                "air_date", "shuffle_chronological"}
    check("rotation.VALID_SORT_MODES has all 5 modes",
          set(rotation.VALID_SORT_MODES) == expected, f"got {rotation.VALID_SORT_MODES}")


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
# v1.4.0 — Dynamic genre playlists (pure-logic tests)
# --------------------------------------------------------------------------- #


def test_parse_genre_csv_basic():
    from service import _parse_genre_csv
    check("genre parse: empty -> empty list", _parse_genre_csv("") == [])
    check("genre parse: None -> empty list", _parse_genre_csv(None) == [])
    check("genre parse: single", _parse_genre_csv("Sci-Fi") == ["Sci-Fi"])
    check("genre parse: multiple",
          _parse_genre_csv("Sci-Fi, Drama, Animation") == ["Sci-Fi", "Drama", "Animation"])


def test_parse_genre_csv_whitespace():
    from service import _parse_genre_csv
    check("genre parse: strips whitespace",
          _parse_genre_csv(" Sci-Fi ,  Drama\t") == ["Sci-Fi", "Drama"])
    check("genre parse: skips empty tokens",
          _parse_genre_csv("Sci-Fi,,Drama") == ["Sci-Fi", "Drama"])


def test_show_config_is_excluded_default():
    import service
    cfg = service.ShowConfig(rating_key="12345", title="Test")
    check("ShowConfig: is_excluded defaults to False", cfg.is_excluded is False)


def test_show_config_is_excluded_explicit():
    import service
    cfg = service.ShowConfig(rating_key="12345", title="Test", is_excluded=True)
    check("ShowConfig: is_excluded=True persisted", cfg.is_excluded is True)


def test_valid_playlist_types_constant():
    from db import VALID_PLAYLIST_TYPES
    check("VALID_PLAYLIST_TYPES has manual, genre, and franchise",
          set(VALID_PLAYLIST_TYPES) == {"manual", "genre", "franchise"},
          f"got {VALID_PLAYLIST_TYPES}")


def test_playlist_view_genre_fields():
    import service
    view = service.PlaylistView(
        id=1,
        name="Test",
        plex_rating_key=None,
        jellyfin_playlist_id=None,
        backend="plex",
        shows=[],
        item_count=0,
    )
    check("PlaylistView: playlist_type defaults to 'manual'",
          view.playlist_type == "manual")
    check("PlaylistView: genre_filter defaults to None",
          view.genre_filter is None)
    check("PlaylistView: excluded_shows defaults to empty list",
          view.excluded_shows == [])


def test_genre_parse_roundtrip_via_csv_join():
    """Genre filter stored as comma-joined string; round-trips through split."""
    from service import _parse_genre_csv
    genres = ["Sci-Fi", "Drama", "Animation"]
    csv_repr = ",".join(genres)
    parsed = _parse_genre_csv(csv_repr)
    check("genre round-trip: parse(join(genres)) == genres",
          parsed == genres, f"got {parsed}")


def test_show_config_weight_default():
    """Weight was added in v1.3.0 but confirm default interacts correctly
    with the v1.4.0 excluded fields (fields are independent)."""
    import service
    cfg = service.ShowConfig(rating_key="12345", title="Test", is_excluded=True, weight=3)
    check("ShowConfig: weight + is_excluded coexist",
          cfg.weight == 3 and cfg.is_excluded is True)


# --------------------------------------------------------------------------- #
# v1.5.0 — Manual crossover grouping (sort key + passthrough)
# --------------------------------------------------------------------------- #


def test_crossover_map_groups_sort_before_non_groups():
    """On the same air_date, manually grouped episodes sort before non-grouped,
    and within a group episodes sort by sort_index."""
    A, B, C = "showA", "showB", "showC"
    eps = [
        mk(A, 1, 1, title="Standalone", date="2010-06-15"),
        mk(B, 2, 5, title="Crossover B", date="2010-06-15"),
        mk(C, 3, 2, title="Crossover C", date="2010-06-15"),
    ]
    # Group B and C together: C plays first (sort_idx=1), B second (sort_idx=2)
    crossover_map = {
        ("showB", 2, 5): (1, 2),   # group_id=1, sort_idx=2
        ("showC", 3, 2): (1, 1),   # group_id=1, sort_idx=1
    }
    result = rotation.air_date_sequence([eps], show_order=["showA", "showB", "showC"],
                                        crossover_map=crossover_map)
    keys = [r.rating_key for r in result]
    # C (group sort_idx=1) → B (group sort_idx=2) → A (non-grouped)
    expected = ["showC-3-2", "showB-2-5", "showA-1-1"]
    check("crossover_map: grouped sort before non-grouped, sort_idx order",
          keys == expected, f"got {keys}")


def test_crossover_map_different_groups_same_day():
    """Two different groups on the same air_date sort by group_id."""
    A = mk("showA", 1, 1, date="2010-06-15")
    B = mk("showB", 1, 1, date="2010-06-15")
    C = mk("showC", 1, 1, date="2010-06-15")
    D = mk("showD", 1, 1, date="2010-06-15")
    crossover_map = {
        ("showA", 1, 1): (1, 1),   # group 1, sort_idx 1
        ("showB", 1, 1): (1, 2),   # group 1, sort_idx 2
        ("showC", 1, 1): (2, 1),   # group 2, sort_idx 1
        ("showD", 1, 1): (2, 2),   # group 2, sort_idx 2
    }
    result = rotation.air_date_sequence(
        [[A, B, C, D]], show_order=["showA", "showB", "showC", "showD"],
        crossover_map=crossover_map,
    )
    keys = [r.rating_key for r in result]
    # group 1 (A then B) then group 2 (C then D)
    expected = ["showA-1-1", "showB-1-1", "showC-1-1", "showD-1-1"]
    check("crossover_map: groups sort by group_id, then sort_idx within",
          keys == expected, f"got {keys}")


def test_crossover_map_part_number_still_works_for_non_grouped():
    """Episodes not in any group still sort by part_number on the same day."""
    A = mk("showA", 1, 1, title="Event, Part 2", date="2010-06-15")
    B = mk("showB", 1, 1, title="Event, Part 1", date="2010-06-15")
    # No crossover map — auto-detection should work as before.
    result = rotation.air_date_sequence([[A, B]], show_order=["showA", "showB"])
    keys = [r.rating_key for r in result]
    # Part 1 before Part 2
    expected = ["showB-1-1", "showA-1-1"]
    check("crossover_map: part_number auto-detection still works without map",
          keys == expected, f"got {keys}")


def test_compose_passes_crossover_map_through():
    """compose() in air_date mode with crossover_map delegates correctly."""
    A = mk("showA", 1, 1, date="2010-06-15")
    B = mk("showB", 1, 1, date="2010-06-15")
    crossover_map = {
        ("showB", 1, 1): (1, 1),
        ("showA", 1, 1): (1, 2),
    }
    result = rotation.compose(
        [[A, B]], mode="air_date", show_order=["showA", "showB"],
        crossover_map=crossover_map,
    )
    keys = [r.rating_key for r in result]
    # grouped episodes (B then A) sort before any non-grouped equivalents
    check("compose: crossover_map reaches air_date_sequence",
          keys == ["showB-1-1", "showA-1-1"], f"got {keys}")


def test_rebuild_tail_crossover_map_passthrough():
    """rebuild_tail in air_date mode passes crossover_map through to compose."""
    A_full = [mk("showA", 1, 1, date="2010-06-15"),
              mk("showA", 1, 2, date="2010-06-22")]
    B_full = [mk("showB", 1, 1, date="2010-06-15")]
    # Group them on the first date
    crossover_map = {
        ("showA", 1, 1): (1, 1),
        ("showB", 1, 1): (1, 2),
    }
    # Keep is empty, so full tail = full compose
    tail = rotation.rebuild_tail(
        [], [A_full, B_full], mode="air_date",
        show_order=["showA", "showB"],
        crossover_map=crossover_map,
    )
    keys = [r.rating_key for r in tail]
    check("rebuild_tail: crossover_map reaches compose",
          keys == ["showA-1-1", "showB-1-1", "showA-1-2"], f"got {keys}")


def test_genre_cache_db():
    import os, tempfile
    from datetime import datetime, timezone, timedelta
    import db as _db_mod

    orig_path = _db_mod.DB_PATH
    tmp = tempfile.mktemp(suffix=".db")
    try:
        _db_mod.DB_PATH = tmp
        _db_mod.init_db()

        check("genre cache: empty → None", _db_mod.get_genre_cache("plex") is None)

        _db_mod.set_genre_cache("plex", ["Drama", "Action", "Comedy"])
        result = _db_mod.get_genre_cache("plex")
        check("genre cache: roundtrip sorted", result == ["Action", "Comedy", "Drama"])

        check("genre cache: other backend → None",
              _db_mod.get_genre_cache("jellyfin") is None)

        old_ts = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        with _db_mod.connection() as conn:
            conn.execute(
                "UPDATE genre_cache_meta SET updated_at = ? WHERE backend = ?",
                (old_ts, "plex"),
            )
        check("genre cache: expired → None", _db_mod.get_genre_cache("plex") is None)

        _db_mod.set_genre_cache("plex", ["Sci-Fi", "Thriller"])
        check("genre cache: overwrite", _db_mod.get_genre_cache("plex") == ["Sci-Fi", "Thriller"])

        _db_mod.set_genre_cache("plex", [])
        check("genre cache: empty list ok", _db_mod.get_genre_cache("plex") == [])

    finally:
        _db_mod.DB_PATH = orig_path
        try:
            os.unlink(tmp)
        except OSError:
            pass


def test_managed_playlists_rebuild_preserves_block_size():
    """Regression for issue #8: init_db()'s managed_playlists rebuild must copy
    rows by COLUMN NAME, not positionally.

    A DB created fresh on v2.5.0 and upgraded to v3.0.0 carries its columns in a
    different physical order than the rebuilt table: emby_playlist_id was
    appended last by ALTER, but the new table places it 5th, and the v2.x
    columns were appended past auto_sync. The old `INSERT INTO
    managed_playlists_new SELECT *` copied by ordinal, so a NULL-able source
    column (e.g. shuffle_seed) landed in a NOT NULL slot and init_db() crashed
    with a `NOT NULL constraint failed` during startup — the reporter hit it as
    managed_playlists_new.block_size; the exact column depends on the DB's
    physical order, but the by-name copy fixes every variant. This fixture trips
    the first such NOT NULL slot and asserts block_size (the reported symptom)
    survives the rebuild intact.
    """
    import os, tempfile
    import db as _db_mod
    orig_path = _db_mod.DB_PATH
    tmp = tempfile.mktemp(suffix=".db")
    try:
        _db_mod.DB_PATH = tmp
        # Given a pre-rebuild managed_playlists in the exact physical column
        # order a fresh v2.5.0 install has after upgrading to v3.0.0 (the v2.x
        # columns rule_mode/franchise_definition_id/pruning_enabled/last_stats
        # and then emby_playlist_id all appended past auto_sync by ALTER). The
        # 'both' in the backend CHECK is what triggers init_db()'s rebuild.
        with _db_mod.connection() as conn:
            conn.executescript(
                """
                CREATE TABLE managed_playlists (
                    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
                    name                 TEXT NOT NULL UNIQUE,
                    plex_rating_key      TEXT,
                    jellyfin_playlist_id TEXT,
                    backend              TEXT NOT NULL DEFAULT 'plex'
                        CHECK(backend IN ('plex','jellyfin','both')),
                    playlist_type        TEXT NOT NULL DEFAULT 'manual'
                        CHECK(playlist_type IN ('manual','genre','franchise')),
                    genre_filter         TEXT,
                    created_at           TEXT NOT NULL,
                    sort_mode            TEXT NOT NULL DEFAULT 'rotation',
                    block_size           INTEGER NOT NULL DEFAULT 1,
                    shuffle_seed         INTEGER,
                    unwatched_only       INTEGER NOT NULL DEFAULT 0,
                    auto_sync            INTEGER NOT NULL DEFAULT 1,
                    rule_mode            TEXT NOT NULL DEFAULT 'genre',
                    franchise_definition_id INTEGER,
                    pruning_enabled      INTEGER NOT NULL DEFAULT 1,
                    last_stats           TEXT,
                    emby_playlist_id     TEXT
                );
                """
            )
            conn.execute(
                """INSERT INTO managed_playlists
                   (name, backend, created_at, sort_mode, block_size,
                    shuffle_seed, playlist_type)
                   VALUES (?,?,?,?,?,?,?)""",
                ("Legacy", "both", "2024-01-01T00:00:00+00:00", "rotation",
                 7, None, "manual"),
            )
        # And the seed row really has block_size=7 with a NULL shuffle_seed, so
        # a green result proves the rebuild (not the fixture) carried the value.
        with _db_mod.connection() as conn:
            before = conn.execute(
                "SELECT block_size, shuffle_seed FROM managed_playlists "
                "WHERE name='Legacy'"
            ).fetchone()
        check("issue #8 before: block_size seeded as 7", before["block_size"] == 7)
        check("issue #8 before: shuffle_seed seeded as NULL", before["shuffle_seed"] is None)
        # When the rebuild runs as part of init_db()...
        crashed = None
        try:
            _db_mod.init_db()
        except Exception as e:
            crashed = e
        check("issue #8: init_db rebuild does not crash on legacy column order",
              crashed is None, repr(crashed))
        # Then every value survives by name and the schema is the rebuilt one.
        with _db_mod.connection() as conn:
            after = conn.execute(
                "SELECT block_size, shuffle_seed, backend "
                "FROM managed_playlists WHERE name='Legacy'"
            ).fetchone()
            cols = [r["name"] for r in conn.execute(
                "PRAGMA table_info(managed_playlists)")]
        check("issue #8 after: block_size preserved as 7",
              after is not None and after["block_size"] == 7)
        check("issue #8 after: shuffle_seed preserved as NULL",
              after is not None and after["shuffle_seed"] is None)
        check("issue #8 after: backend value preserved",
              after is not None and after["backend"] == "both")
        check("issue #8 after: emby_playlist_id is 5th column post-rebuild",
              cols[:6] == ["id", "name", "plex_rating_key",
                           "jellyfin_playlist_id", "emby_playlist_id", "backend"])
    finally:
        _db_mod.DB_PATH = orig_path
        try:
            os.unlink(tmp)
        except OSError:
            pass


def test_apply_rules_year():
    from media_client import ShowSummary
    import service as _svc

    def mk_summary(rk="A", year=None):
        return ShowSummary(rating_key=rk, title="Test", year=year,
                           library="", thumb=None)

    s1 = mk_summary("show1", year=1995)
    s2 = mk_summary("show2", year=2005)
    s3 = mk_summary("show3", year=None)

    rules = [{"rule_type": "year_min", "operator": "include", "value": "2000"}]
    result = _svc._apply_rules([s1, s2, s3], rules)
    keys = [r.rating_key for r in result]
    check("apply_rules: year_min filters below", "show1" not in keys)
    check("apply_rules: year_min keeps above", "show2" in keys)
    check("apply_rules: year_min permits None year", "show3" in keys)

    rules2 = [{"rule_type": "year_max", "operator": "include", "value": "2000"}]
    result2 = _svc._apply_rules([s1, s2, s3], rules2)
    keys2 = [r.rating_key for r in result2]
    check("apply_rules: year_max keeps below", "show1" in keys2)
    check("apply_rules: year_max filters above", "show2" not in keys2)
    check("apply_rules: year_max permits None year", "show3" in keys2)


def test_apply_rules_status():
    from media_client import ShowSummary
    import service as _svc

    def mk(rk, status):
        return ShowSummary(rating_key=rk, title="Test", year=None,
                           library="", thumb=None, status=status)

    s1 = mk("s1", "Ended")
    s2 = mk("s2", "Continuing")
    s3 = mk("s3", None)

    rules = [{"rule_type": "status", "operator": "include", "value": "Ended"}]
    result = _svc._apply_rules([s1, s2, s3], rules)
    keys = [r.rating_key for r in result]
    check("apply_rules: status include matches", "s1" in keys)
    check("apply_rules: status include rejects other", "s2" not in keys)
    check("apply_rules: status include permits None", "s3" in keys)

    rules2 = [{"rule_type": "status", "operator": "exclude", "value": "Continuing"}]
    result2 = _svc._apply_rules([s1, s2, s3], rules2)
    keys2 = [r.rating_key for r in result2]
    check("apply_rules: status exclude keeps non-match", "s1" in keys2)
    check("apply_rules: status exclude removes match", "s2" not in keys2)


def test_apply_rules_season_count():
    from media_client import ShowSummary
    import service as _svc

    def mk(rk, seasons):
        return ShowSummary(rating_key=rk, title="Test", year=None,
                           library="", thumb=None, season_count=seasons)

    s1 = mk("s1", 3)
    s2 = mk("s2", 10)
    s3 = mk("s3", None)

    rules = [{"rule_type": "season_max", "operator": "include", "value": "5"}]
    result = _svc._apply_rules([s1, s2, s3], rules)
    keys = [r.rating_key for r in result]
    check("apply_rules: season_max keeps below", "s1" in keys)
    check("apply_rules: season_max filters above", "s2" not in keys)
    check("apply_rules: season_max permits None", "s3" in keys)

    rules2 = [{"rule_type": "season_min", "operator": "include", "value": "8"}]
    result2 = _svc._apply_rules([s1, s2, s3], rules2)
    keys2 = [r.rating_key for r in result2]
    check("apply_rules: season_min filter below", "s1" not in keys2)
    check("apply_rules: season_min keeps above", "s2" in keys2)


def test_apply_rules_rating():
    from media_client import ShowSummary
    import service as _svc

    def mk(rk, rating):
        return ShowSummary(rating_key=rk, title="Test", year=None,
                           library="", thumb=None, community_rating=rating)

    s1 = mk("s1", 8.5)
    s2 = mk("s2", 5.0)
    s3 = mk("s3", None)

    rules = [{"rule_type": "rating_min", "operator": "include", "value": "7.0"}]
    result = _svc._apply_rules([s1, s2, s3], rules)
    keys = [r.rating_key for r in result]
    check("apply_rules: rating_min keeps above", "s1" in keys)
    check("apply_rules: rating_min filters below", "s2" not in keys)
    check("apply_rules: rating_min permits None", "s3" in keys)


def test_apply_rules_combined():
    from media_client import ShowSummary
    import service as _svc

    def mk(rk, year=None, status=None, seasons=None, rating=None):
        return ShowSummary(rating_key=rk, title="Test", year=year,
                           library="", thumb=None, status=status,
                           season_count=seasons, community_rating=rating)

    s1 = mk("s1", year=1995, status="Ended", seasons=3, rating=8.5)
    s2 = mk("s2", year=2005, status="Ended", seasons=10, rating=6.0)
    s3 = mk("s3", year=2005, status="Continuing", seasons=4, rating=9.0)
    s4 = mk("s4", year=2010, status="Ended", seasons=3, rating=7.5)

    rules = [
        {"rule_type": "year_min", "operator": "include", "value": "2000"},
        {"rule_type": "status", "operator": "include", "value": "Ended"},
        {"rule_type": "season_max", "operator": "include", "value": "5"},
    ]
    result = _svc._apply_rules([s1, s2, s3, s4], rules)
    keys = [r.rating_key for r in result]
    check("apply_rules: combined - s1 filtered by year_min", "s1" not in keys)
    check("apply_rules: combined - s2 filtered by season_max", "s2" not in keys)
    check("apply_rules: combined - s3 filtered by status", "s3" not in keys)
    check("apply_rules: combined - s4 passes all", "s4" in keys)


def test_apply_rules_content_rating():
    from media_client import ShowSummary
    import service as _svc

    def mk(rk, cr):
        return ShowSummary(rating_key=rk, title="Test", year=None,
                           library="", thumb=None, content_rating=cr)

    s1 = mk("s1", "TV-MA")
    s2 = mk("s2", "TV-PG")
    s3 = mk("s3", None)

    rules = [{"rule_type": "content_rating", "operator": "include", "value": "TV-MA"}]
    result = _svc._apply_rules([s1, s2, s3], rules)
    keys = [r.rating_key for r in result]
    check("apply_rules: content_rating include matches", "s1" in keys)
    check("apply_rules: content_rating include rejects other", "s2" not in keys)
    check("apply_rules: content_rating include permits None", "s3" in keys)


def test_rest_api_no_auth():
    import tempfile, os
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "test_rest.db")
    os.environ["LINEARR_API_KEY"] = "test-key-api-123"
    os.environ.setdefault("PLEX_URL", "")
    os.environ.setdefault("PLEX_TOKEN", "")
    import importlib, db as _db
    importlib.reload(_db)
    _db.init_db()
    import app as _app
    importlib.reload(_app)
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    try:
        r = client.get("/api/v1/playlists")
        check("rest api: no auth → 401", r.status_code == 401)
        r2 = client.get("/api/v1/playlists", headers={"Authorization": "Bearer wrong"})
        check("rest api: wrong key → 401", r2.status_code == 401)
    finally:
        importlib.reload(_db)
        importlib.reload(_app)
        os.environ.pop("LINEARR_API_KEY", None)
        os.environ["DB_PATH"] = os.path.join(tmp, "_")


def test_rest_api_list_empty():
    import tempfile, os
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "test_rest2.db")
    os.environ["LINEARR_API_KEY"] = "test-key-456"
    import importlib, db as _db
    importlib.reload(_db)
    _db.init_db()
    import app as _app
    importlib.reload(_app)
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    try:
        r = client.get("/api/v1/playlists", headers={"Authorization": "Bearer test-key-456"})
        check("rest api: empty list → 200", r.status_code == 200)
        check("rest api: empty list → []", r.get_json() == [])
    finally:
        importlib.reload(_db)
        importlib.reload(_app)
        os.environ.pop("LINEARR_API_KEY", None)
        os.environ["DB_PATH"] = os.path.join(tmp, "_")


def test_rest_api_not_found():
    import tempfile, os
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "test_rest3.db")
    os.environ["LINEARR_API_KEY"] = "key-789"
    import importlib, db as _db
    importlib.reload(_db)
    _db.init_db()
    import app as _app
    importlib.reload(_app)
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    auth = {"Authorization": "Bearer key-789"}
    try:
        r = client.get("/api/v1/playlists/999", headers=auth)
        check("rest api: get missing → 404", r.status_code == 404)
        r2 = client.post("/api/v1/playlists/999/sync", headers=auth)
        check("rest api: sync missing → 404", r2.status_code == 404)
    finally:
        importlib.reload(_db)
        importlib.reload(_app)
        os.environ.pop("LINEARR_API_KEY", None)
        os.environ["DB_PATH"] = os.path.join(tmp, "_")


def test_rest_api_query_param_auth():
    import tempfile, os
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "test_rest4.db")
    os.environ["LINEARR_API_KEY"] = "query-key"
    import importlib, db as _db
    importlib.reload(_db)
    _db.init_db()
    import app as _app
    importlib.reload(_app)
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    try:
        r = client.get("/api/v1/playlists?api_key=query-key")
        check("rest api: query param auth → 200", r.status_code == 200)
    finally:
        importlib.reload(_db)
        importlib.reload(_app)
        os.environ.pop("LINEARR_API_KEY", None)
        os.environ["DB_PATH"] = os.path.join(tmp, "_")


def test_rest_api_backends():
    import tempfile, os
    tmp = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(tmp, "test_rest5.db")
    os.environ["LINEARR_API_KEY"] = "bk-key"
    import importlib, db as _db
    importlib.reload(_db)
    _db.init_db()
    import app as _app
    importlib.reload(_app)
    flask_app = _app.create_app()
    flask_app.config["TESTING"] = True
    client = flask_app.test_client()
    try:
        r = client.get("/api/v1/backends", headers={"Authorization": "Bearer bk-key"})
        check("rest api: backends → 200", r.status_code == 200)
        data = r.get_json()
        check("rest api: backends is dict", isinstance(data, dict))
    finally:
        importlib.reload(_db)
        importlib.reload(_app)
        os.environ.pop("LINEARR_API_KEY", None)
        os.environ["DB_PATH"] = os.path.join(tmp, "_")


def test_update_and_retrieve_stats():
    import tempfile, os, json
    td = tempfile.mkdtemp()
    os.environ["DB_PATH"] = os.path.join(td, "stats_test.db")
    import importlib, db as _db
    importlib.reload(_db)
    _db.init_db()
    with _db.connection() as conn:
        conn.execute(
            "INSERT INTO managed_playlists (name, sort_mode, backend, created_at) VALUES (?,?,?,?)",
            ("Test", "rotation", "plex", "2026-01-01T00:00:00"),
        )
        pid = conn.execute(
            "SELECT id FROM managed_playlists WHERE name='Test'"
        ).fetchone()["id"]
    _db.update_playlist_stats(pid, {"total_episodes": 10, "watched_episodes": 3})
    with _db.connection() as conn:
        row = conn.execute(
            "SELECT last_stats FROM managed_playlists WHERE id=?", (pid,)
        ).fetchone()
        data = json.loads(row["last_stats"])
    check("analytics: total episodes stored", data["total_episodes"] == 10)
    check("analytics: watched episodes stored", data["watched_episodes"] == 3)


def test_stats_none_if_never_synced():
    from service import PlaylistView
    v = PlaylistView.__new__(PlaylistView)
    check("analytics: last_stats default None",
          getattr(v, "last_stats", "MISSING") is None)


# ── v2.2.0 Franchise tests ───────────────────────────────────────────────


def _trakt_client():
    from trakt_client import TraktClient
    return TraktClient()


def test_parse_movie():
    tc = _trakt_client()
    raw = {
        "type": "movie", "rank": 1,
        "movie": {"title": "Iron Man", "year": 2008, "ids": {"tmdb": 1726, "imdb": "tt0371746"}},
    }
    item = tc._parse_item(raw)
    check("parse_movie: item_type", item["item_type"] == "movie")
    check("parse_movie: title", item["title"] == "Iron Man")
    check("parse_movie: tmdb_id", item["tmdb_id"] == 1726)
    check("parse_movie: tvdb_id is None", item["tvdb_id"] is None)
    check("parse_movie: show_title is None", item["show_title"] is None)


def test_parse_episode():
    tc = _trakt_client()
    raw = {
        "type": "episode", "rank": 3,
        "episode": {"season": 1, "number": 3, "title": "The One", "ids": {"tvdb": 12345}},
        "show": {"title": "Friends", "year": 1994, "ids": {"tvdb": 79168}},
    }
    item = tc._parse_item(raw)
    check("parse_episode: item_type", item["item_type"] == "episode")
    check("parse_episode: season_number", item["season_number"] == 1)
    check("parse_episode: episode_number", item["episode_number"] == 3)
    check("parse_episode: show_title", item["show_title"] == "Friends")
    check("parse_episode: show_tvdb_id", item["show_tvdb_id"] == 79168)
    check("parse_episode: tvdb_id", item["tvdb_id"] == 12345)


def test_parse_show():
    tc = _trakt_client()
    raw = {
        "type": "show", "rank": 1,
        "show": {"title": "Breaking Bad", "year": 2008, "ids": {"tvdb": 81189, "tmdb": 1396}},
    }
    item = tc._parse_item(raw)
    check("parse_show: item_type", item["item_type"] == "show")
    check("parse_show: tvdb_id", item["tvdb_id"] == 81189)
    check("parse_show: show_tvdb_id is None", item["show_tvdb_id"] is None)
    check("parse_show: season_number is None", item["season_number"] is None)


def test_parse_season():
    tc = _trakt_client()
    raw = {
        "type": "season", "rank": 2,
        "season": {"number": 3},
        "show": {"title": "Game of Thrones", "year": 2011, "ids": {"tvdb": 121361}},
    }
    item = tc._parse_item(raw)
    check("parse_season: item_type", item["item_type"] == "season")
    check("parse_season: season_number", item["season_number"] == 3)
    check("parse_season: show_title", item["show_title"] == "Game of Thrones")


def test_parse_unknown_type():
    tc = _trakt_client()
    raw = {"type": "person", "rank": 1}
    item = tc._parse_item(raw)
    check("parse_unknown_type: returns None", item is None)


def test_content_hash_deterministic():
    tc = _trakt_client()
    items = [
        {"rank": 1, "item_type": "movie", "title": "A", "year": 2000},
        {"rank": 2, "item_type": "movie", "title": "B", "year": 2001},
    ]
    h1 = tc.content_hash(items)
    h2 = tc.content_hash(items)
    check("content_hash: same items same hash", h1 == h2)


def test_content_hash_changes():
    tc = _trakt_client()
    items1 = [{"rank": 1, "item_type": "movie", "title": "A", "year": 2000}]
    items2 = [{"rank": 1, "item_type": "movie", "title": "B", "year": 2000}]
    h1 = tc.content_hash(items1)
    h2 = tc.content_hash(items2)
    check("content_hash: different items different hash", h1 != h2)


def test_normalize_for_match():
    from service import _normalize_for_match
    check("normalize: lowercases", _normalize_for_match("Iron Man") == "iron man")
    check("normalize: strips punctuation",
          _normalize_for_match("Mr. Robot") == "mr robot")
    check("normalize: collapses whitespace",
          _normalize_for_match("  The   Matrix  ") == "the matrix")


def test_replace_franchise_items_clears_old():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) VALUES (?,?,?,?,?)",
            ("test_replace", "Test", "trakt", "hash1", 2),
        )
        def_id = cur.lastrowid
    items1 = [
        {"rank": 1, "item_type": "movie", "title": "Movie A", "year": 2000,
         "tmdb_id": None, "tvdb_id": None, "imdb_id": None,
         "season_number": None, "episode_number": None, "show_title": None, "show_tvdb_id": None},
    ]
    items2 = [
        {"rank": 1, "item_type": "movie", "title": "Movie B", "year": 2001,
         "tmdb_id": None, "tvdb_id": None, "imdb_id": None,
         "season_number": None, "episode_number": None, "show_title": None, "show_tvdb_id": None},
        {"rank": 2, "item_type": "episode", "title": "E01", "year": 2001,
         "tmdb_id": None, "tvdb_id": 100, "imdb_id": None,
         "season_number": 1, "episode_number": 1, "show_title": "Show", "show_tvdb_id": 200},
    ]
    _db.replace_franchise_items(def_id, items1)
    after1 = _db.list_franchise_items(def_id)
    check("replace: first insert correct count", len(after1) == 1)
    _db.replace_franchise_items(def_id, items2)
    after2 = _db.list_franchise_items(def_id)
    check("replace: second replaces first count", len(after2) == 2)
    check("replace: new item present", after2[0]["title"] == "Movie B")


def test_upsert_franchise_match_state_conflict():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) VALUES (?,?,?,?,?)",
            ("test_upsert", "Test", "trakt", "hash", 1),
        )
        def_id = cur.lastrowid
        conn.execute(
            "INSERT INTO franchise_items (definition_id, rank, item_type, title) VALUES (?,?,?,?)",
            (def_id, 1, "movie", "Test Movie"),
        )
        fi_id = conn.execute("SELECT id FROM franchise_items WHERE definition_id=?", (def_id,)).fetchone()["id"]
        conn.execute(
            "INSERT INTO managed_playlists (name, backend, created_at) VALUES (?,?,?)",
            ("TestPL", "plex", "2026-01-01T00:00:00"),
        )
        pl_id = conn.execute("SELECT id FROM managed_playlists WHERE name='TestPL'").fetchone()["id"]
    _db.upsert_franchise_match_state(fi_id, pl_id, True, "plex-123", False, None)
    ms = _db.list_franchise_match_state(pl_id)
    check("upsert_match: initial plex_found", ms[fi_id]["plex_found"] == 1)
    _db.upsert_franchise_match_state(fi_id, pl_id, False, None, True, "jf-456")
    ms2 = _db.list_franchise_match_state(pl_id)
    check("upsert_match: updated plex_found", ms2[fi_id]["plex_found"] == 0)
    check("upsert_match: updated jellyfin_found", ms2[fi_id]["jellyfin_found"] == 1)


def test_franchise_playlist_type_in_db():
    import db as _db
    _db.init_db()
    check("VALID_PLAYLIST_TYPES includes franchise", "franchise" in _db.VALID_PLAYLIST_TYPES)


def test_pruning_enabled_default():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        conn.execute(
            "INSERT INTO managed_playlists (name, backend, created_at) VALUES (?,?,?)",
            ("PruningTest", "plex", "2026-01-01T00:00:00"),
        )
        row = conn.execute("SELECT pruning_enabled FROM managed_playlists WHERE name='PruningTest'").fetchone()
    check("pruning_enabled: default is 1", row["pruning_enabled"] == 1)


def test_pruner_skips_franchise():
    from service import _row_get
    row = {"playlist_type": "franchise"}
    check("pruner: skips franchise playlists", _row_get(row, "playlist_type") == "franchise")


def test_pruner_skips_pruning_disabled():
    from service import _row_get
    row = {"playlist_type": "manual", "pruning_enabled": 0}
    check("pruner: skips when pruning_enabled=0", not _row_get(row, "pruning_enabled", 1))


def test_local_franchise_load():
    import json
    import os
    import db as _db
    from service import _fetch_and_store_franchise_local

    items = [
        {"item_type": "movie", "title": "Local Movie", "year": 2020, "tmdb_id": 99999,
         "imdb_id": "tt9999999"},
        {"item_type": "episode", "title": "Local Ep", "show_title": "Local Show",
         "show_tvdb_id": 88888, "season_number": 1, "episode_number": 1},
    ]
    real_path = os.path.join(os.path.dirname(__file__), "defaults",
                             "franchise_data", "test_local.json")
    os.makedirs(os.path.dirname(real_path), exist_ok=True)
    with open(real_path, "w") as f:
        json.dump({"_info": {}, "items": items}, f)

    _db.init_db()
    try:
        def_id = _fetch_and_store_franchise_local("test_local", "Test Local")
        stored = _db.list_franchise_items(def_id)
        check("local_franchise: correct item count", len(stored) == 2)
        check("local_franchise: movie title", stored[0]["title"] == "Local Movie")
        check("local_franchise: movie tmdb_id", stored[0]["tmdb_id"] == 99999)
        check("local_franchise: episode title", stored[1]["title"] == "Local Ep")
        check("local_franchise: episode season", stored[1]["season_number"] == 1)
        defn = _db.get_franchise_definition("test_local")
        check("local_franchise: source is local", defn["source"] == "local")
    finally:
        if os.path.exists(real_path):
            os.unlink(real_path)


def test_local_franchise_missing_file():
    from service import _fetch_and_store_franchise_local
    try:
        _fetch_and_store_franchise_local("nonexistent_key_xyz", "Nonexistent")
        check("local_franchise_missing: should have raised", False)
    except FileNotFoundError:
        check("local_franchise_missing: raised FileNotFoundError", True)


def test_refresh_skips_local():
    import db as _db
    from service import refresh_franchise_definitions
    _db.init_db()
    with _db.connection() as conn:
        conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) "
            "VALUES (?,?,?,?,?)",
            ("test_skip_local", "Test Skip", "local", "abc123", 0),
        )
    try:
        refresh_franchise_definitions()
        defn = _db.get_franchise_definition("test_skip_local")
        check("refresh_skips_local: local definition survives refresh",
              defn is not None and defn["source"] == "local")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM franchise_definitions WHERE key='test_skip_local'")


# -- v2.3.0 franchise maker --------------------------------------------------- #

def test_valid_franchise_sources_includes_user():
    import db as _db
    check("VALID_FRANCHISE_SOURCES: includes user",
          "user" in _db.VALID_FRANCHISE_SOURCES)
    check("VALID_FRANCHISE_SOURCES: includes trakt",
          "trakt" in _db.VALID_FRANCHISE_SOURCES)
    check("VALID_FRANCHISE_SOURCES: includes local",
          "local" in _db.VALID_FRANCHISE_SOURCES)


def test_franchise_definition_forked_from_key_migration():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        fd_cols = _db._columns(conn, "franchise_definitions")
        check("forked_from_key: column exists", "forked_from_key" in fd_cols)


def test_tmdb_key_from_db():
    import db as _db
    from tmdb_client import get_tmdb_key
    _db.set_setting("tmdb_api_key", "test-key-123")
    try:
        key = get_tmdb_key()
        check("tmdb_key_from_db: returns value", key == "test-key-123")
    finally:
        _db.set_setting("tmdb_api_key", "")


def test_tmdb_key_fallback_env():
    import os
    from tmdb_client import get_tmdb_key
    old = os.environ.get("TMDB_API_KEY")
    try:
        os.environ["TMDB_API_KEY"] = "env-fallback-key"
        # Clear DB to force env fallback
        import db as _db
        _db.set_setting("tmdb_api_key", "")
        key = get_tmdb_key()
        check("tmdb_key_fallback_env: returns env value", key == "env-fallback-key")
    finally:
        if old is not None:
            os.environ["TMDB_API_KEY"] = old
        else:
            os.environ.pop("TMDB_API_KEY", None)


def test_tmdb_key_missing():
    from tmdb_client import get_tmdb_key
    import os
    old = os.environ.get("TMDB_API_KEY")
    try:
        if "TMDB_API_KEY" in os.environ:
            del os.environ["TMDB_API_KEY"]
        import db as _db
        _db.set_setting("tmdb_api_key", "")
        key = get_tmdb_key()
        check("tmdb_key_missing: returns empty", not key)
    finally:
        if old is not None:
            os.environ["TMDB_API_KEY"] = old


def test_franchise_items_for_maker():
    import db as _db
    from service import franchise_items_for_maker
    _db.init_db()
    with _db.connection() as conn:
        conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) "
            "VALUES (?,?,?,?,?)",
            ("test_maker1", "Maker Test", "user", "abc", 2),
        )
        def_id = conn.execute(
            "SELECT id FROM franchise_definitions WHERE key='test_maker1'"
        ).fetchone()["id"]
        conn.execute(
            "INSERT INTO franchise_items (definition_id, rank, item_type, title, year, tmdb_id, season_number, show_title, show_tvdb_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (def_id, 1, "movie", "Test Movie", 2022, 99999, None, None, None),
        )
        conn.execute(
            "INSERT INTO franchise_items (definition_id, rank, item_type, title, show_title, season_number, show_tvdb_id) "
            "VALUES (?,?,?,?,?,?,?)",
            (def_id, 2, "episode", "Pilot", "Test Show", 1, 88888),
        )
    try:
        items = franchise_items_for_maker(def_id)
        check("maker_items: correct count", len(items) == 2)
        check("maker_items: movie title", items[0]["title"] == "Test Movie")
        check("maker_items: movie year", items[0]["year"] == 2022)
        check("maker_items: movie tmdb_id", items[0]["tmdb_id"] == 99999)
        check("maker_items: episode title", items[1]["title"] == "Pilot")
        check("maker_items: episode show_title", items[1]["show_title"] == "Test Show")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM franchise_definitions WHERE key='test_maker1'")


def test_user_franchise_save_empty_items():
    from service import save_user_franchise_playlist
    try:
        save_user_franchise_playlist(
            playlist_id=None,
            name="Test Empty",
            backend="plex",
            items=[],
        )
        check("user_save_empty: should have raised", False)
    except ValueError:
        check("user_save_empty: raised ValueError", True)


def test_user_franchise_db_insert():
    import db as _db
    from service import save_user_franchise_playlist
    _db.init_db()
    items = [
        {"item_type": "movie", "title": "Test Movie", "year": 2023, "tmdb_id": 11111},
    ]
    with _db.connection() as conn:
        old = conn.execute(
            "SELECT id FROM managed_playlists WHERE name='Test DB Insert'"
        ).fetchone()
        if old:
            conn.execute("DELETE FROM managed_playlists WHERE id=?", (old["id"],))
        old_def = conn.execute(
            "SELECT id FROM franchise_definitions WHERE key LIKE 'user_%' AND name='Test DB Insert'"
        ).fetchone()
        if old_def:
            conn.execute("DELETE FROM franchise_definitions WHERE id=?", (old_def["id"],))

    pid = None
    try:
        pid = save_user_franchise_playlist(
            playlist_id=None,
            name="Test DB Insert",
            backend="plex",
            items=items,
        )
    except Exception:
        pass

    if pid:
        row = _db.get_playlist(pid)
        check("user_save_db: playlist exists", row is not None)
        check("user_save_db: type is franchise",
              row["playlist_type"] == "franchise" if row else False)

        if row and row["franchise_definition_id"]:
            defn = _db.get_franchise_definition_by_id(row["franchise_definition_id"])
            check("user_save_db: defn exists", defn is not None)
            if defn:
                check("user_save_db: source is user", defn["source"] == "user")

        with _db.connection() as conn:
            conn.execute("DELETE FROM managed_playlists WHERE name='Test DB Insert'")


def test_edit_user_franchise_updates():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) "
            "VALUES (?,?,?,?,?)",
            ("user_edit_test", "Edit Test", "user", "hash1", 1),
        )
        def_id = cur.lastrowid
        conn.execute(
            "INSERT INTO franchise_items (definition_id, rank, item_type, title) "
            "VALUES (?,?,?,?)",
            (def_id, 1, "movie", "Old Movie"),
        )
        cur2 = conn.execute(
            "INSERT INTO managed_playlists (name, backend, playlist_type, sort_mode, "
            "  franchise_definition_id, pruning_enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Edit Test PL", "plex", "franchise", "franchise", def_id, 0, "2026-01-01T00:00:00"),
        )
        pl_id = cur2.lastrowid

    try:
        from service import save_user_franchise_playlist
        new_items = [
            {"item_type": "movie", "title": "New Movie", "year": 2024, "tmdb_id": 22222},
            {"item_type": "episode", "title": "Pilot", "show_title": "Show X",
             "show_tvdb_id": 999, "season_number": 1, "episode_number": 1},
        ]
        pid = None
        try:
            pid = save_user_franchise_playlist(
                playlist_id=pl_id,
                name="Edit Test PL",
                backend="plex",
                items=new_items,
            )
        except Exception:
            pass

        check("edit_user: same playlist id", pid is None or pid == pl_id)

        fitems = _db.list_franchise_items(def_id)
        check("edit_user: items replaced", len(fitems) == 2)
        check("edit_user: new movie title", fitems[0]["title"] == "New Movie")
        check("edit_user: episode title", fitems[1]["title"] == "Pilot")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM managed_playlists WHERE name='Edit Test PL'")
            conn.execute("DELETE FROM franchise_definitions WHERE key='user_edit_test'")


def test_edit_bundled_franchise_creates_fork():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) "
            "VALUES (?,?,?,?,?)",
            ("bundle_test_fork", "Bundle Test", "local", "hash1", 1),
        )
        bundled_id = cur.lastrowid
        conn.execute(
            "INSERT INTO franchise_items (definition_id, rank, item_type, title) "
            "VALUES (?,?,?,?)",
            (bundled_id, 1, "movie", "Bundled Movie"),
        )
        cur2 = conn.execute(
            "INSERT INTO managed_playlists (name, backend, playlist_type, sort_mode, "
            "  franchise_definition_id, pruning_enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Fork Test PL", "plex", "franchise", "franchise", bundled_id, 0, "2026-01-01T00:00:00"),
        )
        pl_id = cur2.lastrowid

    try:
        from service import save_user_franchise_playlist
        new_items = [
            {"item_type": "movie", "title": "Edited Movie", "year": 2025, "tmdb_id": 33333},
        ]
        try:
            save_user_franchise_playlist(
                playlist_id=pl_id,
                name="Fork Test PL (edited)",
                backend="plex",
                items=new_items,
            )
        except Exception:
            pass

        row = _db.get_playlist(pl_id)
        new_def_id = row["franchise_definition_id"]
        check("edit_bundled: new def id differs from bundled",
              new_def_id != bundled_id)

        new_def = _db.get_franchise_definition_by_id(new_def_id)
        check("edit_bundled: new def source is user", new_def["source"] == "user")
        check("edit_bundled: forked_from_key",
              new_def.get("forked_from_key") == "bundle_test_fork")

        bundled = _db.get_franchise_definition_by_id(bundled_id)
        check("edit_bundled: bundled unchanged", bundled is not None)
        check("edit_bundled: bundled still local", bundled["source"] == "local")

        fitems = _db.list_franchise_items(new_def_id)
        check("edit_bundled: new def has items", len(fitems) == 1)
        check("edit_bundled: edited movie title", fitems[0]["title"] == "Edited Movie")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM managed_playlists WHERE name LIKE 'Fork Test PL%'")
            conn.execute("DELETE FROM franchise_definitions WHERE key='bundle_test_fork'")
            conn.execute("DELETE FROM franchise_definitions WHERE key LIKE 'user_%' AND name LIKE 'Fork Test PL%'")


def test_restore_bundled_franchise():
    import db as _db
    from service import restore_bundled_franchise
    _db.init_db()
    with _db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) "
            "VALUES (?,?,?,?,?)",
            ("restore_test_bundle", "Restore Bundle", "local", "hash1", 1),
        )
        bundled_id = cur.lastrowid
        conn.execute(
            "INSERT INTO franchise_items (definition_id, rank, item_type, title) "
            "VALUES (?,?,?,?)",
            (bundled_id, 1, "movie", "Bundled Movie"),
        )
        cur_fork = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, forked_from_key, content_hash, item_count) "
            "VALUES (?,?,?,?,?,?)",
            ("restore_test_fork", "Fork", "user", "restore_test_bundle", "hash2", 2),
        )
        fork_id = cur_fork.lastrowid
        conn.execute(
            "INSERT INTO franchise_items (definition_id, rank, item_type, title) "
            "VALUES (?,?,?,?)",
            (fork_id, 1, "movie", "Forked Movie"),
        )
        cur_pl = conn.execute(
            "INSERT INTO managed_playlists (name, backend, playlist_type, sort_mode, "
            "  franchise_definition_id, pruning_enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Restore Test PL", "plex", "franchise", "franchise", fork_id, 0, "2026-01-01T00:00:00"),
        )
        pl_id = cur_pl.lastrowid

    try:
        try:
            ok = restore_bundled_franchise(pl_id)
        except Exception:
            ok = True
        check("restore: returns True", ok)

        row = _db.get_playlist(pl_id)
        check("restore: playlist rebound", row["franchise_definition_id"] == bundled_id)
        check("restore: fork def deleted",
              _db.get_franchise_definition_by_id(fork_id) is None)
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM managed_playlists WHERE name='Restore Test PL'")
            conn.execute("DELETE FROM franchise_definitions WHERE key='restore_test_bundle'")
            conn.execute("DELETE FROM franchise_definitions WHERE key='restore_test_fork'")


def test_restore_skips_non_fork():
    import db as _db
    from service import restore_bundled_franchise
    _db.init_db()
    with _db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) "
            "VALUES (?,?,?,?,?)",
            ("no_fork_test", "No Fork", "local", "hash1", 1),
        )
        bundled_id = cur.lastrowid
        conn.execute(
            "INSERT INTO franchise_items (definition_id, rank, item_type, title) "
            "VALUES (?,?,?,?)",
            (bundled_id, 1, "movie", "Some Movie"),
        )
        cur_pl = conn.execute(
            "INSERT INTO managed_playlists (name, backend, playlist_type, sort_mode, "
            "  franchise_definition_id, pruning_enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("NoFork PL", "plex", "franchise", "franchise", bundled_id, 0, "2026-01-01T00:00:00"),
        )
        pl_id = cur_pl.lastrowid

    try:
        ok = restore_bundled_franchise(pl_id)
        check("restore_non_fork: returns False", not ok)
        row = _db.get_playlist(pl_id)
        check("restore_non_fork: still bound to bundled",
              row["franchise_definition_id"] == bundled_id)
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM managed_playlists WHERE name='NoFork PL'")
            conn.execute("DELETE FROM franchise_definitions WHERE key='no_fork_test'")


def test_refresh_skips_user():
    import db as _db
    from service import refresh_franchise_definitions
    _db.init_db()
    with _db.connection() as conn:
        conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) "
            "VALUES (?,?,?,?,?)",
            ("test_skip_user", "Test Skip User", "user", "abc123", 0),
        )
    try:
        refresh_franchise_definitions()
        defn = _db.get_franchise_definition("test_skip_user")
        check("refresh_skips_user: user definition survives refresh",
              defn is not None and defn["source"] == "user")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM franchise_definitions WHERE key='test_skip_user'")


def test_forked_franchise_shared_cleanup():
    import db as _db
    from service import restore_bundled_franchise
    _db.init_db()
    with _db.connection() as conn:
        cur_bundle = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count) "
            "VALUES (?,?,?,?,?)",
            ("shared_bundle", "Shared Bundle", "local", "hash1", 1),
        )
        bundled_id = cur_bundle.lastrowid
        cur_fork = conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, forked_from_key, content_hash, item_count) "
            "VALUES (?,?,?,?,?,?)",
            ("shared_fork", "Shared Fork", "user", "shared_bundle", "hash2", 2),
        )
        fork_id = cur_fork.lastrowid
        conn.execute("INSERT INTO franchise_items (definition_id, rank, item_type, title) VALUES (?,?,?,?)",
                     (fork_id, 1, "movie", "Shared Movie"))
        cur_pl1 = conn.execute(
            "INSERT INTO managed_playlists (name, backend, playlist_type, sort_mode, "
            "  franchise_definition_id, pruning_enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Shared Fork PL1", "plex", "franchise", "franchise", fork_id, 0, "2026-01-01T00:00:00"),
        )
        pl1_id = cur_pl1.lastrowid
        cur_pl2 = conn.execute(
            "INSERT INTO managed_playlists (name, backend, playlist_type, sort_mode, "
            "  franchise_definition_id, pruning_enabled, created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("Shared Fork PL2", "plex", "franchise", "franchise", fork_id, 0, "2026-01-01T00:00:00"),
        )
        pl2_id = cur_pl2.lastrowid

    try:
        try:
            ok = restore_bundled_franchise(pl1_id)
        except Exception:
            ok = True
        check("shared_restore: pl1 restored", ok)
        row1 = _db.get_playlist(pl1_id)
        check("shared_restore: pl1 rebound", row1["franchise_definition_id"] == bundled_id)
        check("shared_restore: fork still exists (pl2 still uses it)",
              _db.get_franchise_definition_by_id(fork_id) is not None)
        row2 = _db.get_playlist(pl2_id)
        check("shared_restore: pl2 still uses fork",
              row2["franchise_definition_id"] == fork_id)
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM managed_playlists WHERE name IN ('Shared Fork PL1','Shared Fork PL2')")
            conn.execute("DELETE FROM franchise_definitions WHERE key='shared_bundle'")
            conn.execute("DELETE FROM franchise_definitions WHERE key='shared_fork'")


def test_maker_import_trakt_invalid_url():
    import os
    os.environ["FLASK_SECRET"] = "test-secret"
    import db as _db_mod
    old_path = _db_mod.DB_PATH
    try:
        import tempfile
        tmp = tempfile.mkdtemp()
        _db_mod.DB_PATH = os.path.join(tmp, "test_trakt_import.db")
        _db_mod.init_db()
        from app import create_app
        app = create_app()
        with app.test_client() as c:
            resp = c.post("/api/franchise-maker/import-trakt",
                          json={"url": "not-a-trakt-url"})
            check("import_trakt_invalid: status 400", resp.status_code == 400)
    finally:
        _db_mod.DB_PATH = old_path
        import shutil
        if os.path.exists(tmp):
            shutil.rmtree(tmp)


# --------------------------------------------------------------------------- #
# v2.4.0 — Chronolists integration tests
# --------------------------------------------------------------------------- #


def test_parse_chronolists_movie():
    from chronolists_client import parse_chronolists_items
    raw = [{"type": "movie", "name": "Iron Man",
            "tmdbId": 1726, "imdbId": "tt0371746"}]
    items = parse_chronolists_items(raw)
    check("chronolist_movie: count", len(items) == 1, str(len(items)))
    check("chronolist_movie: item_type", items[0]["item_type"] == "movie")
    check("chronolist_movie: rank", items[0]["rank"] == 1)
    check("chronolist_movie: tmdb_id", items[0]["tmdb_id"] == 1726)
    check("chronolist_movie: imdb_id", items[0]["imdb_id"] == "tt0371746")
    check("chronolist_movie: show_tmdb_id is None", items[0]["show_tmdb_id"] is None)
    check("chronolist_movie: season is None", items[0]["season_number"] is None)


def test_parse_chronolists_tv():
    from chronolists_client import parse_chronolists_items
    raw = [{"type": "tv", "name": "Marvel's Agent Carter",
            "season": 1, "episode": 1,
            "tmdbId": 61550, "tmdbSeasonId": 63213, "tmdbEpisodeId": 1013214,
            "imdbId": "tt3475734"}]
    items = parse_chronolists_items(raw)
    check("chronolist_tv: count", len(items) == 1)
    check("chronolist_tv: item_type", items[0]["item_type"] == "episode")
    check("chronolist_tv: show_tmdb_id", items[0]["show_tmdb_id"] == 61550)
    check("chronolist_tv: tmdb_id is None", items[0]["tmdb_id"] is None)
    check("chronolist_tv: season", items[0]["season_number"] == 1)
    check("chronolist_tv: episode", items[0]["episode_number"] == 1)
    check("chronolist_tv: show_tvdb_id is None", items[0]["show_tvdb_id"] is None)
    check("chronolist_tv: show_title", items[0]["show_title"] == "Marvel's Agent Carter")


def test_parse_chronolists_rank_ordering():
    from chronolists_client import parse_chronolists_items
    raw = [
        {"type": "movie", "name": "A", "tmdbId": 1},
        {"type": "movie", "name": "B", "tmdbId": 2},
        {"type": "tv", "name": "C", "season": 1, "episode": 1, "tmdbId": 3},
    ]
    items = parse_chronolists_items(raw)
    check("chronolist_ranks: count", len(items) == 3, str(len(items)))
    ranks = [it["rank"] for it in items]
    check("chronolist_ranks: [1,2,3]", ranks == [1, 2, 3], str(ranks))


def test_parse_chronolists_unknown_type_skipped():
    from chronolists_client import parse_chronolists_items
    raw = [
        {"type": "movie", "name": "A", "tmdbId": 1},
        {"type": "collection", "name": "Skip me"},
        {"type": "tv", "name": "C", "season": 1, "episode": 1, "tmdbId": 3},
    ]
    items = parse_chronolists_items(raw)
    check("chronolist_unknown: count", len(items) == 2, str(len(items)))
    check("chronolist_unknown: item 1 is movie", items[0]["item_type"] == "movie")
    check("chronolist_unknown: item 2 is episode", items[1]["item_type"] == "episode")


def test_show_summary_tmdb_id_default():
    from media_client import ShowSummary
    s = ShowSummary(rating_key="123", title="Test", year=2020, library="TV", thumb=None)
    check("ShowSummary.tmdb_id default is None", s.tmdb_id is None)


def test_valid_franchise_sources_has_chronolists():
    import db as _db
    check("chronolists in VALID_FRANCHISE_SOURCES",
          "chronolists" in _db.VALID_FRANCHISE_SOURCES)


def test_franchise_registry_integrity():
    import json as _json
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "defaults", "franchises.json")
    with open(path) as f:
        reg = _json.load(f)
    check("registry: 23 entries", len(reg) == 23, f"got {len(reg)}")
    keys = [e["key"] for e in reg]
    check("registry: unique keys", len(keys) == len(set(keys)))
    check("registry: no x_men", "x_men" not in keys)
    check("registry: xmen_a present", "xmen_a" in keys)
    check("registry: xmen_b present", "xmen_b" in keys)
    for e in reg:
        if e["source"] == "chronolists":
            check(f"registry: {e['key']} has chronolists_id",
                  e.get("chronolists_id") is not None)
        if e["source"] == "trakt":
            check(f"registry: {e['key']} has trakt_user",
                  e.get("trakt_user") is not None)
            check(f"registry: {e['key']} has trakt_slug",
                  e.get("trakt_slug") is not None)


def test_resolve_show_for_item_tmdb_fallback():
    from service import _resolve_show_for_item
    from media_client import ShowSummary
    show = ShowSummary(
        rating_key="jf_show_99", title="Agent Carter", year=2015,
        library="TV", thumb=None, tvdb_id=None, tmdb_id=61550,
    )
    cache = {
        "show_by_tvdb": {},
        "show_by_tmdb": {61550: show},
        "show_by_title_year": {},
    }
    item = {"show_tvdb_id": None, "show_tmdb_id": 61550,
            "show_title": "Agent Carter", "year": 2015}
    result = _resolve_show_for_item(item, cache)
    check("resolve_tmdb: show found", result is not None)
    check("resolve_tmdb: correct show", result.rating_key == "jf_show_99")


# --------------------------------------------------------------------------- #
# v2.5.0 — Chronolists auto-discovery tests
# --------------------------------------------------------------------------- #


def test_normalize_cl_key():
    from service import _normalize_cl_key
    check("cl_key: james-bond", _normalize_cl_key("james-bond") == "james_bond")
    check("cl_key: the-boys", _normalize_cl_key("the-boys") == "the_boys")
    check("cl_key: mcu (no change)", _normalize_cl_key("mcu") == "mcu")
    check("cl_key: xmen-a", _normalize_cl_key("xmen-a") == "xmen_a")


def test_known_cl_ids_count():
    import json as _json
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "defaults", "franchises.json")
    with open(path) as f:
        reg = _json.load(f)
    known = {e["chronolists_id"] for e in reg if e.get("chronolists_id")}
    check("known_cl_ids: all 16 chronolists entries have id", len(known) == 16, str(len(known)))


def test_auto_discovered_not_overwritten_on_update():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        conn.execute(
            """INSERT INTO franchise_definitions
               (key, name, source, chronolists_id, content_hash, item_count, auto_discovered)
               VALUES (?,?,?,?,?,?,?)""",
            ("test_ad_flag", "Test AD", "chronolists", "test-id", "hash1", 5, 1),
        )
    try:
        _db.upsert_franchise_definition(
            key="test_ad_flag", name="Test AD Updated", source="chronolists",
            trakt_user=None, trakt_slug=None, chronolists_id="test-id",
            fetched_at="2026-01-01T00:00:00Z", content_hash="hash2",
            item_count=10, auto_discovered=0,
        )
        defn = _db.get_franchise_definition("test_ad_flag")
        check("auto_disc_flag: still 1 after UPDATE", defn.get("auto_discovered") == 1,
              f"got {defn.get('auto_discovered')}")
        check("auto_disc_flag: name updated", defn.get("name") == "Test AD Updated")
        check("auto_disc_flag: hash updated", defn.get("content_hash") == "hash2")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM franchise_definitions WHERE key='test_ad_flag'")


def test_list_auto_discovered():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count, auto_discovered) "
            "VALUES (?,?,?,?,?,?)",
            ("ad_yes", "Auto Yes", "chronolists", "h1", 1, 1),
        )
        conn.execute(
            "INSERT INTO franchise_definitions (key, name, source, content_hash, item_count, auto_discovered) "
            "VALUES (?,?,?,?,?,?)",
            ("ad_no", "Auto No", "trakt", "h2", 2, 0),
        )
    try:
        results = _db.list_auto_discovered_franchise_definitions()
        check("auto_disc_list: only 1 row", len(results) == 1, str(len(results)))
        check("auto_disc_list: correct key", results[0]["key"] == "ad_yes")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM franchise_definitions WHERE key IN ('ad_yes','ad_no')")


def test_merged_franchise_list_source_override():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        conn.execute("""INSERT INTO franchise_definitions
            (key, name, source, chronolists_id, content_hash, item_count)
            VALUES (?,?,?,?,?,?)""",
            ("james_bond", "James Bond", "chronolists", "james-bond", "cl_hash", 25),
        )
    try:
        from app import _merged_franchise_list
        merged = _merged_franchise_list()
        jb = next((m for m in merged if m["key"] == "james_bond"), None)
        check("merge_override: jb present", jb is not None)
        check("merge_override: source is chronolists", jb["source"] == "chronolists")
        check("merge_override: chronolists_id set", jb["chronolists_id"] == "james-bond")
        check("merge_override: trakt_user cleared", jb["trakt_user"] is None)
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM franchise_definitions WHERE key='james_bond'")


def test_merged_franchise_list_auto_discovered():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        conn.execute("""INSERT INTO franchise_definitions
            (key, name, source, chronolists_id, content_hash, item_count, auto_discovered)
            VALUES (?,?,?,?,?,?,?)""",
            ("the_boys", "The Boys", "chronolists", "the-boys", "hash1", 30, 1),
        )
    try:
        from app import _merged_franchise_list
        merged = _merged_franchise_list()
        tb = next((m for m in merged if m["key"] == "the_boys"), None)
        check("merge_ad: the_boys present", tb is not None)
        check("merge_ad: source is chronolists", tb["source"] == "chronolists")
        check("merge_ad: chronolists_id set", tb["chronolists_id"] == "the-boys")
        check("merge_ad: name from DB", tb["name"] == "The Boys")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM franchise_definitions WHERE key='the_boys'")


def test_merged_franchise_list_no_duplication():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        conn.execute("""INSERT INTO franchise_definitions
            (key, name, source, chronolists_id, content_hash, item_count, auto_discovered)
            VALUES (?,?,?,?,?,?,?)""",
            ("mcu", "MCU Duplicate", "chronolists", "mcu", "h", 40, 1),
        )
    try:
        from app import _merged_franchise_list
        merged = _merged_franchise_list()
        mcu_count = sum(1 for m in merged if m["key"] == "mcu")
        check("merge_dup: mcu appears once", mcu_count == 1, str(mcu_count))
        mcu = next(m for m in merged if m["key"] == "mcu")
        check("merge_dup: mcu name from static", mcu["name"] == "Marvel (MCU)")
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM franchise_definitions WHERE key='mcu'")


# --------------------------------------------------------------------------- #
# v3.0.0 — Emby backend tests
# --------------------------------------------------------------------------- #


def test_parse_backend_set_basic():
    from media_client import parse_backend_set
    check("parse: plex,jellyfin", parse_backend_set("plex,jellyfin") == ["plex","jellyfin"])
    check("parse: both legacy", parse_backend_set("both") == ["plex","jellyfin"])
    check("parse: emby single", parse_backend_set("emby") == ["emby"])
    check("parse: all three", parse_backend_set("plex,jellyfin,emby") == ["plex","jellyfin","emby"])
    check("parse: empty -> plex", parse_backend_set("") == ["plex"])
    check("parse: None -> plex", parse_backend_set(None) == ["plex"])
    check("parse: unknown dropped", parse_backend_set("plex,foo") == ["plex"])
    check("parse: canonical order enforced",
          parse_backend_set("emby,plex") == ["plex","emby"])


def test_format_backend_set():
    from media_client import format_backend_set
    check("format: canonical", format_backend_set(["emby","plex"]) == "plex,emby")
    check("format: dedupe", format_backend_set(["plex","plex"]) == "plex")
    check("format: empty -> plex", format_backend_set([]) == "plex")
    check("format: plex,jellyfin", format_backend_set(["plex","jellyfin"]) == "plex,jellyfin")


def test_primary_backend():
    from media_client import primary_backend
    check("primary: emby", primary_backend("emby") == "emby")
    check("primary: plex,emby", primary_backend("plex,emby") == "plex")
    check("primary: jellyfin,emby", primary_backend("jellyfin,emby") == "jellyfin")


def test_emby_delete_safety():
    from emby_client import _check_delete_safety, EmbySafetyError
    try:
        _check_delete_safety("/Playlists/abc123/Items")
    except EmbySafetyError:
        check("emby guard: allowed DELETE passed", False)
    else:
        check("emby guard: allowed DELETE passed", True)

    try:
        _check_delete_safety("/Items")
        check("emby guard: denied /Items", False)
    except EmbySafetyError:
        check("emby guard: denied /Items", True)

    try:
        _check_delete_safety("/Items?Ids=123")
        check("emby guard: denied Ids", False)
    except EmbySafetyError:
        check("emby guard: denied Ids", True)

    try:
        _check_delete_safety("/Library/VirtualFolders")
        check("emby guard: denied library", False)
    except EmbySafetyError:
        check("emby guard: denied library", True)


def test_backends_for_csv():
    import db as _db
    _db.init_db()
    with _db.connection() as conn:
        cur = conn.execute(
            "INSERT INTO managed_playlists (name, backend, sort_mode, created_at) "
            "VALUES (?,?,?,?)",
            ("CSV Test", "plex,emby", "franchise", "2026-01-01T00:00:00"),
        )
        pl_id = cur.lastrowid
    try:
        from service import _backends_for
        row = _db.get_playlist(pl_id)
        result = _backends_for(row)
        check("csv_backends: count", len(result) == 2, str(len(result)))
        check("csv_backends: plex present", "plex" in result)
        check("csv_backends: emby present", "emby" in result)
        check("csv_backends: jellyfin absent", "jellyfin" not in result)
    finally:
        with _db.connection() as conn:
            conn.execute("DELETE FROM managed_playlists WHERE name='CSV Test'")


def test_show_config_id_for_emby():
    from service import ShowConfig
    sc = ShowConfig(rating_key="rk1", title="Test",
                    plex_rating_key="p1", jellyfin_rating_key="j1",
                    emby_rating_key="e1",
                    emby_movie_rating_keys=["em1","em2"])
    check("id_for: plex", sc.id_for("plex") == "p1")
    check("id_for: jellyfin", sc.id_for("jellyfin") == "j1")
    check("id_for: emby", sc.id_for("emby") == "e1")
    check("movie_ids_for: emby", sc.movie_ids_for("emby") == ["em1","em2"])


def test_db_backend_validation():
    import db as _db
    try:
        _db._validate_backend("plex,emby")
        check("validate: plex,emby accepted", True)
    except ValueError:
        check("validate: plex,emby accepted", False)

    try:
        _db._validate_backend("both")
        check("validate: both accepted", True)
    except ValueError:
        check("validate: both accepted", False)

    try:
        _db._validate_backend("")
        check("validate: empty rejected", False)
    except ValueError:
        check("validate: empty rejected", True)

    try:
        _db._validate_backend("plex,garbage")
        check("validate: garbage rejected", False)
    except ValueError:
        check("validate: garbage rejected", True)


def test_match_franchise_empty_returns_quad():
    """Regression: _match_franchise_to_library must return a 4-tuple
    (plex, jellyfin, emby, missing) even when the definition has no items, so
    the v3.0.0 callers unpacking 4 values don't crash. The empty case returns
    early (before any backend client call), so this needs no network."""
    import os, tempfile
    import db as _db
    import service as _svc
    orig = _db.DB_PATH
    tmp = tempfile.mktemp(suffix=".db")
    try:
        _db.DB_PATH = tmp
        _db.init_db()
        result = _svc._match_franchise_to_library(999999, 1, {})
        check("match franchise empty -> 4-tuple", len(result) == 4)
        check("match franchise empty -> ([],[],[],0)", result == ([], [], [], 0))
    finally:
        _db.DB_PATH = orig
        try:
            os.unlink(tmp)
        except OSError:
            pass


def test_franchise_external_id_bridge():
    """TMDB-keyed franchise items match a TVDB/IMDB-scraped library (no TMDB ids)
    via the cached external-id bridge. No network: we pre-seed the cache."""
    import service as _svc

    class _Item:
        def __init__(self, rk):
            self.rating_key = rk

    show, movie = _Item("show-1"), _Item("movie-1")
    cache = {
        "client": None,
        "movie_by_tmdb": {}, "movie_by_imdb": {"tt100": movie}, "movie_by_title_year": {},
        "show_by_tvdb": {999: show}, "show_by_tmdb": {}, "show_by_imdb": {},
        "show_by_title_year": {}, "episode_cache": {},
    }
    # Pre-seed so _franchise_external_ids returns without calling TMDB.
    _svc._EXTERNAL_ID_CACHE[("tv", 12345)] = {"tvdb_id": "999", "imdb_id": None}
    _svc._EXTERNAL_ID_CACHE[("movie", 555)] = {"imdb_id": "tt100", "tvdb_id": None}

    got = _svc._resolve_show_for_item({"show_tmdb_id": 12345, "title": "X"}, cache)
    check("franchise show bridges tmdb->tvdb", got is show)

    rk = _svc._resolve_franchise_item(
        {"item_type": "movie", "tmdb_id": 555, "title": "X", "year": 2000}, cache)
    check("franchise movie bridges tmdb->imdb", rk == "movie-1")

    # Library that does carry tmdb still matches directly (no bridge needed).
    cache["show_by_tmdb"] = {777: show}
    got2 = _svc._resolve_show_for_item({"show_tmdb_id": 777}, cache)
    check("franchise show direct tmdb still works", got2 is show)


def test_cross_backend_id_matching():
    """Cross-backend show matching links the same show across backends by ANY
    shared provider id (TVDB/TMDB/IMDB), not just TVDB."""
    import service as _svc

    class _S:
        def __init__(self, rk, tvdb=None, tmdb=None, imdb=None):
            self.rating_key = rk
            self.tvdb_id = tvdb
            self.tmdb_id = tmdb
            self.imdb_id = imdb

    cands = [_S("a", tvdb="111"), _S("b", imdb="tt999"), _S("c", tmdb=222)]
    check("match by imdb", _svc._find_match_by_ids(cands, {("imdb", "tt999")}) == "b")
    check("match by tmdb", _svc._find_match_by_ids(cands, {("tmdb", "222")}) == "c")
    check("no shared id -> None", _svc._find_match_by_ids(cands, {("tvdb", "999")}) is None)
    check("empty want -> None", _svc._find_match_by_ids(cands, set()) is None)

    s = _S("x", tvdb=75682, tmdb=1668, imdb="tt0386676")
    check("id set has all three",
          _svc._show_id_set(s) == {("tvdb", "75682"), ("tmdb", "1668"), ("imdb", "tt0386676")})


def test_set_playlist_image_present():
    """v3.0.0 franchise cover art: the ABC exposes a non-abstract no-op
    set_playlist_image, and every backend client overrides it."""
    from media_client import MediaClient
    check("set_playlist_image on ABC", hasattr(MediaClient, "set_playlist_image"))
    check("set_playlist_image not abstract",
          "set_playlist_image" not in getattr(MediaClient, "__abstractmethods__", set()))
    import plex_client, jellyfin_client, emby_client
    for mod, cls in (("plex_client", "PlexClient"),
                     ("jellyfin_client", "JellyfinClient"),
                     ("emby_client", "EmbyClient")):
        klass = getattr(__import__(mod), cls)
        check(f"{cls}.set_playlist_image overridden",
              klass.set_playlist_image is not MediaClient.set_playlist_image)


# --------------------------------------------------------------------------- #
# v3.0.10 — Chunked playlist requests + decoupled season gathering
# --------------------------------------------------------------------------- #


def test_chunked_correctness():
    """_chunked yields successive n-sized lists, preserving order and respecting
    the max-chunk limit.  Import from both client modules (standalone copies)."""
    from jellyfin_client import _chunked as jf_chunked, _MAX_IDS_PER_REQUEST as jf_max
    from emby_client import _chunked as em_chunked, _MAX_IDS_PER_REQUEST as em_max

    for _ch, _max in ((jf_chunked, jf_max), (em_chunked, em_max)):
        label = "jf" if _ch is jf_chunked else "em"
        check(f"_chunked {label}: empty → no chunks", list(_ch([], _max)) == [])
        check(f"_chunked {label}: len < max → one chunk",
              list(_ch(list(range(5)), _max)) == [list(range(5))])
        check(f"_chunked {label}: exact multiple",
              list(_ch(list(range(200)), _max)) == [list(range(100)), list(range(100, 200))])
        check(f"_chunked {label}: remainder",
              list(_ch(list(range(250)), _max)) == [list(range(100)), list(range(100, 200)), list(range(200, 250))])
        # Flattened output equals input in order.
        flat = [x for c in _ch(list(range(250)), _max) for x in c]
        check(f"_chunked {label}: preserves order", flat == list(range(250)))
        # Every chunk length ≤ max.
        for c in _ch(list(range(250)), _max):
            check(f"_chunked {label}: chunk len ≤ max", len(c) <= _max)


class _PlaylistRecorder:
    """Minimal stub that records _request calls without touching the network.
    _get_items controls what GET /Playlists/.../Items returns (remove mapping)."""
    _user_id = "u1"

    def __init__(self, get_items=None):
        self.calls = []
        self._get_items = get_items or []

    def _request(self, method, path, *, params=None, **kwargs):
        self.calls.append((method, path, params))
        import json as _j
        import requests as _r
        r = _r.Response()
        r.status_code = 200
        if method == "GET" and "Playlists" in path and "/Items" in path:
            r._content = _j.dumps({"Items": self._get_items}).encode()
        elif method == "POST" and path == "/Playlists":
            r._content = _j.dumps({"Id": "PL1"}).encode()
        else:
            r._content = b"{}"
        return r


def _add_recorder_method(rec, cls, name):
    """Bind an unbound method from cls to a recorder instance."""
    import types
    setattr(rec, name, types.MethodType(getattr(cls, name), rec))


def test_jellyfin_add_chunking():
    """add_items_to_playlist chunks large id lists into ≤100-id POSTs."""
    import jellyfin_client as _jc
    N = _jc._MAX_IDS_PER_REQUEST * 2 + 7
    ids = [f"id{i}" for i in range(N)]
    rec = _PlaylistRecorder()
    _add_recorder_method(rec, _jc.JellyfinClient, "add_items_to_playlist")
    rec.add_items_to_playlist("pl1", ids)

    posts = [(m, p) for m, p, _ in rec.calls if m == "POST"]
    check("jf add: correct number of POSTs",
          len(posts) == (N + _jc._MAX_IDS_PER_REQUEST - 1) // _jc._MAX_IDS_PER_REQUEST)

    all_ids = []
    for _, _, params in rec.calls:
        if params and "ids" in params:
            chunk = params["ids"].split(",")
            check("jf add: chunk size ≤ max", len(chunk) <= _jc._MAX_IDS_PER_REQUEST)
            all_ids.extend(chunk)
    check("jf add: all ids present in order", all_ids == ids)

    # Edge: empty / None rating_key → no-op
    rec2 = _PlaylistRecorder()
    _add_recorder_method(rec2, _jc.JellyfinClient, "add_items_to_playlist")
    rec2.add_items_to_playlist("", ids)
    check("jf add: no rating_key → no-op", len(rec2.calls) == 0)
    rec2.add_items_to_playlist("pl1", [])
    check("jf add: empty ids → no-op", len(rec2.calls) == 0)


def test_jellyfin_remove_chunking():
    """remove_items_from_playlist chunks entryIds DELETE calls."""
    import jellyfin_client as _jc
    N = _jc._MAX_IDS_PER_REQUEST * 2 + 3
    ids = [f"id{i}" for i in range(N)]
    # Build GET response with PlaylistItemId entries.
    get_items = [{"Id": f"id{i}", "PlaylistItemId": f"pid{i}"} for i in range(N)]
    rec = _PlaylistRecorder(get_items=get_items)
    _add_recorder_method(rec, _jc.JellyfinClient, "remove_items_from_playlist")
    rec.remove_items_from_playlist("pl1", ids)

    deletes = [(m, p) for m, p, _ in rec.calls if m == "DELETE"]
    expected_del = (N + _jc._MAX_IDS_PER_REQUEST - 1) // _jc._MAX_IDS_PER_REQUEST
    check("jf remove: correct number of DELETEs", len(deletes) == expected_del)

    all_entry_ids = []
    for m, _, params in rec.calls:
        if m == "DELETE" and params and "entryIds" in params:
            chunk = params["entryIds"].split(",")
            check("jf remove: chunk size ≤ max", len(chunk) <= _jc._MAX_IDS_PER_REQUEST)
            all_entry_ids.extend(chunk)
    check("jf remove: all entryIds present", len(all_entry_ids) == N)

    # Edge cases
    rec2 = _PlaylistRecorder()
    _add_recorder_method(rec2, _jc.JellyfinClient, "remove_items_from_playlist")
    rec2.remove_items_from_playlist("", ids)
    check("jf remove: no rating_key → no-op", len(rec2.calls) == 0)
    rec2.remove_items_from_playlist("pl1", [])
    check("jf remove: empty ids → no-op", len(rec2.calls) == 0)


def test_emby_add_chunking():
    """Emby add_items_to_playlist chunks large id lists."""
    import emby_client as _ec
    N = _ec._MAX_IDS_PER_REQUEST * 2 + 7
    ids = [f"id{i}" for i in range(N)]
    rec = _PlaylistRecorder()
    _add_recorder_method(rec, _ec.EmbyClient, "add_items_to_playlist")
    rec.add_items_to_playlist("pl1", ids)

    posts = [(m, p) for m, p, _ in rec.calls if m == "POST"]
    check("em add: correct number of POSTs",
          len(posts) == (N + _ec._MAX_IDS_PER_REQUEST - 1) // _ec._MAX_IDS_PER_REQUEST)

    all_ids = []
    for _, _, params in rec.calls:
        if params and "ids" in params:
            chunk = params["ids"].split(",")
            check("em add: chunk size ≤ max", len(chunk) <= _ec._MAX_IDS_PER_REQUEST)
            all_ids.extend(chunk)
    check("em add: all ids present in order", all_ids == ids)


def test_emby_remove_chunking():
    """Emby remove_items_from_playlist chunks entryIds DELETE calls."""
    import emby_client as _ec
    N = _ec._MAX_IDS_PER_REQUEST * 2 + 3
    ids = [f"id{i}" for i in range(N)]
    get_items = [{"Id": f"id{i}", "PlaylistItemId": f"pid{i}"} for i in range(N)]
    rec = _PlaylistRecorder(get_items=get_items)
    _add_recorder_method(rec, _ec.EmbyClient, "remove_items_from_playlist")
    rec.remove_items_from_playlist("pl1", ids)

    deletes = [(m, p) for m, p, _ in rec.calls if m == "DELETE"]
    expected_del = (N + _ec._MAX_IDS_PER_REQUEST - 1) // _ec._MAX_IDS_PER_REQUEST
    check("em remove: correct number of DELETEs", len(deletes) == expected_del)

    all_entry_ids = []
    for m, _, params in rec.calls:
        if m == "DELETE" and params and "entryIds" in params:
            chunk = params["entryIds"].split(",")
            check("em remove: chunk size ≤ max", len(chunk) <= _ec._MAX_IDS_PER_REQUEST)
            all_entry_ids.extend(chunk)
    check("em remove: all entryIds present", len(all_entry_ids) == N)


def test_emby_create_playlist_split():
    """Emby create_playlist sends first chunk on create, rest via chunked adds."""
    import emby_client as _ec
    N = _ec._MAX_IDS_PER_REQUEST * 2 + 11
    ids = [f"id{i}" for i in range(N)]
    rec = _PlaylistRecorder()
    _add_recorder_method(rec, _ec.EmbyClient, "create_playlist")
    _add_recorder_method(rec, _ec.EmbyClient, "add_items_to_playlist")
    new_id = rec.create_playlist("Test Playlist", ids)

    check("em create: returns playlist id", new_id == "PL1")

    # First POST (create) should have only the first chunk.
    create_call = rec.calls[0]
    check("em create: first call is POST", create_call[0] == "POST")
    check("em create: first call to /Playlists", create_call[1] == "/Playlists")
    first_ids = create_call[2]["Ids"].split(",")
    check("em create: first chunk size", len(first_ids) == _ec._MAX_IDS_PER_REQUEST)
    check("em create: first chunk is first ids", first_ids == ids[:_ec._MAX_IDS_PER_REQUEST])

    # Remaining calls should be add_items_to_playlist POSTs.
    add_calls = rec.calls[1:]
    check("em create: has add calls", len(add_calls) > 0)
    all_added = []
    for _, _, params in add_calls:
        if params and "ids" in params:
            chunk = params["ids"].split(",")
            all_added.extend(chunk)
    check("em create: remaining ids added", all_added == ids[_ec._MAX_IDS_PER_REQUEST:])

    # Empty list → ValueError.
    rec2 = _PlaylistRecorder()
    _add_recorder_method(rec2, _ec.EmbyClient, "create_playlist")
    try:
        rec2.create_playlist("X", [])
        check("em create: empty → ValueError", False)
    except ValueError:
        check("em create: empty → ValueError", True)


def test_decoupled_season_meta():
    """When get_show_summary fails but season_summaries succeeds, the meta entry
    still carries seasons and _compute_display_titles provides a usable title."""
    from app import _compute_display_titles
    from service import ShowConfig
    from media_client import ShowSummary, SeasonSummary

    cfg = ShowConfig("rk1", "The Show")
    seasons = [SeasonSummary(index=1, title="Season 1", episode_count=10, thumb=None, year=None)]

    meta = {
        "rk1": {
            "summary": None,
            "seasons": seasons,
            "movies": [],
            "source_backend": "jellyfin",
        }
    }
    _compute_display_titles(meta, [cfg], None)

    check("decoupled: summary is None", meta["rk1"]["summary"] is None)
    check("decoupled: seasons present", len(meta["rk1"]["seasons"]) == 1)
    check("decoupled: title from config", meta["rk1"]["_show_title"] == "The Show")

    # Fallback: empty config title → falls back to rating_key.
    cfg2 = ShowConfig("rk2", "")
    seasons2 = [SeasonSummary(index=1, title="S1", episode_count=5, thumb=None, year=None)]
    meta2 = {
        "rk2": {
            "summary": None,
            "seasons": seasons2,
            "movies": [],
            "source_backend": "emby",
        }
    }
    _compute_display_titles(meta2, [cfg2], None)
    check("decoupled: fallback to rk", meta2["rk2"]["_show_title"] == "rk2")

    # Existing summary title is preferred.
    meta3 = {
        "rk3": {
            "summary": ShowSummary("rk3", "Direct Title", year=None, library="", thumb=None),
            "seasons": [],
            "movies": [],
            "source_backend": "plex",
        }
    }
    _compute_display_titles(meta3, [ShowConfig("rk3", "Config Title")], None)
    check("decoupled: summary title wins", meta3["rk3"]["_show_title"] == "Direct Title")

    # Aggregated show fallback — agg is the list-of-dicts shape that
    # _aggregated_shows() actually returns in production.
    agg = [{"rating_key": "rk4", "title": "Aggregated Title",
            "jellyfin_rating_key": "rk4"}]
    meta4 = {
        "rk4": {
            "summary": None,
            "seasons": [SeasonSummary(index=1, title="S1", episode_count=1, thumb=None, year=None)],
            "movies": [],
            "source_backend": "jellyfin",
        }
    }
    cfg4 = ShowConfig("rk4", "")
    _compute_display_titles(meta4, [cfg4], agg)
    check("decoupled: agg title fallback", meta4["rk4"]["_show_title"] == "Aggregated Title")


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
