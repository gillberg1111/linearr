# Changelog

All notable changes to Linearr. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [0.1.0] - 2026-05-23

First public release as **Linearr** (initially named *Plex Rotator*; renamed
to avoid trademark concerns with the Plex name and adopt the community
`*arr` naming convention — "linearr" plays on "linear TV", the broadcast-era
term for scheduled programming, which the app emulates). Published to
ghcr.io and tested on Unraid.

### Added
- **Sort modes**: each playlist can switch between **Rotation** (round-robin) and
  **Air Date** (chronological across shows). Switch any time; the future portion
  of the playlist rebuilds, watched portion stays put.
- **Crossover alignment**: in Air Date mode, multi-part crossovers stay together
  across different shows via `Part 1` / `Pt. 2` / `(N)` title detection.
- **Associated movies**: each show on the configure page detects matching movies
  in your movie libraries by word-boundary title match. Per-show toggle reveals
  a picker with a styled **Select all** button.
- **Unwatched-only filter**: per-playlist toggle excludes episodes you've
  already watched anywhere in Plex.
- **Live AJAX preview**: every config change debounces (600 ms) and updates the
  preview list in place via `/api/preview` — no full page reloads.
- **Picker improvements**: pinned "Selected" tray, filter input, Clear-selection
  button. Selection order = rotation order.
- **Preview pagination**: 10/25/50/100/All with Prev/Next; page-size persists
  across navigations via sessionStorage.
- **Reorder rotation**: up/down arrows on the playlist page.
- **Reorder-aware splicing**: add/remove/reorder all preserve already-played
  episodes and rebuild only the future portion.

### Safety
- Runtime guard installs at import: `Episode.delete`, `Show.delete`,
  `Season.delete`, `Movie.delete` raise `RuntimeError` instead of doing anything.
  Only `Playlist.delete` / `Playlist.removeItem` are allowed (pure metadata).

### Tests
- `tests.py` has 31 self-contained unit tests for the rotation/sort/prune logic
  in `rotation.py`. No Plex or network needed.
