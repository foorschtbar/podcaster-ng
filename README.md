# PodcasterNG

Turns YouTube playlists into podcast feeds. The **server** coordinates and serves (feeds, files, queue, admin UI, SQLite); replaceable **workers** (yt-dlp, ffmpeg, deno) run on any network and upload finished episodes.

**Status:** server skeleton plus a mobile-first admin shell behind a login with podcast and settings management, public feeds and files compatible with the old Podcaster, an import assistant, and hit statistics with a dashboard, worker registration, and a work queue: the server schedules scans of the playlists, workers claim and run them, and the results become download tasks. Workers download the videos, verify and upload them, and the episodes appear in the feed.

## Layout

| Path | Content |
|---|---|
| `scripts/` | `compare_feeds.py`, `restore_db.py` |
| `shared/` | `pn_shared`: worker API schemas (Pydantic) and logging, used by server and worker |
| `worker/` | `pn_worker`: the worker (hello, ping, backoff) and its Dockerfile |
| `server/` | Python package `app` (FastAPI), `migrations/`, tests, Dockerfile |
| `compose.example.yml` | template for `compose.yml`: server, optional worker, data bind mount `./data` |
| `compose.worker.example.yml`, `worker.env.example` | templates for a standalone worker on another machine |
| `compose.yml` | your private copy (git-ignored): domain, resolver, mounts |
| `.env.example` | supported environment variables |

The server image contains no yt-dlp, ffmpeg or deno.

## Run

```bash
cp compose.example.yml compose.yml   # private copy, git-ignored: adapt domain, proxy, mounts
cp .env.example .env     # set at least ADMIN_USER and ADMIN_PASSWORD
mkdir -p data            # create as your own user, the container runs as UID 1000
docker compose up -d --build
docker compose ps        # STATUS should become "healthy"
```

No port is published. Reach it through a reverse proxy (see the commented Traefik block in `compose.example.yml`) or check from inside:

```bash
docker exec podcaster-ng-server python -c "import urllib.request as u; print(u.urlopen('http://127.0.0.1:8080/healthz').read())"
```

## Configuration (environment variables)

| Variable | Default | Meaning |
|---|---|---|
| `DATA_DIR` | `/data` | database, backups and (later) media |
| `BASE_URL` | empty | public base URL used in feeds |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |
| `ADMIN_USER`, `ADMIN_PASSWORD` | empty | admin login; if either is empty the admin UI is locked (503 "access not configured") |
| `IMPORT_DIR` | `/import` | the import assistant may only read below this directory |
| `SESSION_SECRET` | empty | signs session cookies; if empty a secret is generated once and stored in the `settings` table |

Logs are JSON lines on stdout.

## Public feeds and files

Same URLs as the old Podcaster, no login:

- `GET /<podcast-id>.xml` – RSS feed (only `available` episodes, newest first). The base URL is taken from Settings, then `BASE_URL`, then the request.
- `GET /files/<podcast-id>/<file>` – `.m4a` (`audio/x-m4a`), `.jpg/.jpeg/.png/.webp` and `.chapters.json` (`application/json+chapters`) with range requests and conditional requests. Anything else, hidden names, symlinks and path tricks return 404.

Files live in `data/media/<podcast-id>/`, file names as in the old project. Feed items use the file URL as GUID, a square image per episode and a `podcast:chapters` link when a chapters file exists.

## Workers

A worker is a separate process (yt-dlp, ffmpeg and deno) that can run on any network and only needs outbound HTTPS to the server.

1. In the admin UI open **Workers → New worker** (name, tier, optional pacing limits). The token (`pnw_…`, 32 random bytes) is shown **once**, in the response to the creation; the server stores only its SHA-256 hash. *Regenerate token* invalidates the old one at once; *Delete* revokes it.
2. Start the worker with the token:

   ```bash
   # on any machine with Docker; build the image from this repository once
   docker build -f worker/Dockerfile -t podcaster-ng-worker .
   docker run -d --restart unless-stopped \
     -e SERVER_URL=https://podcaster.example.org -e WORKER_TOKEN=pnw_… podcaster-ng-worker
   ```

   Or on the same host as the server: put `WORKER_TOKEN` into `.env` and run `docker compose --profile worker up -d worker` (it reaches the server at `http://server:8080` over the internal network).

