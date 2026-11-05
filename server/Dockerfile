FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

WORKDIR /srv
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
COPY migrations ./migrations

ARG UID=1000
ARG GID=1000
RUN groupadd -g ${GID} podcaster && useradd -u ${UID} -g ${GID} -M -s /usr/sbin/nologin podcaster \
    && mkdir /data && chown podcaster:podcaster /data
USER podcaster
VOLUME /data
EXPOSE 8080

# An empty ProxyHandler bypasses http_proxy & co. so localhost is always reached directly.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8080/healthz', timeout=4)"]

CMD ["python", "-m", "app"]
