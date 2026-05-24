# Changelog

All notable changes to Linearr. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.0.3] - 2026-05-24

### Changed
- **Buy Me a Coffee button** is now a styled in-app link sized to match the
  other primary buttons (was the larger BMC JS widget). Same destination,
  same blue, just smaller and visually consistent with the rest of the UI.

## [1.0.2] - 2026-05-24

First stable release. Adds the auto-sync background loop and per-playlist
Auto-update toggle, plus visual identity (logo, favicon, banner, blue
accent palette) and the project's settled name (Linearr).

### Added
- **Auto-sync**: the existing background sweep now also splices newly-aired
  episodes and new seasons (within each show's configured range) into managed
  playlists; episodes deleted from Plex drop out. Already-played portion is
  preserved. Controlled by the new `AUTO_SYNC` env var (default `true`).
- **Per-playlist Auto-update toggle**: in addition to the global env var,
  each playlist has its own **Auto-update: Enabled / Disabled** pill on the
  playlist detail page. Disabled playlists are skipped by the scheduler —
  useful for "locked" curated playlists you don't want changing on their own.
- **Auto-update choice on configure page**: when creating a new playlist,
  there's now an **Auto-update** toggle alongside Sort and Filter so you can
  set the initial state at creation rather than create-then-toggle.
- **Buy Me a Coffee** support button in the landing-page footer (only on
  the playlists landing page — not on the configure or detail flows so it
  doesn't intrude while you're working).
- **Logo + visual identity**: banner, favicon (16/32/64), and Unraid
  template icon. Topbar brand mark swapped from a CSS gradient placeholder
  to the real logo image.
- **Tagline**: *"The missing show sequencer for Plex. Automated round-robin
  rotation and chronological crossover alignment for your episodes (and
  their movies)."* — appears on the README, Dockerfile image label, Unraid
  Overview/Description, and the app's landing page subtitle.

### Changed
- **Accent color** swapped from Plex orange (`#e5a00d`) to a balanced blue
  (`#4d96ff`, `rgb(77, 150, 255)`) across the entire UI — buttons, pills,
  toggles, brand mark, ambient page glow. Buy Me a Coffee button matches.
  This is a deliberate visual distinction from Plex's own brand (Plex
  trademark concerns).
- **Project rename**: codebase renamed from *Plex Rotator* → briefly
  *Plaitarr* → **Linearr**. Container image now publishes to
  `ghcr.io/gillberg1111/linearr`. GitHub auto-redirects old repo URLs.

### Fixed
- **Season-range validation**: the configure page now prevents picking an
  "End at" season earlier than "Start from". When you change the start
  season, end-season cards below it are hidden and any invalid prior
  selection auto-resets to **All**. Server-side validation also rejects
  end &lt; start as a safety net.

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