The worker calls `/hello` on start and `/ping` every 30 s, has a timeout on every request, backs off (2 s → 60 s, with jitter) while the server is unreachable and says hello again when it is back. A rejected token is retried every 60 s, so it can be fixed in the UI. Its container health check watches a heartbeat file, not the server.

Status: *online* if the worker was seen within the last 120 s (Settings → Workers), otherwise *offline*; *never connected* before the first call; *inactive* when deactivated. The dashboard lists the workers too.

### Worker API v1

Header `Authorization: Bearer <token>`, base path `/api/worker`, separate from the public and admin routes. Schemas: `shared/pn_shared/api.py`.

| Call | Purpose |
|---|---|
| `POST /hello` | worker reports its version, yt-dlp/ffmpeg/deno versions and capabilities; the reply has server time, API version, the worker's effective pacing limits, lease duration and ping interval |
| `GET /ping` | authenticated liveness call |
| `POST /claim` | ask for work (`{"types": ["scan"]}`); the reply holds a task or `retry_after_seconds` |
| `POST /tasks/{id}/heartbeat` | extend the lease of the claimed task (`409` if it is not yours any more) |
| `POST /tasks/{id}/scan-result` | report the videos of a playlist scan |
| `POST /tasks/{id}/upload` | upload one file of a package as the raw body (`role`, `size`, `sha256`, `ext` as query parameters): `audio`, `info` (info.json), `thumb`, `square`, `chapters` |
| `POST /tasks/{id}/complete` | send the manifest (role, size, SHA-256 per file); the server verifies and publishes the package |
| `POST /tasks/{id}/fail` | report an error class and text (`bot_check`, `not_yet_available`, `private`, `removed`, `network`, `timeout`, `internal`, `incomplete`) |

`401` for a missing, unknown or revoked token, `403` for a deactivated worker, `429` when rate limited: invalid tokens are limited per client address (10 failures in 5 minutes lock that address out for 5 minutes, but never a valid token), valid workers to 120 calls per minute. Every authenticated call updates *last seen*. Tokens are never logged or stored in clear text.

## Queue, claims and scans

State machine in `server/app/queue.py` (the only place that changes task states; API, scheduler and UI call it). Every change runs in one `BEGIN IMMEDIATE` transaction.

```
pending --claim--> claimed --scan-result--> done
   ^                  |
   |      fail / lease expired
   +---- retry <------+-----> closed (private / removed video, or closed by hand)
```

- **Scheduler** (a background task in the server, every 15 s): returns tasks whose lease expired to `pending` (recording a `lease_expired` attempt) and creates a `scan` task for every active podcast whose scan interval has elapsed and that has no open scan. Set `SCHEDULER=0` to switch it off.
- **Claim:** atomic; a worker holds at most one task; scans come before downloads, oldest first; tasks wait for `not_before`; paused podcasts are skipped. For downloads the pacing limits apply: downloads per hour per worker, downloads per hour per podcast (the worker's value caps the podcast's), and a pause between downloads (the larger of worker and podcast). Without a task the reply says when to ask again. All tiers see all tasks for now; escalation across tiers comes with the alerting step.
- **Lease:** 300 s (Settings → Queue). The worker extends it with a heartbeat while it works; a worker that is killed simply stops, the lease runs out, the task becomes pending and the next claim succeeds. A late result from the old worker is answered with `409`.
- **Scan result:** the first *scan depth* videos count. A video becomes a `download` task unless it has an episode row (in any state) or already had a download task (in any state; closed tasks are reopened with *Retry*). Premieres and live streams get `not_before` (announced start plus 30 minutes) instead of failing.
- **Failures:** retried after a growing backoff (1, 2, 4 … 30 minutes); `not_yet_available` waits for `not_before` without counting as a failure; `private`/`removed` close the task. Error texts are trimmed to 500 characters and scrubbed of tokens, credentials and cookies before they are stored.
- **Tasks page** (`/tasks`): filter by state, podcast and type, details with all attempts, and *Release*, *Retry* and *Close*. *Scan now* is on each podcast.

