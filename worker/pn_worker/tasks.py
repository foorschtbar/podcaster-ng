"""Runs one claimed task: heartbeat in the background, execute, report. Never raises on task errors."""

import dataclasses
import logging
import threading
import time
import traceback

from pn_shared.api import FailRequest, ScanResult, TaskInfo

from .client import ApiClient, AuthError, ServerError, TaskLost
from .config import Config
from .download import Progress, deliver, run_download
from .process import Failure, Lost
from .scan import scan_playlist

log = logging.getLogger("pn_worker")


class Heartbeat(threading.Thread):
    """Extends the lease while a task runs; sets ``lost`` if the server took the task away."""

    def __init__(self, client: ApiClient, task_id: int, interval: float, lost: threading.Event, stop: threading.Event,
                 progress: Progress | None = None):
        super().__init__(daemon=True, name=f"heartbeat-{task_id}")
        self.client, self.task_id, self.interval, self.lost, self.stop_event = client, task_id, interval, lost, stop
        self.progress = progress

    def run(self) -> None:
        while not self.stop_event.wait(self.interval):
            try:
                self.client.heartbeat(self.task_id, self.progress.text if self.progress and self.progress.text else None)
            except TaskLost:
                log.warning("Task %d: the server no longer lists it as ours", self.task_id)
                self.lost.set()
                return
            except (ServerError, AuthError) as exc:
                log.warning("Task %d: heartbeat failed (%s); will try again", self.task_id, exc)
            except BaseException:  # noqa: BLE001
                log.exception("Task %d: unexpected heartbeat error", self.task_id)


def _report_failure(client: ApiClient, task: TaskInfo, error_class: str, message: str, retry_after: int | None = None,
                    config: Config | None = None, lost: threading.Event | None = None, pause=time.sleep) -> bool:
    """Tell the server the task failed; an unreachable server (a restart) is waited out so it learns the result."""
    request = FailRequest(error_class=error_class, message=message[:4000], retry_after_seconds=retry_after)
    try:
        # the lease is probably gone by the time a long outage ends, so the report does not wait as long as an upload
        config = dataclasses.replace(config, delivery_timeout=min(config.delivery_timeout, 120.0)) if config else Config("http://x", "x")
        reply = deliver(lambda: client.fail(task.id, request), lost or threading.Event(), config,
                        f"Task {task.id}: reporting the failure", pause)
        log.info("Task %d failed (%s); the server says: %s", task.id, error_class, reply.state)
        return True
    except (Failure, Lost):
        log.warning("Task %d: could not report the failure, the server stayed unreachable; the server is told when "
                    "it is back", task.id)
        return False
    except TaskLost:
        log.warning("Task %d: could not report the failure, it is no longer ours", task.id)
        return True
    except (ServerError, AuthError) as exc:
        log.warning("Task %d: could not report the failure (%s); the server is told when it is back", task.id, exc)
        return False


def run_task(client: ApiClient, task: TaskInfo, config: Config) -> bool:
    """Run the task. Returns False if the result could not be delivered (the server was unreachable), so the caller can
    tell the server later that the task was dropped."""
    delivered = True
    lost, stop, progress = threading.Event(), threading.Event(), Progress()
    interval = config.heartbeat_interval or max(1.0, min(60.0, task.lease_seconds / 3))
    heartbeat = Heartbeat(client, task.id, interval, lost, stop, progress)
    heartbeat.start()
    log.info("Task %d: %s of %s (attempt %d)", task.id, task.type, task.podcast.id, task.attempt)
    try:
        if task.type == "scan":
            videos = scan_playlist(task.podcast.playlist_url, task.podcast.scan_depth, timeout=config.scan_timeout,
                                   proxy=config.ytdlp_proxy, lost=lost)
            if lost.is_set():
                raise Lost()
            reply = client.scan_result(task.id, ScanResult(videos=videos))
            log.info("Task %d done: %d new, %d known", task.id, reply.created, reply.known)
        elif task.type == "download" and task.video_id:
            run_download(client, task, config, lost, progress)
            if lost.is_set():
                raise Lost()
        else:
            raise Failure("internal", f"this worker cannot run {task.type} tasks")
    except Failure as failure:
        delivered = _report_failure(client, task, failure.error_class, failure.message, failure.retry_after, config, lost)
    except (Lost, TaskLost):
        log.warning("Task %d: lease lost, result dropped", task.id)
    except (ServerError, AuthError) as exc:
        delivered = False
        log.warning("Task %d: could not deliver the result (%s); the server is told at the next call", task.id, exc)
    except BaseException as exc:  # noqa: BLE001 - a task must never kill the worker
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        log.exception("Task %d: unexpected error", task.id)
        delivered = _report_failure(client, task, "internal", "".join(traceback.format_exception_only(type(exc), exc)).strip(), None, config, lost)
    finally:
        stop.set()
        heartbeat.join(5)
    return delivered
