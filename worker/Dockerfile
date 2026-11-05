# Build from the repository root:  docker build -f worker/Dockerfile -t podcaster-ng-worker .
# Contains yt-dlp (with its JS challenge solver), deno, ffmpeg/ffprobe and Pillow. The server image never does.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /srv
COPY worker/requirements.txt .
RUN pip install -r requirements.txt

COPY shared/pn_shared ./pn_shared
COPY worker/pn_worker ./pn_worker

# /work holds the files of the running task; mount a volume there (an empty named volume inherits this owner).
# HOME and DENO_DIR point to /tmp so the container also runs with a read-only root filesystem.
RUN useradd -u 1000 -m -s /usr/sbin/nologin worker && mkdir /work /tmp/pn-work && chown worker /work /tmp/pn-work
ENV HOME=/tmp DENO_DIR=/tmp/deno WORK_DIR=/work
USER worker
VOLUME /work

# The worker touches this file on every loop iteration, also while the server is down (backoff <= 60 s).
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s --retries=3 \
    CMD ["python", "-c", "import os,sys,time; sys.exit(0 if time.time()-os.path.getmtime('/tmp/pn_worker_alive') < 180 else 1)"]

CMD ["python", "-m", "pn_worker"]