The worker runs scans with the yt-dlp command line (metadata only, `--flat-playlist`) as a separate process with a hard time limit (`SCAN_TIMEOUT`, default 300 s), so a hung scan is killed. `YTDLP_PROXY` sets a proxy for yt-dlp only; the calls to the server never use proxy environment variables. `POLL_INTERVAL` overrides the idle wait between claims. A worker without yt-dlp only pings.

## Escalation, pausing and Telegram alerts

**Tiers.** Every worker has a tier (1 is tried first). A task has a current tier and is visible to workers of that tier and below. A new task starts at tier 1 and moves to the next tier that has an active worker

- when a worker of the current tier reports `bot_check`, `internal` or `incomplete` for it (a different IP or machine may succeed; `network` and `timeout` are only retried with backoff, `private`, `removed` and `not_yet_available` never escalate), or
- when no worker took it within the escalation deadline (Settings → Queue, default 10 minutes; it counts from when the task became due, so a tier-1 worker that is offline does not hold things up).

A worker does not get a task back that it reported as failed within ten minutes, as long as another worker could take it. Every move is recorded in the task's *escalation history*.

**Pausing.** Failed attempts since the task was last (re)opened are weighed: `bot_check`, `internal` and `incomplete` count 1, `network`, `timeout` and an expired lease count 1/4, `not_yet_available`, `private` and `removed` do not count. When the sum reaches *N* (default 4) **and** the failures come from at least two workers, the task is `paused` with a reason and one Telegram message is queued. If only one worker is active, one worker is enough (otherwise a single blocked worker would never raise the alarm). A paused task is not offered to workers. It reopens by itself after the reopen timer (default 6 hours) with a clean slate at tier 1, or at once with *Release (reopen)* or *Retry* in the UI; *Close* ends it.

**Telegram.** Create a bot with @BotFather, send it a message, and enter its token and your chat id under Settings → Telegram; *Send test message* uses the saved values. Messages: a task was paused (podcast, video, failed attempts per worker, last errors, a link), a worker has been offline for more than 15 minutes (default) and when it is back, and optionally every new episode. Messages go through an outbox in the database: each condition has a unique key, so it produces at most one message however often the scheduler looks; a failed delivery is retried with backoff (up to 8 times); without a configured bot token and chat id messages are marked *skipped*. The bot token is part of every Telegram request URL, so HTTP request logging is switched off for it and error texts are scrubbed. After a server start the watchdog waits three minutes so that the workers can reconnect first.

The dashboard shows the queue, each worker's success rate over the last week, `bot_check` failures per day, and the latest Telegram messages with their delivery status.

## Downloads and packages

A `download` task turns one video into an episode:

