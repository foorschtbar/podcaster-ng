# Build from the repository root:  docker build -f worker/Dockerfile -t podcaster-ng-worker .
# Later steps add yt-dlp, ffmpeg and deno to this image; the server image never contains them.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /srv
COPY worker/requirements.txt .
RUN pip install -r requirements.txt

COPY shared/pn_shared ./pn_shared
COPY worker/pn_worker ./pn_worker

RUN useradd -u 1000 -M -s /usr/sbin/nologin worker
USER worker

# The worker touches this file on every loop iteration, also while the server is down (backoff <= 60 s).
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/pn_worker_alive') < 180 else 1)"]

CMD ["python", "-m", "pn_worker"]
