# Pinnarr — Design Spec

**v0.4 — 19 August 2026**
*Self-hosted release calendar for the shows you actually care about.*

*Changes since v0.1: added season outlook (§10), faceted library filtering and bulk pin (§11), TMDB as a data source, resolved open question 4.*

*Changes since v0.2: configuration moved from environment variables to a database-backed admin panel (§15), with connection tests that resolve open questions 2 and 3 at runtime.*

*Changes since v0.3: multi-user (§19). Pins, calendars and notifications are per account; configuration stays shared and admin-only.*

---

## 1. What it is

Pinnarr sits next to Sonarr, Radarr, Tautulli and Plex. You **pin** the handful of TV shows you actively follow, and Pinnarr gives you a calendar of just those shows' upcoming episodes — plus a push notification the moment an episode lands in Plex and is genuinely watchable.

It is a **curation and presentation layer**. It does not download anything, does not manage quality profiles, and does not replace any part of the existing stack.

## 2. Why not just use Sonarr's calendar

Sonarr already has a calendar and an iCal feed. Every existing tool in this space (`calendarr`, and its two namesakes) is a nicer skin on that same feed. They all share one flaw:

**They show everything.** Every series you ever added, including the one you abandoned in 2019. Forty-seven items a week, of which four matter.

Pinnarr's entire reason to exist is the subset. Two consequences fall out of that:

- Notifications become useful rather than noise, because they only fire for shows you pinned.
- We can afford to show richer per-episode and per-series state — has it aired, has it been grabbed, is it in Plex, is a new season even coming — because there are fifteen shows on screen, not four hundred.

## 3. Scope

**In scope for v1**

- Pin/unpin any TV series in the Plex library, manually, from a web UI
- Faceted filtering of the library so pinning is fast at any library size (§11)
- Month calendar + 14-day agenda of upcoming episodes for pinned series
- Per-episode availability state — upcoming / aired / awaiting / in Plex (§9)
- Per-series **season outlook** — is a new season dated, announced, filming, or is the show quietly dead (§10)
- Push notification via ntfy when a pinned episode arrives in Plex
- Optional Monday-morning digest of the week's pinned episodes
- Single container, deployed to a dedicated Proxmox LXC, image from GHCR

**Explicitly not v1**

- Movies. Theatrical dates are noise; the useful date is the GB *digital* release, which Radarr tracks as `digitalRelease`. Deferred to v1.5 as an off-by-default second tab.
- Auto-pinning from watch history. Discussed and rejected in favour of manual control.
- ~~Multi-user~~ — **added in v0.4**, see §19.
- iCal feed. Cheap to add later (~30 lines) if you decide you'd rather live in your calendar app.
- Sonarr tags as a filter facet. Deferred pending whether you actually tag series in Sonarr.

## 4. Architecture

```
                    ┌──────────────────────────────────────┐
   Plex ────────────┤ library walk + GUIDs + genres (03:00) │
   Sonarr ──────────┤ series, calendar, has_file (2-hourly) │
   Tautulli ────────┤ watch history, recently added (03:20) │──► SQLite
   TMDB ────────────┤ status, in_production (03:30, pinned) │      │
                    └──────────────────────────────────────┘      │
                                                                  ▼
   Sonarr webhook ──────► POST /hooks/sonarr ──┐             FastAPI + HTMX
   (On Import / Upgrade)                       │                   │
                                               ▼             ┌─────┴─────┐
                                          ntfy push       Calendar   Library
                                                           view      browser
```

One process. APScheduler for the jobs, FastAPI for the HTTP surface, SQLite on a bind mount.

## 5. Data sources — who owns what

| Source | Owns | Key calls |
|---|---|---|
| **Sonarr** | Air dates, episode titles, grab state, `nextAiring`, seasons array | `GET /api/v3/calendar?start&end&includeSeries=true`, `GET /api/v3/series`, header `X-Api-Key` |
| **Plex** | What you own, external IDs, genres, poster art | `/library/sections`, `/library/sections/{id}/all?type=2`, `/library/metadata/{key}?includeGuids=1`, header `X-Plex-Token` |
| **Tautulli** | Watch history, arrival confirmation | `/api/v2?apikey=…&cmd=get_history`, `cmd=get_recently_added`, `cmd=get_libraries` |
| **TMDB** | Production status — cancelled vs ended vs returning, `in_production` | `GET /3/tv/{id}`, `GET /3/find/{tvdb_id}?external_source=tvdb_id` |
| **Radarr** | *(v1.5)* digital/physical release dates | `GET /api/v3/calendar`, fields `digitalRelease`, `physicalRelease` |

