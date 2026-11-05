# PodcasterNG

Turns YouTube playlists into podcast feeds. The **server** coordinates and serves (feeds, files, queue, admin UI, SQLite); replaceable **workers** (yt-dlp, ffmpeg, deno) run on any network and upload finished episodes.

**Status:** server skeleton plus a mobile-first admin shell behind a login with podcast and settings management. No feeds or workers yet.

## Layout

| Path | Content |
|---|---|
| `server/` | Python package `app` (FastAPI), `migrations/`, tests, Dockerfile |
| `worker/`, `shared/` | placeholders |
| `compose.yml` | server service, data bind mount `./data` |
| `.env.example` | supported environment variables |

The server image contains no yt-dlp, ffmpeg or deno.

## Run

```bash
cp .env.example .env     # optional, all values may stay empty
mkdir -p data            # create as your own user, the container runs as UID 1000
docker compose up -d --build
docker compose ps        # STATUS should become "healthy"
```

No port is published. Reach it through a reverse proxy (see the commented Traefik labels in `compose.yml`) or check from inside:

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
| `SESSION_SECRET` | empty | signs session cookies; if empty a secret is generated once and stored in the `settings` table |

Logs are JSON lines on stdout.

## Admin UI

Served at `/`: `/login`, then Dashboard, Podcasts, Episodes, Workers, Tasks and Settings. Podcasts and Settings are functional; the other pages are empty placeholders for now.

- Session: signed cookie (HttpOnly, Secure, SameSite=Lax, 12 h). The `Secure` flag means the UI must be reached over HTTPS, i.e. through the reverse proxy. Logout removes the cookie; sessions are stateless, so changing `SESSION_SECRET` (or deleting `session_secret` from `settings`) invalidates all of them.
- Failed logins: 1 s delay each, 5 failures within 15 min lock the client address out for 15 min (in memory, reset on restart).
- CSRF: every state-changing admin request needs the session's token, as form field `csrf_token` or header `X-CSRF-Token` (set for htmx on `<body>`).
- The client address comes from the proxy headers; the image trusts them from any peer (`FORWARDED_ALLOW_IPS=*`). That is only safe because no port is published – keep it that way, or restrict the variable to the proxy's address.
- **Podcasts:** create, edit, pause/resume and delete (with a confirmation page). The ID (slug, lowercase letters, digits and single dashes) is part of the public feed URL and cannot be changed after creation. Scan interval, pacing and retention fields may be left empty to follow the defaults from Settings.
- **Settings:** base URL (falls back to `BASE_URL`), default scan interval, pacing and retention, upload limit, task lease, escalation deadline, failure threshold and Telegram credentials. Changes apply immediately, no restart needed. The Telegram token is never sent back to the browser; leave the field empty to keep it. It is stored unencrypted in the database, so backups of `data/` contain it.
- Input is validated on the server; on any error nothing is saved and the form is shown again with messages.
- htmx is vendored in `server/app/static/` (no CDN, no build step).

## Database and migrations

SQLite in WAL mode with foreign keys on, stored at `$DATA_DIR/podcaster.db`. All code opens it through `app.db.connect`.

Numbered files `server/migrations/NNNN_name.sql` are applied at startup, in order, each in one transaction; applied versions are recorded in `schema_migrations`. Before pending migrations are applied to an existing database, a copy is written to `$DATA_DIR/backups/`. If the database is newer than the code, the server logs an error and exits with status 1.

## Tests

The tests need `fastapi`, `pytest` and `httpx2` (see `server/requirements-dev.txt`). Without a local virtualenv, run them in a throwaway container:

```bash
docker run --rm -v "$PWD/server:/src:ro" python:3.12-slim sh -c \
  'cp -r /src /tmp/s && cd /tmp/s && pip install -q -r requirements-dev.txt && pytest -q -p no:cacheprovider'
```
