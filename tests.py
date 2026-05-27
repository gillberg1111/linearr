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
    check("VALID_PLAYLIST_TYPES has manual and genre",
          set(VALID_PLAYLIST_TYPES) == {"manual", "genre"},
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