**Note on Tautulli.** You went manual on pinning, so we don't strictly need watch history. It's still worth syncing, because sorting the library browser by *recently watched* makes the manual pinning job dramatically less tedious — the shows you want to pin float to the top instead of you scrolling an alphabetical wall. Cheap to collect, big ergonomic payoff.

**Note on TMDB.** Only queried for pinned series plus anything visible in the current library filter, not the whole library — keeps us well inside rate limits and makes the nightly job trivial.

## 6. Identity and matching

**TVDB ID is the primary join key.** Everything hangs off it:

```
Plex show → includeGuids → tvdb://83462 → Sonarr series.tvdbId → episodes
                                        → TMDB /find?external_source=tvdb_id → status
```

Fallback chain when TVDB is absent: `tmdb://` → `imdb://` → normalised title + year (last resort, logged as a soft match for review).

**Known edge cases:**

- **Legacy Plex agents.** A library still on `com.plexapp.agents.thetvdb://83462?lang=en` needs different parsing to the modern `plex://show/5d9c081…` + `includeGuids` route. The parser handles both; we should check one series on your server to see which you're on.
- **Anime.** Frequently lands on a TMDB-only GUID in Plex, and Sonarr may be using absolute episode numbering. Anime series will match by TMDB and may show odd episode numbers. Acceptable for v1; flagged in the UI as a soft match.
- **Multi-episode files.** One file covering S01E01-E02 marks both episodes available. Handled.

## 7. Data model

```sql
CREATE TABLE series (
    id                   INTEGER PRIMARY KEY,
    tvdb_id              INTEGER UNIQUE,
    tmdb_id              INTEGER,
    imdb_id              TEXT,
    plex_rating_key      TEXT,
    plex_section_id      INTEGER,
    sonarr_id            INTEGER,
    title                TEXT NOT NULL,
    sort_title           TEXT,
    year                 INTEGER,
    network              TEXT,
    poster_url           TEXT,
    overview             TEXT,

    -- source status
    sonarr_status        TEXT,          -- continuing | ended | upcoming | unknown
    tmdb_status          TEXT,          -- Returning Series | Ended | Canceled | In Production | Planned | Pilot
    in_production        INTEGER,

    -- season tracking, feeds the outlook ladder
    next_airing          TEXT,          -- ISO UTC, from Sonarr
    previous_airing      TEXT,
    latest_season        INTEGER,       -- highest season known to metadata
    latest_aired_season  INTEGER,       -- highest season with an aired episode
    outlook              TEXT,          -- derived, see §10
    outlook_computed_at  TEXT,

    in_plex              INTEGER NOT NULL DEFAULT 0,
    in_sonarr            INTEGER NOT NULL DEFAULT 0,
    match_confidence     TEXT DEFAULT 'exact',   -- exact | soft
    pinned               INTEGER NOT NULL DEFAULT 0,
    pinned_at            TEXT,
    notify               INTEGER NOT NULL DEFAULT 1,
    last_watched_at      TEXT,
    created_at           TEXT NOT NULL,
    updated_at           TEXT NOT NULL
);

CREATE TABLE episodes (
    id                 INTEGER PRIMARY KEY,
    series_id          INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    sonarr_episode_id  INTEGER UNIQUE,
    season             INTEGER NOT NULL,
    episode            INTEGER NOT NULL,
    title              TEXT,
    air_date_utc       TEXT,            -- ISO 8601 UTC, authoritative
    runtime            INTEGER,
    monitored          INTEGER NOT NULL DEFAULT 1,
    has_file           INTEGER NOT NULL DEFAULT 0,
    in_plex            INTEGER NOT NULL DEFAULT 0,
    arrived_at         TEXT,
    notified_at        TEXT,
    updated_at         TEXT NOT NULL,
    UNIQUE (series_id, season, episode)
);

CREATE TABLE genres (
    id    INTEGER PRIMARY KEY,
    name  TEXT NOT NULL UNIQUE
);

CREATE TABLE series_genres (
    series_id  INTEGER NOT NULL REFERENCES series(id) ON DELETE CASCADE,
    genre_id   INTEGER NOT NULL REFERENCES genres(id)  ON DELETE CASCADE,
    PRIMARY KEY (series_id, genre_id)
);

CREATE TABLE sync_log (
    id          INTEGER PRIMARY KEY,
    job         TEXT NOT NULL,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT,                  -- ok | error
    detail      TEXT
);

CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);

CREATE INDEX idx_episodes_air     ON episodes (air_date_utc);
CREATE INDEX idx_episodes_series  ON episodes (series_id);
CREATE INDEX idx_series_pinned    ON series (pinned);
CREATE INDEX idx_series_outlook   ON series (outlook);
CREATE INDEX idx_series_section   ON series (plex_section_id);
```

