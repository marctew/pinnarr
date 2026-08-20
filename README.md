# Pinnarr

**Pin the shows you actually care about, and get a calendar of just those.**

Sonarr already has a calendar. The problem is that it shows *everything* — every
series you ever added, including the one you abandoned in 2019. Forty-seven
items a week, of which four matter.

Pinnarr is the curation layer. You pin a handful of shows; you get a calendar of
their upcoming episodes and a push notification the moment an episode actually
lands in Plex and is watchable.

> **Status: in use, single-household.** Running daily against a real Plex,
> Sonarr and Tautulli. See [SPEC.md](SPEC.md) for the full design and
> [the roadmap](#roadmap) for what is and isn't built.

---

## What makes it different

Most tools in this space are a nicer skin on Sonarr's calendar feed. Pinnarr adds
three things they don't have:

**A curated subset.** Notifications are only useful if they're rare. Pinning
means a push means *something you care about is ready*, not "47 things aired".

**Honest availability state.** Not just "did it air" but "is it actually here":

| State | Meaning |
|---|---|
| `upcoming` | Hasn't aired yet |
| `awaiting` | Aired in the last 48h, not grabbed yet |
| `missing` | Aired more than 48h ago and *still* isn't here — something's wrong |
| `available` | In Plex, watchable now |

That `missing` state is the one nothing else surfaces well. Sonarr buries it
under Wanted.

**Season outlook.** Sonarr's `status: continuing` means only "TVDB hasn't marked
this ended" — a quietly cancelled show keeps that status for years. Pinnarr
combines Sonarr's scheduling data with TMDB's production status to give an
honest verdict:

| Outlook | Meaning |
|---|---|
| `dated` | Next episode has a date |
| `announced` | Next season exists in metadata, no dates yet |
| `in_production` | Filming, nothing scheduled |
| `between_seasons` | Normal gap |
| `dormant` | Nominally continuing, but nothing for 18+ months and not in production |
| `cancelled` / `ended` | Two different things; TMDB distinguishes them |

`dormant` is pin-list hygiene: without it, your pins slowly fill with zombie
shows and nothing ever tells you.

## How it fits together

```
Plex      → what you own, external IDs, genres
Sonarr    → air dates, grab state, season structure
Tautulli  → watch history (makes the pinning pass fast)
TMDB      → production status: cancelled vs on hiatus
                    ↓
              SQLite + FastAPI
                    ↓
        calendar · library browser · ntfy push
```

Series identity is the **TVDB id**, resolved from Plex GUIDs, falling back to
TMDB → IMDb → title+year.

Notifications fire from Sonarr's `On Import` webhook rather than polling, so
they're instant — and they mean *arrived*, not *aired*. Getting pinged at 2am
because something aired in America is a tease.

## Requirements

- Plex Media Server
- Sonarr v3+ API
- A free [TMDB API key](https://www.themoviedb.org/settings/api) (for season outlook)
- Tautulli (optional, improves library sorting)
- An [ntfy](https://ntfy.sh) topic (optional, for push)

## Install

```bash
git clone https://github.com/marctew/pinnarr.git
cd pinnarr
docker compose up -d
```

Then open `http://<host>:8737`, which lands on **Settings**. Fill in Plex and
Sonarr, save, and hit **Test** on each — the Plex test lists your libraries so
you can tick the TV ones rather than hunting for section IDs.

There is no configuration file to edit. `.env` carries only `DATABASE_PATH`
and `LOG_LEVEL`; everything else lives in the database.

**Your tokens are in the database.** `PLEX_TOKEN` is long-lived and grants full
read access to your library, and it now sits in `pinnarr.db` rather than a
`chmod 600` file. Give the data directory permissions to match, and don't hand
the database around.

### Sonarr webhook

Settings → Connect → **+** → Webhook:

- URL: `http://<pinnarr-host>:8737/hooks/sonarr?secret=<the webhook secret from Settings>`
- Method: POST
- Triggers: **On Import**, **On Upgrade**

Sonarr needs to be able to reach Pinnarr, not just the other way round — worth
checking if your *arr stack and Pinnarr are on different VLANs.

### Running on Proxmox

If you're deploying to an LXC, Docker needs `nesting=1` and `keyctl=1` set on
the container. Unprivileged is fine — making it privileged is an outdated
workaround. 1 vCPU / 1GB RAM / 8GB disk is plenty.

## Development

```bash
pip install -r requirements-dev.txt
pytest -q
ruff check app tests
uvicorn app.main:app --reload --port 8737
```

## API

For anything that isn't a browser — a home-automation module, a Stream Deck
plugin, a shell script. Create a key under **Your account → API keys**; it is
shown once and stored hashed.

```bash
curl -H "X-Api-Key: pnr_..." http://pinnarr.lan:8737/api/summary
```

`Authorization: Bearer pnr_...` works equally. A key acts as the account that
made it — same pins, same watch state, same role — so a standard user's key
cannot reach the admin routes.

| Endpoint | Answers |
|---|---|
| `GET /api/summary` | Everything a dashboard needs, in one call |
| `GET /api/schedule?days=7&back=1` | What's on, for your pins |
| `GET /api/next` | The single next episode |
| `GET /api/watching` | Shows you're partway through |
| `GET /api/arrivals?hours=24` | What landed recently — the automation trigger |
| `GET /api/downloads` | The queue, and what has stalled |
| `POST /api/series/{id}/refresh-watched` | Re-read one show's watch state from Plex now |
| `POST /api/series/{id}/watched` | Mark a show (or `season=N`) watched in Plex |
| `POST /api/episodes/{id}/watched` | Mark one episode watched in Plex |
| `GET /api/calendar` | The raw calendar feed |
| `GET /healthz` | Liveness, config state and last job runs (no key needed) |

`/api/summary` is deliberately not RESTful: a wall display redrawing every
minute shouldn't make six requests to fill one screen, and every number in it
comes from the same instant. It carries `healthy` and `failing_jobs` — not
"is Pinnarr up", since you just reached it, but whether what it's telling you
is still fresh.

Full schemas at `/api/docs`.

## Roadmap

Built:

- [x] Schema, migrations, database-backed config with an admin panel
- [x] Plex / Sonarr / Tautulli / TMDB / ntfy clients
- [x] Season outlook engine
- [x] Sync jobs, scheduler and series identity resolution
- [x] Arrival notifications, reconcile pass and weekly digest
- [x] Calendar — month grid, agenda, and live download progress
- [x] Library browser with faceted filtering and bulk pin
- [x] Sonarr webhook receiver (`/hooks/sonarr`)
- [x] Poster proxy route (`/poster/{id}`)
- [x] Accounts: admin-created users, per-user pins, notifications and watch state
- [x] Two-way pin sync with Sonarr tags and the Plex Watchlist
- [x] Per-episode watch state from Plex, with Tautulli filling in play times
- [x] Ready to watch, gaps, retire and discover
- [x] Backup and restore
- [x] Cast cross-referencing against your own library
- [x] API keys and a read API for integrations

Not built:

- [ ] Radarr digital-release track (movies, GB region)
- [ ] iCal feed of pinned episodes

## Licence

MIT
