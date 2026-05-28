<p align="center">
  <img src="images/banner.png" alt="Linearr — The missing show sequencer for Plex and Jellyfin" width="900">
</p>

# Linearr

### The missing show sequencer for Plex and Jellyfin.
Shows, genres, franchises — Automated. Sequenced. Yours.

---

A web app that builds and maintains custom playlists across multiple TV shows
(and their associated movies) — on **Plex**, **Jellyfin**, or **both at once**.
Configure either or both backends; each playlist independently targets Plex,
Jellyfin, or "Both" (mirrored to each server in lockstep). Single-backend
installs see no UI change — the picker only appears when both backends are
configured.

Five ways to order episodes:

**Rotation** — round-robin across shows in the order you picked them.

```
Show A S01E01
Show B S01E01
Show A S01E02
Show B S01E02
```

**Block Scheduling** — N consecutive episodes per show before rotating
("3 Simpsons, then 3 Futuramas, then 3 South Parks...").

**Weighted Rotation** — give heavy shows more slots per cycle ("The Simpsons
gets 3 episodes for every 1 of Firefly").

**Air Date** — chronological across every show, like Tuesday-night TV from
2008. **Multi-part crossovers stay aligned** across different shows via
title parsing (`Part 1` / `Pt. 2` / `(1)`), with **manual crossover grouping**
for edge cases the title heuristic misses.

**Intelligent Shuffle** — random sequence, but each show's episodes stay in
chronological order and same-show consecutive plays are avoided when possible.
Seed-based (deterministic); reshuffle any time.

Switch any playlist between modes at any time. Add a series' movies (e.g.
*Psych: The Movie*, *Mr. Monk's Last Case*) without leaving the configure
screen — they auto-detect from your movie library by title. Background
pruning keeps watched episodes from piling up, with a configurable
fall-asleep buffer.

> [!IMPORTANT]
> This app **never deletes media files or library items from Plex or Jellyfin.**
> It only manages playlists. Two layers of safety guard back this up:
> - **Plex** — a runtime monkey-patch disables `delete()` on Episodes, Shows,
>   Seasons, and Movies in the python-plexapi client.
> - **Jellyfin** — an HTTP-layer guard deny-by-default refuses every outbound
>   `DELETE` request; only `DELETE /Playlists/{id}/Items` is allow-listed.
>   The intentional `delete_playlist()` is the single audited bypass and
>   verifies the target is a playlist before each call.
>
> Even an internal bug couldn't remove anything. Both guards are
> defense-in-depth and have unit tests verifying they hold.

---

## Screenshots

<p align="center">
  <img src="images/01-landing.png" alt="Landing page" width="900">
  <br><sub><em>Landing page — your playlists live here, with the support link in the footer.</em></sub>
</p>

<p align="center">
  <img src="images/02-new-playlist.png" alt="Show picker" width="900">
  <br><sub><em>Show picker — filter through your TV libraries and pick the shows you want in the rotation.</em></sub>
</p>

<p align="center">
  <img src="images/03-selected.png" alt="Selected tray" width="900">
  <br><sub><em>As you pick, shows jump into a pinned "Selected" tray at the top. Order in the tray = the rotation order.</em></sub>
</p>

<p align="center">
  <img src="images/04-configure.png" alt="Configure page" width="900">
  <br><sub><em>Per-show configure: season range, specials toggle (only when Season 0 exists), sort mode (Rotation / Air Date), unwatched-only filter, and Auto-update toggle. Every change updates the preview list below without reloading the page.</em></sub>
</p>

---

## Table of contents

- [Screenshots](#screenshots)
- [Features](#features)
- [Quick start](#quick-start)
- [Install on Unraid](#install-on-unraid)
- [Install with Docker Compose](#install-with-docker-compose)
- [Install with `docker run`](#install-with-docker-run)
- [Install without Docker (Python)](#install-without-docker-python)
- [Finding your Plex token](#finding-your-plex-token)
- [Jellyfin authentication](#jellyfin-authentication)
- [Configuration reference](#configuration-reference)
- [Usage walk-through](#usage-walk-through)
- [How adds, removes, sort changes, and prunes work](#how-adds-removes-sort-changes-and-prunes-work)
- [Safety guarantee](#safety-guarantee)
- [Running tests](#running-tests)
- [Updating](#updating)
- [Troubleshooting](#troubleshooting)
- [Architecture](#architecture)
- [Contributing](#contributing)
- [Support the project](#support-the-project)
- [Acknowledgments](#acknowledgments)

---

## Features

- **Plex AND Jellyfin support.** Configure either or both. Each playlist
  targets Plex, Jellyfin, or "Both" (mirrored to each server independently
  using each server's own library state and watch state). When both backends
  are configured, a triple-pill picker appears on the configure page:
  `Push to: Both / Plex / Jellyfin`. With only one backend configured, the
  picker is hidden and that backend is used.
  - **Cross-backend matching:** shows added to a "Both" playlist are matched
    on the other side by **TVDB ID first**, then normalized title + year as
    a fallback. TVDB ID bridges cases where backends disagree on the title
    (e.g. "Stargirl" on Plex ↔ "DC's Stargirl" on Jellyfin). Matched IDs
    are persisted so each backend's playlist references the correct local item.
  - **Heal-on-sync:** every sync re-attempts matching for shows missing an
    ID on one side, using the same TVDB-first strategy. Add a show to a
    previously-empty Jellyfin library and the next sync auto-resolves it onto
    every "Both" playlist that should contain it — no manual reconciliation.
  - **Missing-side warning:** an informational (never blocking) banner lists
    any shows that aren't on every targeted backend. Add the show to the
    missing library and the next sync heals it automatically. Or use the
    **Link manually…** disclosure in the banner to pick the exact show on
    the other backend — useful when the TVDB ID is missing on one side and
    the titles differ enough that auto-matching fails.
- **Five sort modes per playlist:**
  - **Rotation** — round-robin in the order you picked shows.
  - **Block Scheduling** — N consecutive episodes from each show before
    rotating. Single playlist-wide block size.
  - **Weighted Rotation** — per-show weight (1–20); heavier shows get more
    episodes per cycle. Edit weights inline on the playlist page.
  - **Air Date** — chronological across every show; multi-part crossovers
    stay in order via `Part 1` / `Pt. 2` / `(N)` title parsing, with
    **manual crossover grouping** for linking specific episodes across shows
    when the title heuristic misses.
  - **Intelligent Shuffle** — random sequence with per-show chronological
    order preserved and no same-show consecutive plays when avoidable.
    Seed-based; hit **Reshuffle** for a new order.
  - Toggle a playlist between modes any time — already-watched portion is
    untouched; the future portion regenerates instantly.
- **Per-show season range** — start from any season, end at any season; skip
  pilots or bad final seasons. Single-season shows skip the picker entirely
  with a clean "all N episodes included" note.
- **Smart specials** — Season 0 toggle only appears on shows that actually
  have specials. When enabled, specials slot in by air date.
- **Per-show "Include associated movies"** — for each show, the app searches
  your movie libraries by word-boundary title match (so *Mr. Monk's Last
  Case: A Monk Movie* attaches to *Monk*, and *Psych: The Movie* attaches to
  *Psych*). If matches are found, an in-line toggle appears; flipping it on
  reveals a poster grid + **Select all** button. Movies sort by air date in
  Air Date mode, or play at the end of their show's chronology in Rotation
  mode.
- **Unwatched-only filter** — per-playlist toggle that excludes any episode
  you've already watched anywhere in Plex (not just inside the playlist).
- **Show picker with selected tray** — clicking a poster moves it into a
  pinned tray at the top so you always see your picks; filter input;
  Clear-selection button.
- **Live episode preview (AJAX, no page reloads)** — every config change
  (sort mode, season range, specials, movies, unwatched filter) triggers a
  debounced fetch that swaps just the preview list in place. Client-side
  pagination 10/25/50/100/All with persistent page-size across sessions.
- **Add shows mid-rotation** — new shows splice in from your current
  playback point forward; watched portion stays intact.
- **Remove a show** — strips every one of its episodes (and associated
  movies) from the playlist. Files and library items are never touched.
- **Reorder the rotation** — up/down arrows on the playlist page rebuild
  the future portion to follow the new order.
- **Auto-prune watched** — keeps the last N watched episodes as a
  fall-asleep buffer; removes older watched ones every 10 minutes (both
  configurable).
- **Auto-sync new episodes** — same 10-minute sweep also splices
  newly-aired episodes and new seasons into your playlists automatically.
  Episodes removed from your Plex/Jellyfin library drop out. Toggle off
  **globally** with `AUTO_SYNC=false`, or **per playlist** with the
  **Auto-update** pill on the playlist's detail page (defaults to Enabled).
  Disabled playlists are skipped on every sweep and stay locked until you
  edit them.
- **Dynamic genre playlists** — click **+ New playlist** and choose
  **By Genre** to create a playlist that auto-populates from your library
  by genre. Background sync re-queries your library every sweep and auto-adds
  new shows matching the chosen genres. Exclude individual shows and they stay
  excluded across syncs. Genre selection uses a **pill picker** — Linearr
  scrapes all genres from your connected backends on first launch (and weekly
  thereafter) and renders them as selectable buttons, so you don't have to type
  exact genre names. When creating, the block-size and weight fields appear and
  hide automatically as you toggle between sort modes.
- **Smart playlist rules** — in **Genre mode**, select genres via pill
  buttons; in **Smart rules mode**, build a rule set from these types: Genre
  (include), Year min/max, Status (Ended/Continuing), Content rating,
  Season count min/max, and Rating min. Genre rules narrow the candidate pool;
  other rule types combine with AND logic as post-filters. Adding or removing
  a rule triggers an immediate sync to keep the show list current.
- **Metadata refresh** — a **Refresh metadata** button in the maintenance
  section of each playlist page asks every configured backend to re-fetch
  metadata for all shows in the playlist (Plex: `PUT /library/metadata/{id}/refresh`;
  Jellyfin: `POST /Items/{id}/Refresh`). Useful when air dates look wrong
  or a newly added season isn't appearing. The refresh is server-side
  asynchronous — the flash message says "queued" to set the right expectation.
- **Manual "Sync Now" button** — force an immediate sync on any playlist,
  bypassing the auto-update toggle. Reports added/removed counts.
- **Per-episode exclusions** — exclude specific episodes from a show within
  a playlist (season-grouped accordion picker, lazy-loaded on expand).
  Applies uniformly across both backends.
- **Manual crossover grouping** (air_date mode) — when the title-based
  Part N heuristic misses a crossover (e.g. "Buffy — Fool for Love" /
  "Angel — Darla"), link specific episodes across shows into an explicit
  group with a user-defined play order.
- **Cover art everywhere** — poster grids, season cards, playlist tiles; the
  thumbnail proxy keeps every Plex token / Jellyfin access token server-side.
- **Never destructive** — two-layer safety guard refuses any backend API
  call that could delete media or library items (Plex's monkey-patch on
  `Episode/Show/Season/Movie.delete` + Jellyfin's HTTP-layer DELETE allow-list).
- **Playlist stats** — after every sync, the current episode count is stored
  for each playlist and shown on the detail page. The watched-episode fraction
  is intentionally not displayed: because Linearr prunes watched episodes down
  to the `WATCHED_KEEP` buffer, a watched percentage would always read near
  zero and be misleading.
- **REST API** (`/api/v1/`) — JSON endpoints for external integrations
  (Home Assistant, Sonarr webhooks, shell scripts). All routes require a
  bearer token or `?api_key=` query param. The API key is auto-generated on
  first boot and shown on the new **Settings** page (`/settings`, linked in
  the top bar); pin it across restarts with `LINEARR_API_KEY`. Endpoints:
  list all playlists, get playlist detail + rules, trigger a sync, list
  configured backends with health checks, get genre cache, get playlist stats.
- **Outbound webhooks** — configure one or more webhook URLs on the Settings
  page and Linearr will POST a JSON payload when a playlist is created,
  updated (episodes added or removed), or deleted. Works with Home Assistant,
  Ntfy, Gotify, Pushover, Discord, Slack, or any HTTP endpoint that accepts
  a POST. Each URL has an optional label; a "Send test" button verifies
  delivery before you rely on it. Payload includes event type, timestamp,
  playlist metadata, and (for syncs) added/removed counts. Delivery runs in
  a background thread — a failing endpoint is logged but never interrupts a
  sync or any other operation.
- **Franchise playlists** — a third creation mode alongside By Show and By
  Genre. Build a chronologically ordered playlist mixing movies, full
  series, individual seasons, and individual episodes — perfect for MCU
  watch orders, Star Wars chronological viewing, Star Trek timelines, etc.
  17 pre-baked franchises ship with the app sourced from curated community
  Trakt.tv lists: MCU, Star Wars, DCEU, DCU (James Gunn's new universe),
  Arrowverse, Star Trek, Stargate, Doctor Who, Buffy & Angel, X-Men,
  Mission: Impossible, Jurassic Park, MonsterVerse, John Wick, Alien &
  Predator, Conjuring Universe, James Bond. Override any with a custom
  Trakt list URL per-playlist. Items not yet in your library are flagged
  in red and added automatically once you add them and the next sync runs.
- **Franchise Playlist Maker** — an in-app visual builder at
  `/franchise-maker` for creating fully custom franchise watch orders.
  Search TMDB for movies and TV shows, browse seasons and episodes inline,
  add items individually or with **+ Add Series** to drop all seasons of a
  show at once. Drag to reorder. **Import from Trakt URL** populates the
  editor with any public Trakt list as a starting point so you don't
  start from scratch. Editing a pre-baked franchise creates a custom copy
  — the bundled list stays untouched — and adds a **Restore default**
  button to revert. Requires a free TMDB API key (v3 key or v4 Read
  Access Token), configured on the Settings page.
- **Per-playlist pruning toggle** — every playlist has a Pruning on/off
  toggle on the configure page (default on for show/genre playlists, off
  for franchise playlists). Disabling pruning keeps every episode in the
  playlist regardless of watch state.
- **Mobile-responsive layout** — breakpoints at 768 px (tablet) and 480 px
  (phone). Poster grids narrow, config cards stack, the builder toolbar wraps,
  commit button goes full-width, and secondary topbar buttons hide on small
  screens.

---

## Quick start

```bash
git clone https://github.com/gillberg1111/linearr.git
cd linearr
cp .env.example .env
# Edit .env — set PLEX_URL+PLEX_TOKEN, JELLYFIN_URL+JELLYFIN_USERNAME+JELLYFIN_PASSWORD, or both.
docker compose up -d
```

Open <http://localhost:5005>. That's it. With both backends configured you'll
see the `Push to: Both / Plex / Jellyfin` picker when creating playlists;
with only one, that backend is used automatically.

---

## Install on Unraid

The fastest reliable path is to add the container manually. Unraid saves
your configuration as a local template after the first run, so you can
edit/restart it from the Docker tab just like a Community App.

### Add Container manually

1. **Docker** tab → **Add Container**.
2. Fill in:

   | Field            | Value                                                            |
   | ---------------- | ---------------------------------------------------------------- |
   | **Name**         | `linearr`                                                   |
   | **Repository**   | `ghcr.io/gillberg1111/linearr:latest`                       |
   | **Network Type** | `Bridge`                                                         |
   | **WebUI**        | `http://[IP]:[PORT:5005]`                                        |

3. Click **Add another Path, Port, Variable, Label or Device** at the
   bottom and add the following one at a time:

   | Type     | Container Path / Key   | Host Path / Value                | Notes                                            |
   | -------- | ---------------------- | -------------------------------- | ------------------------------------------------ |
   | Port     | `5005` TCP             | `5005`                           |                                                  |
   | Path     | `/data`                | `/mnt/user/appdata/linearr`      | Read/Write                                       |
   | Variable | `PLEX_URL`             | `http://<unraid-ip>:32400`       | required *if Plex enabled* (blank to disable)    |
   | Variable | `PLEX_TOKEN`           | *(your token)*                   | required *if Plex enabled*                       |
   | Variable | `JELLYFIN_URL`         | `http://<unraid-ip>:8096`        | required *if Jellyfin enabled* (blank to disable)|
   | Variable | `JELLYFIN_USERNAME`    | *(your Jellyfin username)*       | required *if Jellyfin enabled*                   |
   | Variable | `JELLYFIN_PASSWORD`    | *(your Jellyfin password)*       | required *if Jellyfin enabled*                   |
   | Variable | `WATCHED_KEEP`         | `2`                              | optional                                         |
   | Variable | `PRUNE_INTERVAL_MINUTES` | `10`                           | optional                                         |
   | Variable | `TV_LIBRARIES`         | *(blank = all show libraries)*   | optional — applies to BOTH backends if set       |

   At least one backend (Plex *or* Jellyfin) must be configured. Both are
   optional; both work; both at once works.

4. **Apply** → Unraid pulls the image from `ghcr.io` and starts the
   container.
5. Container icon → **WebUI** to open the rotator at
   `http://<unraid-ip>:5005`.

### Notes for Unraid

- **Plex or Jellyfin on the same Unraid box?** Use the LAN IP of the host
  (e.g. `http://192.168.1.50:32400` for Plex, `http://192.168.1.50:8096` for
  Jellyfin), **not** `localhost` — the Linearr container can't see them via
  `localhost`.
- **Jellyfin-only install**: leave `PLEX_URL` and `PLEX_TOKEN` blank, fill in
  `JELLYFIN_URL` / `JELLYFIN_USERNAME` / `JELLYFIN_PASSWORD`. The picker is
  hidden and every playlist targets Jellyfin.
- **Appdata path**: SQLite state lives in
  `/mnt/user/appdata/linearr/rotator.db`. Back this up if you care about
  your playlist configs; episode/show state lives in each backend.
- **Networking**: Bridge mode is fine. No host network needed.
- **Updates**: container icon → **Check for Updates** (or **Force Update**).
  Unraid will repull the image and restart.
- **Community Applications**: not yet listed (submission in progress).
  Until then, the manual setup above is the path. The
  `templates/linearr.xml` in this repo is the CA template, and
  `ca_profile.xml` at the repo root describes the repository for CA.

---

## Install with Docker Compose

The repo includes a [`docker-compose.yml`](docker-compose.yml) that supports
both **build-from-source** (default) and **pull-from-registry**.

```bash
git clone https://github.com/gillberg1111/linearr.git
cd linearr
cp .env.example .env
# edit .env — set PLEX_URL and PLEX_TOKEN
docker compose up -d
```

To pull a pre-built image instead, edit `docker-compose.yml`:

```yaml
services:
  linearr:
    # build: .                                                # comment out
    image: ghcr.io/gillberg1111/linearr:latest          # uncomment
```

Logs / status:
```bash
docker compose logs -f
docker compose ps
docker compose down       # stop
docker compose pull && docker compose up -d   # update (registry image)
```

---

## Install with `docker run`

Plex-only:

```bash
docker run -d \
  --name linearr \
  --restart unless-stopped \
  -p 5005:5005 \
  -v /path/to/your/appdata:/data \
  -e PLEX_URL=http://192.168.1.100:32400 \
  -e PLEX_TOKEN=YOUR_TOKEN_HERE \
  -e WATCHED_KEEP=2 \
  -e PRUNE_INTERVAL_MINUTES=10 \
  ghcr.io/gillberg1111/linearr:latest
```

Jellyfin-only:

```bash
docker run -d \
  --name linearr \
  --restart unless-stopped \
  -p 5005:5005 \
  -v /path/to/your/appdata:/data \
  -e JELLYFIN_URL=http://192.168.1.100:8096 \
  -e JELLYFIN_USERNAME=YOUR_JELLYFIN_USERNAME \
  -e JELLYFIN_PASSWORD=YOUR_JELLYFIN_PASSWORD \
  -e WATCHED_KEEP=2 \
  -e PRUNE_INTERVAL_MINUTES=10 \
  ghcr.io/gillberg1111/linearr:latest
```

Both Plex and Jellyfin (triple-pill picker enabled):

```bash
docker run -d \
  --name linearr \
  --restart unless-stopped \
  -p 5005:5005 \
  -v /path/to/your/appdata:/data \
  -e PLEX_URL=http://192.168.1.100:32400 \
  -e PLEX_TOKEN=YOUR_TOKEN_HERE \
  -e JELLYFIN_URL=http://192.168.1.100:8096 \
  -e JELLYFIN_USERNAME=YOUR_JELLYFIN_USERNAME \
  -e JELLYFIN_PASSWORD=YOUR_JELLYFIN_PASSWORD \
  ghcr.io/gillberg1111/linearr:latest
```

---

## Install without Docker (Python)

You need Python 3.11+.

```bash
git clone https://github.com/gillberg1111/linearr.git
cd linearr
python -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env                 # then edit
python app.py
```

The app listens on `WEB_HOST:WEB_PORT` (defaults `0.0.0.0:5005`).

systemd unit (`/etc/systemd/system/linearr.service`):
```ini
[Unit]
Description=Linearr
After=network.target

[Service]
WorkingDirectory=/opt/linearr
ExecStart=/opt/linearr/.venv/bin/python app.py
Restart=on-failure
EnvironmentFile=/opt/linearr/.env

[Install]
WantedBy=multi-user.target
```

`systemctl daemon-reload && systemctl enable --now linearr`.

---

## Finding your Plex token

1. Open the Plex web app and play any item.
2. **⋮** menu on the playing item → **Get Info** → **View XML**.
3. The new tab's URL ends with `?X-Plex-Token=XXXXXXX...`. Copy that value.
4. Full instructions: <https://support.plex.tv/articles/204059436-finding-an-authentication-token-x-plex-token/>

The token grants admin access to your server — treat it like a password. The
app never exposes it in HTML; posters proxy through the server.

## Jellyfin authentication

Linearr authenticates against Jellyfin with **username + password** (NOT an
API key). Set `JELLYFIN_USERNAME` to your Jellyfin login name and
`JELLYFIN_PASSWORD` to its password.

> [!NOTE]
> **Why username/password instead of an API key?** Jellyfin's API-key
> authentication is unresolved-broken on the playlist endpoints Linearr
> needs (see Jellyfin issues
> [#15600](https://github.com/jellyfin/jellyfin/issues/15600) and
> [#12999](https://github.com/jellyfin/jellyfin/issues/12999), open as of
> 10.11.3) — `GET /Playlists/{id}` and `DELETE /Playlists/{id}/Items`
> return 400 "Guid can't be empty" when called with an API key.
> Authenticating as a user via `POST /Users/AuthenticateByName` works for
> every endpoint, which is the same path the official Jellyfin web UI uses.
>
> **How the credentials are handled:** the password is only used during the
> initial `/Users/AuthenticateByName` call and is held in memory only — never
> written to the DB. The resulting access token is also memory-only and is
> rotated automatically on any 401. A stable DeviceId persists to
> `<DB_DIR>/device_id` so the server's "one access token per (deviceId, user)"
> rule doesn't churn on every restart.
>
> A dedicated low-privilege Jellyfin user works fine — Linearr only needs
> read access to libraries and the ability to manage playlists owned by
> that user.

---

## Configuration reference

All values are environment variables. **At least one backend (Plex or
Jellyfin) must be configured.** Both backends, both, or either alone all work.

| Variable                 | Required | Default                | Notes                                                                                          |
| ------------------------ | -------- | ---------------------- | ---------------------------------------------------------------------------------------------- |
| `PLEX_URL`               | if Plex  | —                      | e.g. `http://192.168.1.100:32400`. LAN IP, not `plex.tv`. Leave blank to disable Plex.         |
| `PLEX_TOKEN`             | if Plex  | —                      | X-Plex-Token (see above). Leave blank to disable Plex.                                         |
| `JELLYFIN_URL`           | if Jellyfin | —                   | e.g. `http://192.168.1.100:8096`. LAN IP. Leave blank to disable Jellyfin.                     |
| `JELLYFIN_USERNAME`      | if Jellyfin | —                   | Jellyfin login name. Leave blank to disable Jellyfin.                                          |
| `JELLYFIN_PASSWORD`      | if Jellyfin | —                   | Held in memory only; never written to DB. See note above on why username/password.             |
| `WEB_HOST`               | no       | `0.0.0.0`              | `127.0.0.1` to restrict to localhost.                                                          |
| `WEB_PORT`               | no       | `5005`                 | HTTP port.                                                                                     |
| `DB_PATH`                | no       | `/data/rotator.db`     | SQLite file. Container `/data` is the persistent volume.                                       |
| `WATCHED_KEEP`           | no       | `2`                    | Recently-watched episodes to leave in each playlist as a fall-asleep buffer.                   |
| `PRUNE_INTERVAL_MINUTES` | no       | `10`                   | How often the prune + auto-sync sweep runs.                                                    |
| `AUTO_SYNC`              | no       | `true`                 | When true, newly-aired episodes and new seasons are spliced into managed playlists every sweep. Set `false` to lock playlists at creation. |
| `TV_LIBRARIES`           | no       | *(all show libs)*      | Comma-separated library names to source shows from. Blank = every "show" library. Applies to BOTH backends when set. |
| `FLASK_SECRET`           | no       | `dev-secret-change-me` | Random secret for Flask session cookies. `openssl rand -hex 32`.                               |
| `LINEARR_API_KEY`        | no       | *(auto-generated)*     | PIN the REST API key across container restarts. If unset, a random key is generated on first boot and stored in the DB. View it at `/settings`. |

The app searches every **movie** library on every configured backend when
looking for associated movies — that isn't currently filterable.

---

## Usage walk-through

### Create a new playlist

1. Click **+ New playlist** in the top-right and choose **By Show** or
   **By Genre**.
2. **By Show:** click the posters of the shows you want. They jump into the
   **Selected** tray pinned at the top. Filter or **Clear selection** as
   needed. Optionally type a name — if you leave it blank, Linearr
   auto-generates one ("Linearr 001", "Linearr 002", …). When ready:
   **Configure →**.
3. **Configure** each show:
   - The **Playlist name** field at the top is editable — rename before
     creating if you skipped it or want to change the auto-generated name.
   - For multi-season shows: pick a **Start from** season and an optional
     **End at** season (defaults to *Entire Series*).
   - If the show has a Season 0, toggle **Include specials** on/off.
   - If the app found any movies in your library whose titles match the
     show name, an **Include associated movies (N found)** toggle appears.
     Flip it on to reveal the matched movies with a **Select all** button
     and individual checkboxes.
   - At the top of the page: choose episode order (Rotation, Blocks,
     Weighted, Air Date, Shuffle), the **Only unwatched episodes** toggle,
     and the **Auto-update** toggle.
4. The **Preview** at the bottom updates automatically (no page reload) as
   you change settings. It shows every episode that would land in the
   playlist, paginated 10/25/50/100/All with Prev/Next buttons. Air dates
   are visible so you can sanity-check a chronological build.
5. **Create Playlist** commits — the result appears in every targeted
   Plex/Jellyfin client as a native playlist.

### Edit a playlist later

From the playlist's detail page:
- **Rotation / Air Date** pill — flip to rebuild the future portion of the
  playlist in the other order.
- **All episodes / Unwatched only** pill — same idea, but for the watched
  filter.
- **Add another show** — picker → configure → splices in.
- **Remove** below any show — wipes every one of its episodes from the
  playlist (and any of its movies you added).
- **▲ / ▼** to reorder; **Save order** rebuilds the future portion.
- **Prune watched now** triggers cleanup outside the 10-minute schedule.

---

## How adds, removes, sort changes, and prunes work

- **Add show**: finds the current playback point in the Plex playlist
  (last watched/partially-watched item). Everything before it stays.
  After it, the future portion is regenerated so all shows — including
  the new one — interleave (rotation) or chronologize (air-date) in their
  next episodes.
- **Remove show**: every episode of that show is deleted **from the
  playlist** (actual media files and library entries are never touched).
- **Reorder rotation**: same logic as add — kept portion stays, future
  portion is regenerated to honor the new order.
- **Switch sort mode**: same again — kept portion stays, future portion
  is regenerated using the new mode.
- **Switch unwatched-only**: same again — kept portion stays; the future
  portion is rebuilt under the new filter.
- **Prune sweep**: every `PRUNE_INTERVAL_MINUTES`, watched episodes older
  than the most recent `WATCHED_KEEP` are removed.
- **Auto-sync** (`AUTO_SYNC=true`, default): on the same interval, each
  managed playlist is re-checked against current backend metadata. Newly-aired
  episodes and new seasons (if within the show's configured range) splice
  into the future portion of the playlist; episodes deleted from your library
  drop out. Already-played portion is never disturbed. Each playlist also has
  its own **Auto-update: Enabled / Disabled** pill — set to Disabled, the
  scheduler skips that one playlist regardless of the global env var.

### Crossover alignment (Air Date mode)

When sorting by Air Date, two things happen automatically:

1. **Same-day adjacency.** Episodes that aired on the same date land back
   to back, regardless of which shows they're from.
2. **Multi-part ordering.** Within a same-day group, episodes whose titles
   contain `Part 1` / `Pt. 2` / `(1)` etc. sort by their part number so a
   2-part crossover plays in the right order even when Parts 1 and 2 are
   on different shows.

Throw **Law & Order**, **L&O: SVU**, and **L&O: Criminal Intent** into one
Air Date playlist and you'll get a chronological mix with their crossover
two-parters intact.

For crossovers the title heuristic misses (episodes that aired same-night as a
two-parter but don't have "Part N" in their titles), the **Crossover groups**
section on the playlist page lets you manually link specific episodes across
shows and set their play order.

### Movie placement

- **Air Date mode:** movies use their `originallyAvailableAt` date and slot
  in chronologically. *Mr. Monk's Last Case* (2023) plays after *Monk*
  S08E16 (2009).
- **Rotation mode:** movies play at the *end* of their associated show's
  chronology — after the show's last episode.

---

## Safety guarantee

This app **never** deletes media files or library items from Plex or
Jellyfin. The only destructive backend operations it performs:

| Operation                                       | What it touches                                                |
| ----------------------------------------------- | -------------------------------------------------------------- |
| Plex `Playlist.delete()`                        | The Plex playlist (metadata only).                             |
| Plex `Playlist.removeItem()`                    | One entry IN a Plex playlist. Underlying media is untouched.   |
| Jellyfin `DELETE /Items?ids={playlistId}`       | The Jellyfin playlist (metadata only). Only via `delete_playlist()` after verifying the target is a playlist. |
| Jellyfin `DELETE /Playlists/{id}/Items`         | Items IN a Jellyfin playlist. Underlying media is untouched.   |

### Plex — monkey-patch on import

`plex_client.py` installs a runtime safety guard at import time:

```python
for cls in (Episode, Show, Season, Movie):
    cls.delete = _refuse_delete   # raises RuntimeError on call
```

So `episode.delete()`, `show.delete()`, `movie.delete()`, etc. fail
immediately with a clear error instead of doing anything. `Playlist.delete()`
is intentionally left intact (playlists are pure metadata). Unit tests
verify each of the four classes' `.delete` is actually patched.

### Jellyfin — HTTP-layer deny-by-default

`jellyfin_client.py` routes every outbound `DELETE` request through
`_check_delete_safety(path)`, which raises `JellyfinSafetyError` unless the
path matches the allow-list (currently a single pattern:
`^/Playlists/[^/]+/Items$`). The intentional `delete_playlist()` is the
single audited code path that bypasses the check — and it verifies the
target really is a playlist via `GET /Playlists/{id}` first.

Unit tests assert refusal across 18 dangerous endpoint categories:
- `DELETE /Items` (mass library delete) and `DELETE /Items/{id}` (single)
- `DELETE /Items/{id}/Images/...` (asset deletion)
- `DELETE /Library/VirtualFolders` / `.../Paths` (library removal)
- `DELETE /Collections/{id}/Items`, `DELETE /Users/{id}`, `DELETE /Devices`
- `DELETE /Videos/.../AlternateSources`, `.../Subtitles/{n}`, `.../Lyrics`
- `DELETE /Auth/Keys/{key}`, `DELETE /Plugins/{id}`, `DELETE /LiveTv/Recordings/{id}`
- and more — see [`tests.py`](tests.py) `test_jellyfin_safety_blocks_library_item_deletion`.

---

## Running tests

The unit-test suite is stdlib-only — no Plex, Jellyfin, or network required.

```bash
python tests.py
# expected: 269 passed, 0 failed, 269 total
```

Covers:
- **Rotation logic** (36 tests): round-robin interleaving, weighted rotation,
  block scheduling, intelligent shuffle, splice-from-current-position,
  watched pruning with last-N retention, Part N detection, air-date sequence
  with crossover Part 1/2 alignment, show-order tie-breaks, rebuild-tail in
  all five modes, movie identity preservation, crossover_map sort key.
- **Safety guards** (22 tests): every Plex item class confirmed monkey-patched,
  every dangerous Jellyfin DELETE endpoint refused by the HTTP-layer guard,
  the one allow-listed Jellyfin DELETE pattern accepted, lookalike paths
  rejected.
- **Cross-backend matching** (20 tests): title normalization, country-code and
  year suffix stripping (`(US)`, `(UK)`, `(2018)` etc.), case insensitivity,
  punctuation handling, year disambiguation, year-known-on-one-side
  permissiveness, None/empty handling.
- **Service-layer dispatch** (8 tests): `ShowConfig` back-compat,
  `id_for(backend)` routing, `movie_ids_for(backend)`, `_backends_for`
  expansion, `_find_match` with year tiebreak.
- **Per-episode exclusions** (7 tests): CSV parse/serialize round-trips,
  malformed-input tolerance, default-empty, sorted output.
- **Advanced sequencing** (19 tests): weighted depletion-fallback, block
  patterns, shuffle determinism + chronological preservation + no-consecutive
  avoidance, compose dispatch, rebuild_tail in weighted/shuffle modes.
- **Genre playlists** (14 tests): genre CSV parsing, `is_excluded` field,
  `PlaylistView` genre defaults, `VALID_PLAYLIST_TYPES`.
- **Genre cache** (6 tests): empty-cache → None, round-trip store/retrieve,
  per-backend isolation, expiry after 7 days, overwrite, empty-list handling.
- **Crossover grouping** (5 tests): crossover_map sort key behavior, compose
  and rebuild_tail passthrough.
- **Smart rules** (20 tests): `_apply_rules` with year min/max, status,
  season count min/max, rating min, content rating, combined multi-rule
  filtering, None-field permissiveness.

---

## Updating

**Docker Compose (built locally):**
```bash
git pull
docker compose build --pull
docker compose up -d
```

**Docker Compose (registry image):**
```bash
docker compose pull
docker compose up -d
```

**Unraid:** click the container icon → **Check for Updates** (or **Force Update**).

**Python:**
```bash
git pull
.venv/bin/pip install -r requirements.txt
systemctl restart linearr   # if using the systemd unit
```

SQLite migrations run automatically on startup (lightweight `ALTER TABLE`
calls for new columns). Existing Plex playlists are never modified during
updates.

---

## Troubleshooting

**"Couldn't reach Plex" on the New playlist page**
- Confirm `PLEX_URL` is reachable from inside the container:
  ```bash
  docker exec -it linearr python -c "import urllib.request; print(urllib.request.urlopen('YOUR_PLEX_URL/identity').status)"
  ```
- If Plex is on the same host as the rotator, **don't** use
  `localhost`/`127.0.0.1` — use the LAN IP.

**Token error / 401**
- Tokens rotate when you sign out everywhere. Refresh via Plex web → Get Info.

**Playlist looks out of order after a manual edit in Plex**
- The rotator owns the future portion of the playlist. Manual reorders
  inside Plex get overwritten on the next add/remove/reorder/sort change.
  Use the rotator's controls instead.

**Associated movies don't appear for a show**
- The matcher uses word-boundary title match. The movie title must
  literally contain the show's name as a word. *"Mr. Monk's Last Case: A
  Monk Movie"* matches *Monk*; *"Funky Monk"* would match too but
  *"Psychic Detective"* wouldn't match *Psych* (no boundary).
- If you have the movie but it isn't matching, check the title metadata in
  Plex — sometimes scrapers pull a localized title that doesn't include
  the show name.

**Prune isn't removing anything**
- Episodes have to be marked watched in Plex (~90% playback). Scrub-and-skip
  may not register.

**"Address already in use"**
- Something else is on `WEB_PORT`. Change `WEB_PORT` in `.env` and the
  published port.

**Logs**
```bash
docker compose logs -f                 # compose
docker logs -f linearr            # plain docker
journalctl -u linearr -f          # systemd
```

---

## Architecture

```
app.py                       — Flask routes (/, /new, /new/configure,
                                /playlist/<id>, /thumb, /api/preview, …)
                                + cross-backend show aggregation for the picker
service.py                   — High-level ops: create / add / remove /
                                reorder / set-sort / set-unwatched / sync /
                                prune. Dispatches to each enabled backend
                                via _clients_for_playlist().
media_client.py              — Abstract MediaClient base + shared dataclasses.
                                Single get_client(backend) factory. Pure
                                title-match helper for cross-backend bridging.
plex_client.py               — PlexClient(MediaClient): wraps python-plexapi.
                                Module-level monkey-patch refuses
                                Episode/Show/Season/Movie.delete().
                                Module-level shims for backward compat.
jellyfin_client.py           — JellyfinClient(MediaClient): raw requests
                                against the Jellyfin REST API. Authenticates
                                via /Users/AuthenticateByName. HTTP-layer
                                deny-by-default DELETE safety guard.
                                Atomic playlist replace via UpdatePlaylist.
rotation.py                  — Pure interleave / air-date-sort / splice /
                                prune logic. Backend-agnostic.
                                Unit-tested.
db.py                        — SQLite schema, migrations, helpers.
                                Tracks per-row backend IDs (plex_show_item_id,
                                jellyfin_show_item_id) for "Both"-mode
                                playlists.
scheduler.py                 — APScheduler background prune + sync sweeps
templates/
  base.html                  — Layout + top bar (+ New playlist button)
  index.html                 — Playlist landing page (+ backend badges)
  new.html                   — Show picker (with tray + clear + per-show
                                "Plex only" / "Jellyfin only" overlays)
  new_genre.html             — Genre playlist creator (name + genres +
                                preview matches)
  playlist.html              — Per-playlist detail page (+ backend badge,
                                missing-side warning banner, crossover
                                groups section in air_date mode)
  configure.html             — Per-show season range, specials, movies,
                                sort/filter pills, AJAX preview, and the
                                triple-pill "Push to" backend picker
                                (shown only when ≥2 backends configured)
  _preview_partial.html      — Just the preview list (rendered server-side
                                on initial load and via /api/preview AJAX)
  linearr.xml                — Unraid Community Applications template (XML),
                                lives here per CA's required folder structure
static/
  picker.js                  — Tray-based show picker (reusable)
  style.css                  — All styles (incl. backend-badge + warning-banner)
images/                      — Logo, banner, favicons, Unraid icon (SVG + PNG)
ca_profile.xml               — Repository-wide metadata for Unraid CA
tests.py                     — Self-contained unit tests (rotation, safety
                                guards, title matching, dispatch — 187 total)
```

Each backend's playlist is the source of truth for *its own* episode order.
SQLite only stores configuration (which shows in which playlist, their
seasons, specials choice, included movies, position, sort + filter modes,
plus which backend(s) each playlist targets and per-row backend IDs for
"Both"-mode playlists).

---

## License & disclaimer

Linearr is open-source software released under the **[MIT License](LICENSE)**.

> **No warranty.** This software is provided "as is", without warranty of any
> kind, express or implied. The author is not responsible for any data loss,
> playlist corruption, media library damage, or any other issue that may result
> from its use. Please **review the source code** and test thoroughly in your
> own environment before relying on it. By using Linearr you accept full
> responsibility for any outcomes.

---

## Contributing

Issues and PRs welcome. The rotation/sort logic in `rotation.py` is pure
and unit-tested — keep it that way; side effects belong in `service.py` or
`plex_client.py`. Run `python tests.py` before any PR.

---

## Support the project

Linearr is free, open-source, and has no business model behind it. If it
saves you time and you'd like to chip in:

[☕ Buy me a coffee](https://buymeacoffee.com/gillberg1111)

The button is also embedded at the bottom of the app's landing page.

---

## Acknowledgments

Linearr was built collaboratively with [Claude Code](https://claude.com/claude-code)
(Anthropic's AI coding assistant) across many pair-programming sessions.
Architecture, naming, testing against a real Plex / Unraid setup,
deployment, and ongoing maintenance are mine.

Franchise watch-order playlists are powered by community-maintained lists on
[Trakt.tv](https://trakt.tv). Linearr uses the Trakt API under a registered
application key. Trakt® is a trademark of Trakt, LLC.

---

> Linearr follows the `*arr` naming convention popular in the Plex / Sonarr /
> Radarr ecosystem, but it is not affiliated with the Servarr project, Plex
> Inc., or the Jellyfin project. "Plex" is a trademark of Plex GmbH;
> Jellyfin is a community-developed free software project (GPL-2.0). Linearr
> is an independent third-party client.