A few thousand rows total. SQLite in WAL mode is comfortably enough — no Postgres needed.

## 8. Sync jobs

| Job | Schedule | What it does |
|---|---|---|
| `plex_library` | 03:00 nightly | Walk TV sections, upsert series, resolve GUIDs, cache posters, populate genres |
| `sonarr_series` | 03:10 nightly | Map `sonarr_id`, status, `nextAiring`, seasons array → `latest_season` |
| `sonarr_calendar` | every 2h | Pull −7d…+60d window for all series; filter to pinned at render time |
| `tautulli_history` | 03:20 nightly | Update `last_watched_at` per series |
| `tmdb_status` | 03:30 nightly | Production status for pinned series; recompute `outlook` for all |
| `plex_availability` | hourly | For pinned series only: confirm recent episodes are present in Plex |
| `reconcile` | 04:00 nightly | Catch anything the webhook missed; fire late notifications |

Pulling the calendar for *all* series rather than per-pinned-series is deliberate: one request beats fifteen, and it means unpinning and repinning is instant with no refetch.

## 9. Episode state

Derived at render time, not stored — so it's always correct without a migration:

| State | Condition | UI |
|---|---|---|
| `upcoming` | `air_date_utc > now` | Neutral, shows countdown |
| `airing_today` | Airs within today (Europe/London) | Highlighted |
| `awaiting` | Aired, `!has_file`, < 48h ago | Amber — "expected soon" |
| `missing` | Aired, `!has_file`, > 48h ago | Red — something's wrong |
| `available` | `has_file` or `in_plex` | Green — watchable |

The `missing` state is the one no existing tool surfaces well. "Aired four days ago and still isn't here" is exactly the thing you want to know, and Sonarr buries it under Wanted.

## 10. Season outlook

**The problem.** Sonarr's `status: continuing` means only "TVDB hasn't marked this ended." Nobody goes back to update a quietly cancelled show, so it stays `continuing` indefinitely. A badge built on that field alone lies in precisely the case you care about.

So `outlook` is **derived from several signals**, evaluated top-down — first match wins. Recomputed nightly by `tmdb_status`.

| # | Outlook | Condition | Badge |
|---|---|---|---|
| 1 | `dated` | `next_airing` is set *and in the future* | `▸ 22 Aug` |
| 2 | `announced` | `latest_season > latest_aired_season`, but that season's episodes have no `air_date_utc` | `S3 announced` |
| 3 | `in_production` | TMDB `in_production = true` or `tmdb_status = In Production`, nothing scheduled | `filming` |
| 4 | `cancelled` | `tmdb_status = Canceled` | `cancelled` |
| 5 | `ended` | `tmdb_status = Ended` or `sonarr_status = ended` | `ended` |
| 6 | `between_seasons` | Continuing, `previous_airing` within 9 months, nothing announced | `hiatus` |
| 7 | `dormant` | Continuing per TVDB, but `previous_airing` > 18 months ago and not in production | `⚠ probably over` |
| 8 | `unknown` | No usable signal | `—` |