1. **Worker:** yt-dlp (`-f m4a/bestaudio/best -x --audio-format m4a`, thumbnail and `info.json`, as in the old project; JS challenges are solved with deno), then verification: `info.json` present with the right video id, ffprobe can read the m4a, the audio is AAC, and its duration is within 10 s / 3 % of the metadata. Then the square artwork (1400 px, blurred background with the original centred) and the chapters file (Podcasting 2.0, titles at most 45 characters). A hard time limit (`DOWNLOAD_TIMEOUT`, default 3600 s) kills yt-dlp; `YTDLP_COOKIES` and `YTDLP_PROXY` are optional.
2. **Upload:** every file is streamed to `data/staging/<task>-<attempt>/` with its SHA-256; the server hashes while receiving and refuses a wrong hash, a wrong size, a file that is not what its role says, or one above the upload limit (Settings → Uploads).
3. **Complete:** the server recomputes the hashes, checks that `audio` and `info.json` exist and that the `info.json` belongs to the video, and only then moves the files into `data/media/<podcast>/` and creates the episode row, in this order, so a feed never shows a half-published item. If a step fails, the files go back to staging and nothing is visible. A package that does not verify is recorded as an `incomplete` attempt (retried with backoff).
4. **Names** are assigned by the server from the title, exactly as yt-dlp names files (`%(title)s`, including the full-width look-alikes for `: ? " * < > | /`), so new and imported episodes look alike; this is tested against yt-dlp and the real imported file names. If the name is taken, ` [video id]` is added; names never start with a dot.
5. **Pacing:** one task per worker at a time, the pause between downloads (server minimum, then a random pause between the configured min and max on the worker) and downloads per hour. A premiere is rescheduled with `not_before` (from the scan, or from yt-dlp's "will begin in N hours") and is not counted as a failure.

Stale staging directories (finished or abandoned attempts, or untouched for 12 hours) are removed when the server starts and every night. The worker image contains yt-dlp, deno, ffmpeg and Pillow; the server image still contains none of them.

Behind a reverse proxy, the maximum request body size has to be at least the upload limit (default 1024 MB), and its timeouts have to allow a slow upload.

## Episodes and retention

- **Episodes** (admin UI): list with filter by podcast and state, per-podcast totals (in feed / hidden / deleted, disk space) and a
  detail page with the files and their sizes. Actions: **hide / show** (state `hidden` leaves the feed at once, files stay),
  **delete files** (state `deleted`, row kept, never downloaded again by a scan) and **re-download** (deletes the files and queues
  a new download task; the episode comes back through the normal worker pipeline, normally with the same file name). Deleting and
  re-downloading go through a confirmation page.
- **Retention**: per podcast *keep the last N* and/or *delete episodes older than X days* (podcast value, else the default in
  Settings; empty = off). The **Retention** page shows a preview (what would go, how much space it frees) and has *run now*; the same
  preview is on the podcast page. Only files are removed (m4a, images, chapters, `info.json`); the row stays as `deleted`, so scans
  skip it and the hit statistics keep their history.
- Safety: the newest episode of a podcast is never deleted; a run deletes at most *retention_max_per_run* episodes (default 25, oldest
  first, shared by all podcasts; the rest follows in the next run); every deleted episode is logged (visible on the Retention page and
  the episode).
- The **nightly run** is off by default (Settings: *Delete old episodes automatically*, time in UTC, default 03:45) and runs once a
  day from the scheduler. Rows are changed before the files are unlinked, so a failure leaves at worst a stray file, never a feed
  entry without a file.

## Statistics and dashboard

Anonymous daily counters in `hits_daily` (day, podcast, episode, kind, app group, count, bytes). No IP addresses and no raw user agents are stored; the user agent is only mapped to an app group (table `RULES` in `server/app/app_groups.py`: Apple Podcasts, Overcast, Pocket Casts, Spotify, …, Bots, Browsers, Other).

Counting rules (days are UTC):

- only the public routes are counted, and only successful `GET` responses; admin, `/healthz`, static files, `HEAD`, `304` and errors are not;
- `feed`: every feed request; `file`: audio (`.m4a`) downloads only, not artwork or chapter files;
- a download is counted once: a request without `Range`, or one whose first range starts at byte 0. Further ranges of the same download add their bytes but no count. Apps that probe with several `bytes=0-` requests can still count more than once;
- bytes are the size of each response.

Old counters are deleted at start and every night at 03:30 UTC; the retention (default 730 days) is set under Settings → Statistics.

The dashboard (`/dashboard`) shows downloads, feed requests and transferred data for 30 days (SVG charts rendered on the server, hover/keyboard tooltips, a table view below), top episodes, shares per app, disk space and storage per podcast. The workers/queue/error card is a placeholder until those exist.

## Import from the old Podcaster

Mount the old project read-only (uncomment the two mounts in your `compose.yml`, see `compose.example.yml`), restart, then Settings → Import assistant. It shows a dry run first (podcasts, episodes, sizes, problems) and imports only after confirmation. It reads `config.yml` and the downloads directory (`jobs`, `baseurl`, `interval`, `*.info.json`, `*.m4a`, thumbnails, chapters, `archive.txt`; the cookie file is never read), copies the files with unchanged names into `data/media/` and records videos from `archive.txt` without files as known (`deleted`) so they are not downloaded again. The old project is never modified. The import is only possible while there are no episodes; the database is written in one transaction after all files are in place, so a failed run can simply be repeated.

Compare the feeds of both instances (host names are ignored):

```bash
python3 scripts/compare_feeds.py https://old.example.org https://new.example.org abgt asot
```

## Admin UI

Served at `/`: `/login`, then Dashboard, Podcasts, Episodes, Workers, Tasks and Settings (plus Retention under Episodes).

- Session: signed cookie (HttpOnly, Secure, SameSite=Lax, 12 h). The `Secure` flag means the UI must be reached over HTTPS, i.e. through the reverse proxy. Logout removes the cookie; sessions are stateless, so changing `SESSION_SECRET` (or deleting `session_secret` from `settings`) invalidates all of them.
- Failed logins: 1 s delay each, 5 failures within 15 min lock the client address out for 15 min (in memory, reset on restart).
- CSRF: every state-changing admin request needs the session's token, as form field `csrf_token` or header `X-CSRF-Token` (set for htmx on `<body>`).
- The client address comes from the proxy headers; the image trusts them from any peer (`FORWARDED_ALLOW_IPS=*`). That is only safe because no port is published – keep it that way, or restrict the variable to the proxy's address.
- **Podcasts:** create, edit, pause/resume and delete (with a confirmation page). The ID (slug, lowercase letters, digits and single dashes) is part of the public feed URL and cannot be changed after creation. Scan interval, pacing and retention fields may be left empty to follow the defaults from Settings.
- **Settings:** base URL (falls back to `BASE_URL`), default scan interval, pacing and retention, upload limit, task lease, escalation deadline, failure threshold and Telegram credentials. Changes apply immediately, no restart needed. The Telegram token is never sent back to the browser; leave the field empty to keep it. It is stored unencrypted in the database, so backups of `data/` contain it.
- Input is validated on the server; on any error nothing is saved and the form is shown again with messages.
- htmx is vendored in `server/app/static/` (no CDN, no build step).

## Operations

### Layout of a deployment

- **Server host:** `compose.yml` (copy of `compose.example.yml`) runs the server behind your reverse proxy, optionally with a local worker (`docker compose --profile worker up -d worker`, `WORKER_TOKEN` in `.env`).
- **Home worker:** on any other machine copy `compose.worker.example.yml` to `compose.worker.yml` and `worker.env.example` to `worker.env` (`SERVER_URL`, `WORKER_TOKEN`), then `docker compose -f compose.worker.yml pull && docker compose -f compose.worker.yml up -d` (the image comes from the container registry, see *Images* below). No port is opened. In the admin UI give it a higher tier than the local worker (tier 2 is only used when tier 1 fails or is offline) and a conservative pacing (for example 2 downloads per hour, 30–120 s pause). Check it with *Retry now* on a task that was blocked on the server's IP.
- Containers run as a non-root user with a read-only root filesystem, all capabilities dropped and `no-new-privileges`; the server may write only `/data` and `/tmp`, the worker `/work` (a volume) and `/tmp`. The worker has memory, CPU and process limits (`mem_limit`, `cpus`, `pids_limit`), the server a memory and process limit. Adapt them in your `compose.yml`.

### Limits and timeouts

Every outgoing call has a timeout (worker to server 10/20 s, uploads up to 10 min, Telegram 10/15 s, yt-dlp socket 30 s and a hard limit per scan and per download task). The server refuses request bodies over 1 MiB (413) everywhere except the worker upload, which is limited per file role (audio: *Upload size limit* in Settings, default 1024 MB; images and metadata a few MB). **The reverse proxy must allow at least that body size** for `/api/worker/tasks/*/upload`; keep the setting at or below the proxy's limit.

### Backups

- The server writes a consistent copy of the database every day after the configured time (Settings → Backups, default 02:30 UTC) to `data/backups/daily/podcaster-YYYYMMDD.db`, using the SQLite backup API (safe while the server writes). Each copy is checked (`PRAGMA integrity_check`) before it replaces the temporary file; the newest 14 are kept. A failed backup sends a Telegram message. Migrations additionally copy the database to `data/backups/` before they run.
- Include `data/` in your regular host backup (database, `media/` and `backups/`). `data/staging/` holds only unfinished uploads and may be excluded. The settings table contains the Telegram bot token, so protect the backups.
- **Check a backup:** `python3 scripts/restore_db.py --check data/backups/daily/podcaster-20261105.db` prints its schema version and row counts.
- **Restore:** stop the server (`docker compose stop server`), then
  `python3 scripts/restore_db.py data/backups/daily/podcaster-20261105.db data/` and `docker compose start server`. The script verifies the copy, moves the current database aside as `podcaster.db.before-restore-<time>` (nothing is deleted) and puts the copy in place. Media files are not part of the database copy; restore `data/media/` from the host backup. A backup of an older version is migrated at the next start.
- Try the restore once on a scratch directory before you need it (`mkdir /tmp/rt && cp -r data/backups /tmp/rt/ && python3 scripts/restore_db.py --check …`, then restore into `/tmp/rt`).

### Images (GitHub Actions)

`.github/workflows/build.yml` runs the tests and then builds `podcaster-ng-server` and `podcaster-ng-worker` for amd64 and arm64 and pushes them to `ghcr.io/<owner>/…`: pushes to `master` give the tags `latest` and `sha-<commit>`, a tag `v1.2.3` gives `1.2.3`; pull requests only build. Run it by hand from the Actions tab (*Run workflow*) to rebuild with fresh base images. A machine that should pull a private package needs `docker login ghcr.io` once with a GitHub personal access token that has `read:packages` (or make the package public in its settings). To use the server image instead of building it locally, replace `build:` by `image: ghcr.io/<owner>/podcaster-ng-server:latest` in your `compose.yml`.

### Upgrades

```bash
git pull
docker compose build            # add --pull to refresh the base images
docker compose up -d            # migrations run at startup, after a copy of the database
docker compose ps               # wait for "healthy"
```

To go back, check out the previous version, rebuild and restore the database copy from `data/backups/` made before the migration (the server refuses a database that is newer than its code).

### Keeping yt-dlp current

yt-dlp ages quickly. The worker image pins its versions in `worker/requirements.txt`; the version in use is shown per worker under Workers. To update: change the `yt-dlp` pin there (and `deno` if needed), then on every worker host `docker compose build --pull worker && docker compose up -d worker` (home worker: `docker compose -f compose.worker.yml pull && docker compose -f compose.worker.yml up -d`, after the new image was built by the pipeline). Do it when downloads start failing with `internal` errors, or monthly.

### Logs

JSON lines on stdout. Tokens, cookies and passwords are not logged: the Telegram client keeps the bot token out of URLs in errors and log lines, worker error texts are scrubbed before they are stored, and the access log contains no headers or bodies.

### Rollout and cut-over from the old Podcaster

1. **Parallel phase.** Run PodcasterNG under its own host name, import the old data (Settings → Import assistant) and compare: `python3 scripts/compare_feeds.py https://old.example.org https://new.example.org abgt asot` must report identical items, GUIDs, enclosure URLs and lengths. While comparing, keep the old project as the only one that downloads: leave the PodcasterNG workers stopped (scan and download tasks then simply wait in the queue).
2. **Enable PodcasterNG downloads.** Start the workers and check a manually triggered download (*Retry now*) of a video the server's IP could not fetch; it must appear in the feed with the same file naming.
3. **Cut-over.** Stop the old container (leave its config, archive and data untouched), point the reverse proxy router of the public host name at PodcasterNG and set *Base URL* to that host name. Verify in a podcast app that the subscription still works and shows no duplicate episodes, and watch the hit counters on the dashboard.
4. **Fallback.** Keep the old stack stopped but intact for a week. To go back: point the router at the old container again and start it (`docker compose up -d` in its directory); feeds are served again as soon as the container is up. Because the GUIDs and file URLs are identical, apps do not notice.

## Database and migrations

SQLite in WAL mode with foreign keys on, stored at `$DATA_DIR/podcaster.db`. All code opens it through `app.db.connect`.

Numbered files `server/migrations/NNNN_name.sql` are applied at startup, in order, each in one transaction; applied versions are recorded in `schema_migrations`. Before pending migrations are applied to an existing database, a copy is written to `$DATA_DIR/backups/`. If the database is newer than the code, the server logs an error and exits with status 1.

## Tests

The tests (server, shared code and worker) need the packages listed in `server/requirements-dev.txt`. Without a local virtualenv, run them in a throwaway container:

```bash
docker run --rm -v "$PWD/server:/src/server:ro" -v "$PWD/scripts:/src/scripts:ro" \
  -v "$PWD/shared:/src/shared:ro" -v "$PWD/worker:/src/worker:ro" python:3.12-slim sh -c \
  'cp -r /src /tmp/s && cd /tmp/s/server && pip install -q -r requirements-dev.txt && pytest -q -p no:cacheprovider'
```
