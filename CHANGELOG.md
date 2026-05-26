# Changelog

All notable changes to Linearr. Format loosely follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [1.5.1] - 2026-05-26

Bug-fix release addressing issues found during post-v1.3.0 testing.

### Fixed

- **False "Plex Only" / "Jellyfin Only" badges in the show picker.** The
  cross-backend dedup key in `_aggregated_shows()` and `_resolve_genre_shows()`
  used `year or 0`, turning `None` into `0`. When one backend carried a year and
  the other didn't, the show appeared as two separate entries with one-backend
  badges. Both functions now match `titles_match()` semantics: year only
  disambiguates when both sides carry a non-None year that differs.
- **Unselecting a show in the picker sent it to the bottom of the grid.** The
  picker now records each tile's original DOM position at init and inserts
  unchecked tiles back at their correct spot instead of appending.
- **Shuffle hint text reworded** from "A stable seed is generated at create
  time" to "The shuffle order is fixed until you hit Reshuffle on the playlist
  page."
- **Pill order normalized** across all three templates: Rotation, Air Date,
  Blocks, Weighted, Shuffle (was: Rotation, Blocks, Weighted, Air Date, Shuffle
  on two pages).
- **Number input browser spinners removed** via CSS for weight and block-size
  fields.
- **Backend badge background opacity increased** for better readability on
  poster thumbnails.

### Files touched

`app.py` · `service.py` · `static/picker.js` · `static/style.css` ·
`templates/configure.html` · `templates/new_genre.html` ·
`templates/playlist.html` · `CHANGELOG.md`.

## [1.5.0] - 2026-05-26

Manual crossover grouping for air_date mode. When title-based Part N detection
misses a crossover (e.g. "Buffy — Fool for Love" / "Angel — Darla"), users can
explicitly link specific episodes across shows and set their play order.

### Added

- **Crossover groups** — two new tables (`crossover_groups` + `crossover_links`)
  let users manually link specific episodes across different shows. Within the
  same air date, grouped episodes sort before non-grouped ones and play in
  the user-defined `sort_index` order.
- **Sort key integration.** `rotation.air_date_sequence()` gains an optional
  `crossover_map` parameter. The sort key inserts `(0, group_id, sort_idx)`
  between `air_date` and `part_number`, so manually grouped episodes take
  priority over title-based Part N detection on the same day.
- **Crossover groups UI section** on the playlist detail page (air_date mode
  only). Create groups, add episodes by show + season + episode number, remove
  episodes, and delete groups. Each action rebuilds the playlist tail on every
  enabled backend.
- **New routes:** `POST /playlist/<id>/crossover/create`,
  `/crossover/<group_id>/add`, `/crossover/<group_id>/delete`,
  `/crossover/link/<link_id>/remove`.
- **`PlaylistView.crossover_groups`** field populated from
  `db.list_crossover_groups()` — available in the playlist template as
  `playlist.crossover_groups`.

### Changed

- **`rotation.compose()`** and **`rotation.rebuild_tail()`** gain a
  `crossover_map` kwarg (only used by air_date mode, ignored by others).
- **`service._rebuild_tail_on()`** and **`service._rebuild_playlist_tails()`**
  build and pass `crossover_map` when the sort mode is `air_date`.
- **`service.get_playlist_view()`** populates the new `crossover_groups`
  field.

### Database (additive only)

- New table `crossover_groups` (id, playlist_id FK, label, sort_index)
- New table `crossover_links` (id, group_id FK, show_rating_key, season,
  episode, sort_index)
- New helpers: `db.list_crossover_groups`, `db.create_crossover_group`,
  `db.delete_crossover_group`, `db.add_crossover_link`,
  `db.remove_crossover_link`

### Tests

- 136 passing (up from 131). New: 5 covering crossover_map sort key
  (grouped-before-non-grouped, group_id ordering, part_number fallback),
  compose passthrough, and rebuild_tail passthrough.

### Files touched