**Ordering is load-bearing, and v0.2 of this spec had it wrong.** `cancelled`
and `ended` must be evaluated *before* the hiatus/dormant rungs, or a
genuinely finished show gets described as merely "on hiatus". Equally,
`dated` must come first: a show TMDB calls `Ended` whose finale hasn't aired
yet is `dated`, not `ended`. Both cases are pinned by tests
(`test_ended_outranks_hiatus`, `test_dated_beats_a_stale_ended_status`).

Note also rung 1 requires `next_airing` to be *in the future* — stale Sonarr
data with a past `nextAiring` must fall through rather than read as dated.

**Why `dormant` earns its keep.** It's pin-list hygiene. Without it, your pins slowly fill with zombie shows you're nominally still waiting on and nothing ever tells you. With it, one glance at the library sorted by outlook clears them out. This is also the distinction TVDB and Sonarr simply cannot make — hence TMDB.

**Honest limit.** These reflect *metadata state*, not insider knowledge. A show renewed by the network last Tuesday won't read as `in_production` until someone updates TMDB. The badge can be stale in either direction by a few weeks. It is not prophetic, and the UI shouldn't imply it is.

**Calendar treatment.** Month cells name the shows airing that day rather than
marking them with a dot — a dot tells you something is happening and refuses to
say what, which is only tolerable on a calendar you already know by heart. Each
name links to the series page.

The old undifferentiated "no date yet" pile splits into three sections: **Announced**, **In production**, and **Dormant** — the last collapsed by default, since it's a prompt to prune rather than something to look forward to.

## 11. Library browser: filtering and pinning

A flat alphabetical poster grid is fine at 40 series and miserable at 400.
The reference library turned out to hold **2060 series**, so the grid also
**pages** — 60 per page. Facets narrow; pagination is what makes the
un-narrowed view survivable at all, and no amount of faceting removes the
need for it because the default view has no facets applied. Facets, all AND'd together, multi-select values within a facet OR'd:

| Facet | Source | Notes |
|---|---|---|
| **Search** | `title`, `sort_title` | Debounced, substring |
| **Library section** | `plex_section_id` | TV / Anime / Documentaries as separate Plex libraries |
| **Status** | `sonarr_status` | Continuing / ended / upcoming |
| **Outlook** | `outlook` (§10) | Includes the high-value *has something actually coming* case |
| **Genre** | `series_genres` join | Multi-select, OR'd within the facet |
| **Network** | `network` | "Everything on Sky Atlantic" |
| **Pin state** | `pinned` | All / pinned / unpinned |

**Facet counts** — `Drama (47)`, `Continuing (61)` — one `GROUP BY` per facet, and they stop you clicking into empty results.

Each facet's counts are computed with **that facet excluded** from the filter.
A count then answers "what would I get if I ticked this as well", which is the
question you are actually asking. Counting with the facet applied to itself
makes every unticked value read zero, which looks like a bug and is useless.

**Sort:** recently watched (default), title, next airing, outlook.

**Filter state lives in the URL** so views are bookmarkable: `?section=2&status=continuing&genre=drama,scifi&outlook=dated,announced&sort=recent`. HTMX swaps only the grid.

**Bulk pin.** Once filtered to something like *Anime + continuing + has something coming*, a **Pin all shown** button turns the first-run setup from ten minutes of clicking into thirty seconds. Implementation detail that matters: the request sends the **filter querystring, not a list of IDs**, and the server re-runs the filter — so there's no stale-ID class of bug. Confirm dialog states the count. Reversible via *undo last bulk pin*, which is cheap because `pinned_at` gives us the set for free.

## 12. HTTP surface

