# AGENTS.md

Working notes for anyone (human or AI agent) changing this repository. The README describes the product; this file
lists what you must know before touching it. Keep it short and current.

## What this is

PodcasterNG turns YouTube playlists into podcast feeds. A **server** (FastAPI, SQLite in WAL mode, Jinja2 + htmx, no
build step) serves the feeds, files, the admin UI and a work queue. Replaceable **workers** (yt-dlp, ffmpeg, deno) poll
`/api/worker/*` with bearer tokens, download and verify videos and upload the finished package.

- `server/app` the server, `server/migrations` numbered SQL, `server/tests` all tests (server, shared and worker).
- `worker/pn_worker` the worker, `shared/pn_shared` code and schemas used by both, `scripts/` helper scripts.
- The **server image must not contain yt-dlp, ffmpeg or deno**. Only the worker image does.
- Language: code, comments, docs and commit messages in English.

## Rules that are easy to break

- **Secrets:** never read, print, log or copy `.env`, `cookies.txt` or tokens. Configuration is environment variables
  only; `.env.example` has empty values. Error texts are scrubbed (`app/scrub.py`); the Telegram token is part of a URL, so
  httpx logging stays at WARNING.
- **`compose.yml` is the owner's private file** (git-ignored). Change `compose.example.yml`; touch `compose.yml` only when asked.
  `docs/` is git-ignored planning material: never copy it into tracked files. The README must stand alone.
- **Git:** commit only when asked, never push unless asked. **Stage explicit paths, never `git add -A`**: more than one
  session may work in the tree (an earlier `git add -A` swept another session's work into an unrelated commit).
- **Migrations:** `server/migrations/NNNN_name.sql`, applied at startup after a copy of the database. Check the highest
  existing number (also in other sessions' uncommitted work) before adding one. Never edit an applied migration.
- **All database access goes through `app.db.connect`** (WAL, busy timeout, foreign keys, autocommit). Use explicit
  `BEGIN IMMEDIATE` transactions (`queue.transaction`).
- **Task state changes live only in `app/queue.py`** (claims, leases, failures, tiers, pausing, reopening). Add behaviour there.
- Every network call has a timeout. Request bodies are limited (`BodyLimit`); the worker upload has per-file limits.
- Admin pages need a session and a CSRF token; the CSP forbids inline scripts and styles. Put JS in `static/`.
- Production imports must be in `server/requirements.txt` (not only in `requirements-dev.txt`); `test_requirements.py`
  guards this. Pin versions.

## Tests

There is no host virtualenv. Run the whole suite in a throwaway container (about 2 minutes):

```bash
docker run --rm -v "$PWD:/src:ro" python:3.12-slim bash -c \
  'cp -r /src /tmp/s && cd /tmp/s/server && pip install -q -r requirements-dev.txt && pytest -p no:cacheprovider -q'
```

Fakes: `tests/fake_ytdlp.py` (yt-dlp, ffprobe), `tests/tghelpers.py` (Telegram), `httpx.MockTransport` for HTTP. Do not call
the real YouTube or Telegram from tests. A step is done when the tests pass **and** the real container was rebuilt and checked.

## Deploying on this host

```bash
docker compose build server && docker compose up -d server   # worker: build worker, up -d worker
docker compose ps                                            # wait for "healthy"
```

Use project and container names starting with `podcaster-ng-` and never touch other containers. For experiments use a separate
compose project and data directory. The public host name is served by Traefik (labels in the private `compose.yml`); the home
worker pulls its image from the container registry built by `.github/workflows/build.yml`.

## Things learned the hard way

- YouTube blocks downloads from the server's IP (`bot_check`); scans (metadata) work. Downloads succeed from a worker on a
  home IP (tier 2). Tiers escalate on `bot_check`, `internal` and `incomplete`, not on `network` or `timeout`.
- Filenames follow yt-dlp's sanitising (full-width look-alike characters); `tests/data/real_titles.json` guards it.
- Podcast apps cache by URL: cover files carry a content hash in their name. Feed GUIDs and enclosure URLs must stay stable.
- The sidebar page list is in `app/web.py` (`PAGES`) and `app/admin.py` (`IMPLEMENTED`): a new page needs both.
- Backups: the SQLite backup API writes `data/backups/daily/`; media files are not in it, so the host backup must include `data/`.
- Admin actions are not audited; the Events page only shows task attempts, escalations, deletions and Telegram messages.