`rotation.py` · `service.py` · `db.py` · `app.py` ·
`templates/playlist.html` · `static/style.css` · `tests.py` ·
`CHANGELOG.md`.

## [1.4.0] - 2026-05-26

Dynamic genre playlists. Instead of picking shows one-by-one, you now choose
a genre (or several) and Linearr builds the playlist from every matching show
in your library. Background sync re-queries your library and auto-adds new
shows that match. Exclude individual shows and they stay excluded across syncs.

### Added

- **Dynamic genre playlists** — new playlist type alongside manual. Created via
  the **+ Genre** button in the topbar. Input genre names (comma-separated,
  e.g. "Sci-Fi, Drama"), preview matches live before creating.
- **Genre auto-discovery on sync.** Every background sweep re-queries the
  configured backends for genre-matching shows. New matches are auto-added
  (with default settings: Season 1+, no specials). Excluded shows stay
  excluded — the `is_excluded` flag persists across syncs.
- **Per-show exclude button** on the playlist rotation list (genre playlists
  only). Removes the show from rotation but keeps it in the DB so future
  syncs don't re-add it. An **Excluded shows** section below the rotation
  list offers a **Re-include** button per show.
- **`MediaClient.list_shows_by_genres(genres)`** — new ABC method.
  - **Plex:** `section.search(genre=...)` per TV section, deduplicated by
    ratingKey.
  - **Jellyfin:** shared `_list_series_via_items(extra_params)` with
    `genres="pipe|delimited"` query param for multi-genre OR matching.
- **`service._resolve_genre_shows(genres, target_backends)`** — queries each
  backend, deduplicates across backends via title+year, fills in cross-backend
  IDs for 'both' setups.
- **`service._genre_sync_discover(playlist_id, row, configs)`** — called
  during every sync for genre playlists. Compares discovered shows against
  existing (including excluded) and adds only genuinely new ones.
- **`service.set_show_excluded(playlist_id, rk, excluded)`** — soft-delete
  or re-include a single show, then rebuilds tails.
- **New routes:** `GET/POST /new/genre` (genre playlist creation + preview),
  `POST /playlist/<id>/exclude`, `POST /playlist/<id>/include`.
- **New template:** `templates/new_genre.html` — genre name input, genre
  comma-separated input, backend picker, sort mode pill, unwatched-only
  toggle, auto-update toggle, preview section, create button.

### Changed

- **`service.PlaylistView`** gains `playlist_type` (default `"manual"`),
  `genre_filter` (nullable CSV string), and `excluded_shows` (list of
  soft-deleted show dicts).
- **`service.get_playlist_view`** splits `list_shows` rows into active
  (`is_excluded=0`) and excluded (`is_excluded=1`).
- **`service._rebuild_playlist_tails`** filters out `is_excluded=True`
  configs so excluded shows never contribute episodes.
- **`templates/playlist.html`** subtitle shows a **Genre** badge with the
  genre filter when `playlist_type == 'genre'`. Exclude button per row on
  genre playlists. New "Excluded shows" section with re-include buttons.
- **`templates/base.html`** topbar gains a **+ Genre** button alongside
  **+ New playlist**.

### Database (additive only)

- `managed_playlists.playlist_type TEXT NOT NULL DEFAULT 'manual'
  CHECK(playlist_type IN ('manual','genre'))`
- `managed_playlists.genre_filter TEXT` (comma-separated genre names)
- `playlist_shows.is_excluded INTEGER NOT NULL DEFAULT 0`
- `db.VALID_PLAYLIST_TYPES` tuple constant
- New helpers: `db.set_genre_filter`, `db.set_show_excluded`

### Tests

- 131 passing (up from 117). New: 14 covering `_parse_genre_csv` parse +
  whitespace tolerance, `ShowConfig.is_excluded` default and explicit,
  `PlaylistView` genre-field defaults, `VALID_PLAYLIST_TYPES` shape, genre
  CSV round-trip, `weight + is_excluded` field independence.

### Files touched