```
GET   /                          Calendar view (month grid + 14-day agenda)
GET   /series/{id}               Per-series page: metadata, episodes, pin toggle
GET   /settings/jobs             Sync jobs, last result, run now (admin)
GET   /settings/webhook          Connection URL and recent deliveries (admin)
GET   /settings                  Admin panel — all configuration
POST  /settings                  Save the panel; reschedules if timing changed
POST  /api/settings/test/{svc}   Connection test against saved settings
GET   /library                   Poster grid; facets per §11 as query params
POST  /api/series/{id}/pin       Toggle, returns {pinned, pinned_total} as JSON
POST  /api/series/{id}/unpin
POST  /api/series/{id}/notify    Per-series notification opt-out
POST  /api/series/bulk-pin       Body carries the filter querystring
POST  /api/series/bulk-undo      Unpin the most recent bulk-pin batch
GET   /poster/{series_id}        Cached poster proxy — keeps the Plex token server-side
GET   /api/calendar?start&end    JSON, for anything you want to build later
POST  /hooks/sonarr?secret=…     Webhook receiver
POST  /api/sync/{job}            Manual job trigger (for debugging)
GET   /healthz                   Liveness, plus last successful sync per job
```

## 13. UI

Two views. Server-rendered, HTMX for the toggles, no build step.

**Calendar** — the default landing page:

```
┌────────────────────────────────────────────────────────────┐
│  PINNARR          [ Calendar ]  Library          ⚙  ↻      │
├────────────────────────────────────────────────────────────┤
│  NEXT 14 DAYS                                              │
│                                                            │
│  Tue 19 Aug   ▸ Severance            S02E07   ● in Plex    │
│  Wed 20 Aug   ▸ The Bear             S04E03   ◐ expected   │
│  Fri 22 Aug   ▸ Andor                S02E01   ○ upcoming   │
│               ▸ Slow Horses          S05E04   ○ upcoming   │
│                                                            │
│  ⚠ AIRED, NOT ARRIVED                                      │
│  Sat 16 Aug   ▸ Foundation           S03E08   ✕ 3 days     │
│                                                            │
│  ── August 2026 ─────────────────────── ‹ › ──────────────  │
│   M   T   W   T   F   S   S                                │
│                   1   2   3                                │
│   4   5   6   7   8   9  10                                │
│  11  12  13  14  15  16● 17                                │
│  18  19● 20● 21  22●● 23  24                               │
│                                                            │
│  ANNOUNCED, NO DATES YET                                   │
│  Silo · S3        The Diplomat · S3                        │
│                                                            │
│  IN PRODUCTION                                             │
│  Shrinking · filming                                       │
│                                                            │
│  ▸ DORMANT (2)                              [collapsed]    │
└────────────────────────────────────────────────────────────┘
```

**Library** — where pinning happens:

```
┌──────────────────┬─────────────────────────────────────────┐
│ PINNARR   Calendar [ Library ]                     ⚙  ↻    │
├──────────────────┼─────────────────────────────────────────┤
│ 🔍 ____________  │  312 shows · 14 pinned                  │
│                  │  ⇅ Recently watched      [Pin all shown]│
│ LIBRARY          ├─────────────────────────────────────────┤
│ ☑ TV        (241)│  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐   │
│ ☐ Anime      (58)│  │poster│ │poster│ │poster│ │poster│   │
│ ☐ Docs       (13)│  │    📌│ │      │ │    📌│ │      │   │
│                  │  └──────┘ └──────┘ └──────┘ └──────┘   │
│ OUTLOOK          │  Severance The Bear  Andor    Silo      │
│ ☑ Dated      (9) │  ▸ 22 Aug  ◐ hiatus  ▸ 22 Aug S3 ann.  │
│ ☑ Announced  (6) │                                         │
│ ☐ Filming    (4) │  ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐   │
│ ☐ Hiatus    (22) │  │poster│ │poster│ │poster│ │poster│   │
│ ☐ Dormant   (17) │  │      │ │      │ │      │ │      │   │
│ ☐ Ended    (193) │  └──────┘ └──────┘ └──────┘ └──────┘   │
│                  │  Dark      Shrinking Foundation Slow H. │
│ GENRE            │  ended     filming   ▸ 24 Aug  ◐ hiatus │
│ ☐ Drama     (98) │                                         │
│ ☐ Sci-Fi    (41) │                                         │
│ ☐ Comedy    (37) │                                         │
│                  │                                         │
│ NETWORK      ▾   │                                         │
│ [ Clear all ]    │                                         │
└──────────────────┴─────────────────────────────────────────┘
```

