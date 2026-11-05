"""Runs one claimed task: heartbeat in the background, execute, report. Never raises on task errors."""

import logging
import threading
import traceback

from pn_shared.api import FailRequest, ScanResult, TaskInfo

from .client import ApiClient, AuthError, ServerError, TaskLost
from .config import Config
from .scan import ScanFailure, ScanLost, scan_playlist

log = logging.getLogger("pn_worker")


class Heartbeat(threading.Thread):
    """Extends the lease while a task runs; sets ``lost`` if the server took the task away."""

    def __init__(self, client: ApiClient, task_id: int, interval: float, lost: threading.Event, stop: threading.Event):
        super().__init__(daemon=True, name=f"heartbeat-{task_id}")
        self.client, self.task_id, self.interval, self.lost, self.stop_event = client, task_id, interval, lost, stop

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.client.heartbeat(self.task_id)
            except TaskLost:
                log.warning("Task %d: the server no longer lists it as ours", self.task_id)
                self.lost.set()
                return
            except (ServerError, AuthError) as exc:
                log.warning("Task %d: heartbeat failed (%s); will try again", self.task_id, exc)
            except BaseException:  # noqa: BLE001
                log.exception("Task %d: unexpected heartbeat error", self.task_id)


def _report_failure(client: ApiClient, task: TaskInfo, error_class: str, message: str, retry_after: int | None = None) -> None:
    try:
        reply = client.fail(task.id, FailRequest(error_class=error_class, message=message[:4000], retry_after_seconds=retry_after))
        log.info("Task %d failed (%s); the server says: %s", task.id, error_class, reply.state)
    except TaskLost:
        log.warning("Task %d: could not report the failure, it is no longer ours", task.id)
    except (ServerError, AuthError) as exc:
        log.warning("Task %d: could not report the failure (%s); the lease will expire", task.id, exc)


def run_task(client: ApiClient, task: TaskInfo, config: Config) -> None:
    lost, stop = threading.Event(), threading.Event()
    interval = config.heartbeat_interval or max(1.0, min(60.0, task.lease_seconds / 3))
    heartbeat = Heartbeat(client, task.id, interval, lost, stop)
    heartbeat.start()
    log.info("Task %d: %s of %s (attempt %d)", task.id, task.type, task.podcast.id, task.attempt)
    try:
        if task.type != "scan":
            raise ScanFailure("internal", f"this worker cannot run {task.type} tasks yet")
        videos = scan_playlist(task.podcast.playlist_url, task.podcast.scan_depth, timeout=config.scan_timeout,
                               proxy=config.ytdlp_proxy, lost=lost)
        if lost.is_set():
            raise ScanLost()
        reply = client.scan_result(task.id, ScanResult(videos=videos))
        log.info("Task %d done: %d new, %d known", task.id, reply.created, reply.known)
    except ScanFailure as failure:
        _report_failure(client, task, failure.error_class, failure.message, failure.retry_after)
    except (ScanLost, TaskLost):
        log.warning("Task %d: lease lost, result dropped", task.id)
    except (ServerError, AuthError) as exc:
        log.warning("Task %d: could not deliver the result (%s); the lease will expire and the task is retried", task.id, exc)
    except BaseException as exc:  # noqa: BLE001 - a task must never kill the worker
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        log.exception("Task %d: unexpected error", task.id)
        _report_failure(client, task, "internal", "".join(traceback.format_exception_only(type(exc), exc)).strip())
    finally:
        stop.set()
        heartbeat.join(5)
