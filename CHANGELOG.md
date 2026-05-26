# Changelog

All notable changes to Linearr. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

## [1.2.0] - 2026-05-26

Quick-wins release based on a code review. No backend semantics change; new
features are additive and opt-in.

### Added

- **Manual "Sync Now" button** on the playlist detail page. Hits a new
  `POST /playlist/<id>/sync` route that calls `service.sync_playlist(id,
  force=True)`, bypassing the per-playlist `auto_sync` opt-out so a manual
  click always works. Flash reports `(added, removed)` counts.
- **Per-episode exclusions.** Each show's configure card now has an
  *Exclude individual episodes* expander. Lazy-loaded on open via a new
  `GET /api/episodes/<rk>?b=<backend>` endpoint, grouped by season into
  collapsible sections. Uncheck an episode to skip it everywhere this show
  appears in this playlist. Excluded set is filtered out of
  `_episodes_for_config()` before rotation/air-date composition, so it
  applies uniformly across both backends.
- **`service.sync_playlist(playlist_id, force=False)`** signature gained
  the optional `force` kwarg. Scheduler always uses `force=False`;
  user-initiated Sync Now uses `force=True`.

### Changed

- **`_rebuild_playlist_tails` helper** consolidates the 5 near-identical
  per-backend loops in `service.py` (`add_shows_to_playlist`,
  `reorder_shows`, `set_playlist_sort_mode`, `set_playlist_unwatched_only`,
  `sync_playlist`). Pure refactor — zero behavior change. `sort_mode` and
  `unwatched_only` can be passed as kwargs by callers that have just
  written a new value to the DB but haven't refetched the row.

### Database (additive only)

- `playlist_shows.excluded_episode_keys TEXT NOT NULL DEFAULT ''` — CSV of
  `S:E` pairs (e.g. `"1:1,3:14"`). New helper `db.set_excluded_episodes()`.

### Tests

- 87 → 98 passing. New: CSV parse/serialize round-trips, malformed-input
  tolerance, default-empty behavior, sorted output stability.

### Files touched

`service.py` · `db.py` · `app.py` · `templates/playlist.html` ·
`templates/configure.html` · `static/style.css` · `tests.py`.

## [1.1.0] - 2026-05-24

**Linearr is now for Plex *and* Jellyfin.** Each managed playlist can target
Plex, Jellyfin, or both (mirrored to each server). Single-backend installs
look and behave exactly like v1.0.7 — the dual-backend UI only appears when
both backends are configured.

### Added

