# Pinnarr

**Pin the shows you actually care about, and get a calendar of just those.**

Sonarr already has a calendar. The problem is that it shows *everything* — every
series you ever added, including the one you abandoned in 2019. Forty-seven
items a week, of which four matter.

Pinnarr is the curation layer. You pin a handful of shows; you get a calendar of
their upcoming episodes and a push notification the moment an episode actually
lands in Plex and is watchable.

> **Status: early development.** Not yet usable. See [SPEC.md](SPEC.md) for the
> full design and [the roadmap](#roadmap) for what's built so far.

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

## Roadmap

- [x] Schema, migrations, config
- [x] Plex / Sonarr / Tautulli / TMDB / ntfy clients
- [x] Season outlook engine
- [x] Sync jobs, scheduler and series identity resolution
- [x] Arrival notifications, reconcile pass and weekly digest
- [ ] Calendar view
- [ ] Library browser with faceted filtering and bulk pin
- [ ] Sonarr webhook receiver (`/hooks/sonarr`)
- [ ] Poster proxy route (`/poster/{id}`)
- [ ] Radarr digital-release track (movies, GB region)
- [ ] iCal feed of pinned episodes

## Licence

MIT