`media_client.py` · `plex_client.py` · `jellyfin_client.py` · `db.py` ·
`service.py` · `app.py` · `templates/new_genre.html` ·
`templates/base.html` · `templates/playlist.html` · `static/style.css` ·
`tests.py` · `CHANGELOG.md`.

## [1.3.0] - 2026-05-26 - 2026-05-26

Three new sequencing modes alongside Rotation and Air Date. Existing
playlists keep their current mode and behave identically — the new modes
are opt-in per playlist.

### Added

- **Weighted Rotation** (`sort_mode = 'rotation_weighted'`). Per-show
  weight on `playlist_shows.weight` (default 1). New
  `rotation.interleave_weighted(shows_episodes, weights)` takes
  `weights[i]` episodes from show *i* per cycle, falling back to
  partial-take when a show is short. Solves the "The Simpsons drowns out
  Firefly" problem. Edit weights inline on the playlist detail page when
  the playlist is in this mode.
- **Block Scheduling** (`sort_mode = 'rotation_blocks'`). A single
  playlist-wide `block_size` integer on `managed_playlists` (default 1 =
  current rotation behavior). New `rotation.interleave_blocks(...)`. Pill
  toggle on the playlist detail page reveals a block-size adjuster when
  this mode is active.
- **Intelligent Shuffle** (`sort_mode = 'shuffle_chronological'`). New
  `rotation.shuffle_chronological(shows_episodes, seed)` produces a
  random sequence where each show's episodes stay in chronological order
  and same-show consecutive plays are avoided when possible (with a
  forced-fallback when one show vastly outnumbers the rest). A seed is
  auto-generated at creation; users can hit **Reshuffle** on the playlist
  detail page to regenerate it.
- **`rotation.VALID_SORT_MODES`** constant — a tuple of every supported
  value, used as the single source of truth for validation in
  `db.set_sort_mode`, `service.create_managed_playlist`,
  `service.set_playlist_sort_mode`, and `app.change_sort_mode`.
- **New service helpers**: `set_playlist_block_size`,
  `reshuffle_playlist` (regenerates seed + rebuilds tails),
  `set_show_weight`. Each rebuilds tails on every enabled backend after
  updating the relevant value.
- **New routes**: `POST /playlist/<id>/block_size`,
  `POST /playlist/<id>/reshuffle`, `POST /playlist/<id>/weight`.

### Changed

- **`rotation.compose()` and `rotation.rebuild_tail()`** gained keyword-
  only args `weights`, `block_size`, `shuffle_seed`. Backward compat:
  unused args are ignored on old modes. For rotation_* modes the
  remainder-per-show approach is preserved; for air_date and shuffle the
  full canonical sequence is composed once, then kept items are dropped
  (preserves the original randomized/dated order).
- **`service._rebuild_tail_on` and `_rebuild_playlist_tails`** thread the
  new params through. Both default values from the row when callers
  don't override.
- **Configure page sort-mode pill** expanded from 2 options to 5 (with
  `.pill-toggle-5` modifier that wraps on narrow screens). Reactive
  hint text and conditional visibility for the block-size input and
  per-show weight steppers.
- **Playlist detail subtitle** shows the new mode label and block size
  when relevant.

### Database (additive only)

- `playlist_shows.weight INTEGER NOT NULL DEFAULT 1`
- `managed_playlists.block_size INTEGER NOT NULL DEFAULT 1`
- `managed_playlists.shuffle_seed INTEGER` (nullable)

### Tests

- 117 passing (up from 98). New: 18 covering the three sequencing modes
  — depletion-fallback for weighted, exact block patterns for blocks,
  per-show chronological order preservation + no-consecutive-when-
  possible + forced-fallback for shuffle, dispatch table, `rebuild_tail`
  in each new mode, and `VALID_SORT_MODES` shape.

### Files touched

`rotation.py` · `service.py` · `db.py` · `app.py` ·
`templates/configure.html` · `templates/playlist.html` · `static/style.css` ·
`tests.py`.

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