On mobile the facet rail becomes a slide-over sheet.

## 14. Notifications

**ntfy**, via plain HTTP POST — no SDK needed:

```bash
curl -H "Title: The Bear S04E03 is in Plex" \
     -H "Tags: tv,white_check_mark" \
     -H "Priority: default" \
     -H "Click: http://pinnarr.lan:8737/" \
     -d "Forks — just arrived and ready to watch." \
     https://ntfy.sh/<your-topic>
```

**Trigger: arrival, not airing.** Sonarr fires `On Import` → we hit `/hooks/sonarr` → look up the TVDB ID → if pinned and `notify = 1`, push. Airing notifications are a tease; arrival notifications are actionable.

Webhook rather than polling means it's instant and costs nothing. The nightly `reconcile` job is the safety net for a webhook that didn't land.

**Weekly digest** (optional, borrowed from `jordanlambrecht/calendarr`): Monday 08:00, one push listing the week's pinned episodes grouped by day. Different job to the per-episode ping, both useful.

**Dedupe:** `notified_at` on the episode row. An upgrade from 720p to 1080p fires `On Upgrade`, which must not re-notify.

## 15. Configuration

**Configuration lives in the database and is edited in the admin panel at
`/settings`.** v0.1 of this spec put everything in environment variables; that
is no longer true, and the reasoning is worth recording.

An `.env` file means SSHing into the LXC to change a token, and it means the
app can never help you fill it in. The panel can: it tests each connection on
demand, and the Plex test enumerates your libraries so section IDs are ticked
rather than guessed. This is also how the rest of the stack behaves — Sonarr,
Radarr and Tautulli all keep configuration in their own store behind a
settings UI — so it is the least surprising option on a box that already runs
them.

**Two things stay in the environment, because they must:**

```ini
DATABASE_PATH=/data/pinnarr.db
LOG_LEVEL=INFO
```

`DATABASE_PATH` cannot live in the database — the app has to find and open the
file before it can read a single setting out of it. `LOG_LEVEL` is read while
logging is being configured, which happens before the first query. Everything
else is panel-owned, and the environment is **not** consulted for it: setting
`PLEX_URL` in `.env` has no effect at all.

This split is enforced in code rather than by convention. `app.config` exposes
`get_bootstrap()` (env, used only by `app.db`) and `get_settings()` (database,
used by everything else), so there is no path by which a panel-owned field can
be read from the environment by accident.

**Cache invalidation.** `get_settings()` is memoised, since it is called on
every job run and every request. `save_settings()` clears it. Anything holding
a `Settings` instance across a save is holding a stale one — the clients avoid
this by reading settings in `__init__` and being constructed per job run.

**Rescheduling.** `tz`, `digest_enabled` and `digest_cron` are baked into the
cron triggers when the scheduler is built, so saving any of them rebuilds the
scheduler. Without that the new schedule sits in the database doing nothing.

**Secrets.** Tokens are never rendered into the settings page — the form only
learns whether each one is set, and an empty password box means "keep what is
stored" rather than "clear it". The corresponding cost is that `PLEX_TOKEN` now
lives in `pinnarr.db` instead of a `chmod 600` file. The database is on a bind
mount and should carry the file permissions the `.env` used to.

**Still open:** the panel has no authentication, matching the rest of the app.
That is defensible on a trusted LAN and indefensible the moment Pinnarr is
exposed to the internet. Anyone doing the latter needs to put a reverse proxy
with auth in front of it. See §17.

## 16. Deployment

**Proxmox LXC**

- Debian 13 template, **unprivileged**
- Options: `nesting=1`, `keyctl=1` — Docker will not start without both. Making the container privileged is an outdated workaround; it isn't needed.
- 1 vCPU / 1 GB RAM / 8 GB disk
- Static IP or DHCP reservation

**Networking is bidirectional** — the easily-missed bit. Pinnarr calls out to Plex/Sonarr/Tautulli/TMDB, *and* Sonarr calls back in to the webhook. Firewall/VLAN rules need to allow `Sonarr → pinnarr:8737`, not just the reverse.

**compose**