- **Jellyfin support.** New `jellyfin_client.py` implements the full
  `MediaClient` contract against the Jellyfin REST API. Authenticated via
  username + password against `POST /Users/AuthenticateByName` (API keys are
  broken on the playlist endpoints we need — see Jellyfin issue #15600).
  Configure with `JELLYFIN_URL` / `JELLYFIN_USERNAME` / `JELLYFIN_PASSWORD`.
- **Triple-pill backend picker** on the configure page when both backends
  are configured: `Push to: Both / Plex / Jellyfin`. Hidden when only one
  backend is available.
- **Cross-backend show matching** at add-time: shows added to a "Both"
  playlist are matched on the other side by title + year. The matched
  Plex/Jellyfin IDs are persisted alongside the show row.
- **Heal-on-sync.** For "Both" playlists, every sync re-attempts matching
  for shows missing an ID on one side — so adding a show to your
  previously-empty Jellyfin library auto-resolves on the next sweep without
  any manual intervention.
- **Missing-side warning banner** on the configure and playlist pages,
  listing shows that aren't on every targeted backend. Informational, never
  blocking — you can deliberately add a show to a "Both" playlist that only
  exists on one server today and expect to add it to the other later.
- **Backend badges** on playlist cards (index page), playlist detail
  header, and individual show rows when applicable.
- **`_aggregated_shows()`** in `app.py` lists shows across every configured
  backend and dedupes via title+year normalization, so the show picker is
  one unified list with per-backend overlays.
- **41 new unit tests** in `tests.py` (now 87/87 passing):
  - 18 covering the Jellyfin DELETE safety guard against every dangerous
    endpoint category (`/Items`, `/Library/VirtualFolders`, `/Users/{id}`,
    image deletes, recordings, plugins, etc.).
  - 4 verifying the Plex `Episode/Show/Season/Movie.delete()` monkey-patch
    is actually applied on import (defense-in-depth verification).
  - 15 covering the title-match helper across normalization, year
    disambiguation, None handling.
  - 8 covering the service-layer dispatch (`ShowConfig.id_for`,
    `_backends_for`, `_find_match`, etc.).

### Changed

- **Architecture: `MediaClient` interface.** Every backend operation now
  goes through the abstract base class in `media_client.py`. Plex's existing
  module-level functions remain as thin shims for back-compat with external
  callers; internally everything uses the interface. This is what makes
  dual-backend possible.
- **Tail-rebuild deduplication.** The 6 near-identical 15-line tail-rebuild
  blocks across `add_shows_to_playlist`, `reorder_shows`,
  `set_playlist_sort_mode`, `set_playlist_unwatched_only`, `sync_playlist`,
  and `add_shows_to_playlist` are collapsed into a single
  `_rebuild_tail_on()` primitive. ~80 lines of duplication eliminated.
  Per-backend dispatch loops over the new primitive.
- **Jellyfin's native atomic playlist replace.** `JellyfinClient`
  overrides `replace_playlist_items()` with a single `POST /Playlists/{id}`
  (UpdatePlaylistDto.Ids) — Jellyfin clears `LinkedChildren` then re-adds
  in one transaction. No PlaylistItemId bookkeeping, no partial-state
  window. (Plex still uses the existing incremental remove/add.)
- **`PlexClient` lazy connection.** The PlexServer instance is created on
  first API call instead of at `PlexClient.__init__`. If Plex is briefly
  unreachable when the app starts, the app still boots and degrades
  gracefully per request — instead of hard-crashing during init.
- **DB schema (additive, no data movement):**
  - `managed_playlists.backend` (`plex` | `jellyfin` | `both`, default `plex`)
    with CHECK constraint enforcement on new DBs and helper-layer
    validation on migrated ones.
  - `managed_playlists.jellyfin_playlist_id` (nullable)
  - `playlist_shows.plex_show_item_id` and `jellyfin_show_item_id` (both
    nullable; one-time backfill copies `show_rating_key` into
    `plex_show_item_id` for legacy rows since they were all Plex-originated)
  - `playlist_shows.jellyfin_movie_item_ids` (nullable, comma-separated)

### Safety

- **HTTP-layer DELETE safety guard** in `jellyfin_client.py` deny-by-default
  for every outbound DELETE; only `/Playlists/{id}/Items` (removing items
  from a playlist) is allow-listed. The intentional `delete_playlist()`
  bypass is the single audited route that hits `DELETE /Items?ids=X` —
  it first verifies the target is a playlist via `GET /Playlists/{id}`,
  then sets a one-shot bypass flag. Every other DELETE raises
  `JellyfinSafetyError` before the request goes out.
- **Plex safety guard unchanged.** The module-level monkey-patch of
  `Episode.delete` / `Show.delete` / `Season.delete` / `Movie.delete`
  remains, now also verified by unit tests.

### Single-backend behavior

Plex-only installs see no UI change. The picker is hidden, backend badges
are hidden, the warning banner only fires if you somehow have a "both" row
in the DB. Every operation runs exactly once with the Plex client — the
dispatch loop short-circuits to a single iteration. Tail-rebuild semantics
on Plex are byte-for-byte identical to v1.0.7.

### Fixed

- **Empty `FLASK_SECRET=` no longer crashes session-using routes.** Previously
  an empty-string value (set in `.env` but blank) was passed through to Flask
  instead of falling back to the dev default — every redirect that called
  `flash()` 500'd, including the success page after creating a playlist
  (Plex/Jellyfin playlists were created, but the user saw an error). Now any
  empty/unset value falls back to the dev secret.

### Migration

Migrating from v1.0.7: no manual steps. On first boot, `db.init_db()`
runs the additive ALTER TABLEs and backfills `plex_show_item_id` from
`show_rating_key` for every legacy row.

### Testing notes

`tests.py` is stdlib-only, no Plex or Jellyfin connection needed. Integration
testing against real servers is a manual step before tagging.

## [1.0.7] - 2026-05-24

### Added
- **Acknowledgments section** in the README disclosing that Linearr was built
  collaboratively with Claude Code (Anthropic's AI coding assistant).
  Architecture, testing, deployment, and maintenance are mine.

## [1.0.6] - 2026-05-24

### Fixed
- **Unraid Docker tab icon**: switched the CA template's `<Icon>` (and the
  `ca_profile.xml` icon) from SVG to the 256×256 PNG. Unraid's Apps tab
  renders SVG fine, but the Docker tab's installed-container list uses a
  different rendering path and was leaving the SVG icon blank. PNG works in
  both views.

## [1.0.5] - 2026-05-24

### Changed
- **Repository restructured for Unraid Community Applications submission:**
  - `unraid-template.xml` moved to `templates/linearr.xml` (CA's required
    folder layout).
  - `ca_profile.xml` added at the repo root with repository-wide metadata
    (description, support links, donation).
  - CA Icon now points at the SVG variant in `images/`.

## [1.0.4] - 2026-05-24

### Added
- **Screenshots in README** — four UI screenshots (landing, show picker,
  selected tray, configure page) with library posters blurred for privacy.
  Linked from the table of contents.

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
