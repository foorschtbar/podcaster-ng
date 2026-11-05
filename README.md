# PodcasterNG

Turns YouTube playlists into podcast feeds. The **server** coordinates and serves (feeds, files, queue, admin UI, SQLite); replaceable **workers** (yt-dlp, ffmpeg, deno) run on any network and upload finished episodes.

**Status:** the server skeleton only – it starts, creates its SQLite database with migrations and answers `GET /healthz`. No UI, feeds or workers yet.

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

Logs are JSON lines on stdout.

## Database and migrations

SQLite in WAL mode with foreign keys on, stored at `$DATA_DIR/podcaster.db`. All code opens it through `app.db.connect`.

Numbered files `server/migrations/NNNN_name.sql` are applied at startup, in order, each in one transaction; applied versions are recorded in `schema_migrations`. Before pending migrations are applied to an existing database, a copy is written to `$DATA_DIR/backups/`. If the database is newer than the code, the server logs an error and exits with status 1.

## Tests

The tests need `fastapi`, `pytest` and `httpx2` (see `server/requirements-dev.txt`). Without a local virtualenv, run them in a throwaway container:

```bash
docker run --rm -v "$PWD/server:/src:ro" python:3.12-slim sh -c \
  'cp -r /src /tmp/s && cd /tmp/s && pip install -q -r requirements-dev.txt && pytest -q -p no:cacheprovider'
```