```yaml
services:
  pinnarr:
    image: ghcr.io/<you>/pinnarr:latest
    container_name: pinnarr
    restart: unless-stopped
    ports: ["8737:8737"]
    env_file: .env
    volumes:
      - ./data:/data
```

**CI** — GitHub Actions builds on push to `main`, publishes `ghcr.io/<you>/pinnarr:latest` plus a semver tag per release. The LXC only ever pulls a finished image, so it needs no build toolchain. Rollback is `image: …:v0.3.1` and `docker compose up -d`.

## 17. Open questions and risks

1. **Sonarr webhook payload isn't documented** in the Servarr wiki — *handled, not solved.* The receiver is written to be wrong safely: it acknowledges everything once the secret checks out, records every delivery with its raw body, and shows them at `/settings/webhook`. Press **Test** in Sonarr and the real shape is there to read. It must never return 5xx for a parse problem — Sonarr disables a connection that keeps failing, so reporting one bad delivery that way would take the feature down permanently.
2. **Plex TV section IDs** — *resolved by tooling.* The Plex connection test in the admin panel lists every library with its ID, so these are ticked rather than discovered.
3. **Which Plex agent** your TV library uses — *resolved by tooling.* The same connection test reports each library's agent and flags legacy ones explicitly.
4. **TMDB status enum** — the values in §10 (`Returning Series`, `Ended`, `Canceled`, `In Production`, `Planned`, `Pilot`) are from memory; TMDB's own docs don't publish the list cleanly. Confirm against the live API during build and treat unknown values as `unknown` rather than crashing.
5. **Outlook thresholds** (9 months for hiatus, 18 months for dormant) are a first guess. They're in `settings` so they're tunable without a redeploy once you see how they behave against your actual library.
6. **Future seasons often have no dates.** TVDB won't have S4 dates until the network announces. Mitigated by the `announced` outlook, not solvable.
7. **The admin panel is unauthenticated**, like the rest of the app. Fine on a trusted LAN; put a reverse proxy with auth in front of it before exposing Pinnarr to the internet, since the panel now reads and writes your tokens.
8. **Timezones.** Always render from `air_date_utc`. Sonarr's `airDate` field is network-local and will put US shows on the wrong UK day.

*Resolved since v0.1: shows in Plex but not in Sonarr — TMDB is now a first-class source, so these get status and outlook. They still won't get per-episode air dates, since TMDB's episode data is thinner than TVDB's; if that turns out to matter, TVmaze's `/shows/{id}/episodes` is the top-up.*

## 19. Multi-user

**Shared configuration, separate lists.** One Plex, one Sonarr, one TMDB key —
those describe the household's media stack, not a person. Pins, calendars and
notifications are personal. Two roles: `admin` may reach `/settings` and manage
accounts; `user` may not.

**`pins` is the source of truth, `series.pinned` is derived.** The flag stays on
the series row, but its meaning narrows to "pinned by at least one user", and it
is refreshed on every pin change. The sync jobs ask exactly that question — the
calendar fetch and the availability check should cover anything anyone follows —
so none of them needed changing. Only the user-facing queries join `pins`.

**Notifications fan out.** Each user sets their own ntfy topic; the server URL
and token stay admin-owned. An arrival pushes once per user who pinned the show
and has a topic. Dedupe moved from `episodes.notified_at` to a
`(user_id, episode_id)` table, because one person's push must not suppress
another's for the same episode.

**Auth.** Password login over a signed session cookie, scrypt for storage, no
self-registration. The first visit to an instance with no accounts offers a
one-time setup form that creates an admin — and adopts any pins made before
accounts existed, so upgrading doesn't silently empty the list.

**Still not solved:** there is no HTTPS and no rate limiting on the login form.
On a LAN that is a considered trade; exposed to the internet it is not. Put a
reverse proxy in front.

## 18. Beyond v1

- Radarr digital-release track, GB region, as a second tab
- iCal feed of pinned episodes only
- Sonarr tags as a filter facet
- Season-premiere-only mode per series (pin the show, only notify on premieres)
- Bidirectional sync with a Plex label, so pins are visible in Plex itself
- Per-episode air dates for non-Sonarr shows via TVmaze
