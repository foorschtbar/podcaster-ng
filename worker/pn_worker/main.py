"""Worker main loop: say hello, then ping periodically; survive server downtime with backoff."""

import logging
import os
import random
import threading
import time

from pn_shared.api import ClaimRequest, HelloRequest

from . import __version__, tools
from .client import ApiClient, AuthError, ServerError
from .config import Config
from .tasks import run_task

log = logging.getLogger("pn_worker")
AUTH_RETRY_SECONDS = 60.0  # a rejected token may be fixed in the admin UI at any time


def backoff_delays(start: float, maximum: float, rng: random.Random | None = None):
    """Exponentially growing delays with +-20 % jitter, capped at ``maximum``."""
    rng = rng or random.Random()
    delay = start
    while True:
        yield min(maximum, delay) * rng.uniform(0.8, 1.2)
        delay = min(maximum, delay * 2)


def supported_types(found: dict[str, str]) -> list[str]:
    """Task types this worker can execute with the tools it has. Downloads arrive in a later step."""
    return ["scan"] if "ytdlp" in found else []


def hello_request(found: dict[str, str] | None = None) -> HelloRequest:
    found = tools.detect() if found is None else found
    return HelloRequest(
        worker_version=__version__, ytdlp_version=found.get("ytdlp"), ffmpeg_version=found.get("ffmpeg"),
        deno_version=found.get("deno"), capabilities=sorted([*found, *supported_types(found)]))


def _touch(path: str) -> None:
    try:
        with open(path, "a"):
            os.utime(path, None)
    except OSError:
        pass


def idle_delay(hint: float, idle_count: int, maximum: float = 60.0) -> float:
    """Wait after an empty claim: the server's hint, growing by half per empty answer (up to 3 times)."""
    return min(maximum, hint * 1.5 ** min(idle_count, 3))


def run(config: Config, stop: threading.Event, client: ApiClient | None = None, sleep=None, found: dict | None = None) -> None:
    """Run until ``stop`` is set. Never raises on network or server problems."""
    client = client or ApiClient(config.server_url, config.token)
    wait = sleep or stop.wait
    delays = backoff_delays(config.backoff_start, config.backoff_max)
    connected = False
    interval = config.ping_interval or 30.0
    found = tools.detect() if found is None else found
    types = supported_types(found)
    idle = 0
    log.info("Worker %s starting, server %s", __version__, config.server_url)
    while not stop.is_set():
        _touch(config.alive_file)  # the container health check looks at this file
        try:
            if not connected:
                reply = client.hello(hello_request(found))
                connected = True
                idle = 0
                interval = config.ping_interval or float(reply.ping_interval_seconds)
                delays = backoff_delays(config.backoff_start, config.backoff_max)
                skew = abs(time.time() - _epoch(reply.server_time))
                log.info("Connected as %r (tier %d), pinging every %.0f s", reply.worker.name, reply.worker.tier, interval)
                if skew > 300:
                    log.warning("Clock differs from the server's by %.0f s", skew)
                wait(interval if not types else 0)
            elif types:
                answer = client.claim(ClaimRequest(types=types))   # also counts as "seen"
                if answer.task:
                    idle = 0
                    run_task(client, answer.task, config)
                else:
                    wait(config.poll_interval or idle_delay(answer.retry_after_seconds, idle))
                    idle += 1
            else:
                client.ping()
                wait(interval)
        except AuthError as exc:
            connected = False
            log.error("%s; retrying in %.0f s", exc, AUTH_RETRY_SECONDS)
            wait(AUTH_RETRY_SECONDS)
        except ServerError as exc:
            if connected:
                log.warning("Lost the connection to the server: %s", exc)
            connected = False
            delay = next(delays)
            log.warning("Server not reachable (%s); retrying in %.1f s", exc, delay)
            wait(delay)
        except BaseException as exc:  # noqa: BLE001 - the loop must survive everything but a stop request
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            connected = False
            log.exception("Unexpected error in the main loop")
            wait(next(delays))
    client.close()
    log.info("Worker stopped")


def _epoch(iso: str) -> float:
    from datetime import datetime, timezone
    try:
        return datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return time.time()
