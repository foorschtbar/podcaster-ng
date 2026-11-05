"""Running external tools as separate processes with a hard time limit."""

import subprocess
import threading
import time

POLL = 0.5


class Failure(Exception):
    """A task failed in a way that is reported to the server with an error class."""

    def __init__(self, error_class: str, message: str, retry_after: int | None = None):
        super().__init__(message)
        self.error_class, self.message, self.retry_after = error_class, message, retry_after


class Lost(Exception):
    """The lease was lost while working; whatever is produced would be discarded anyway."""


def run_process(cmd: list[str], timeout: float, lost: threading.Event, what: str = "The command",
                cwd: str | None = None, env: dict | None = None) -> tuple[int, str, str]:
    """Run ``cmd``; kill it when ``timeout`` passes (Failure 'timeout') or the lease is lost (Lost)."""
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace",
                            stdin=subprocess.DEVNULL, cwd=cwd, env=env)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                out, err = proc.communicate(timeout=POLL)
                return proc.returncode, out, err
            except subprocess.TimeoutExpired:
                if lost.is_set():
                    raise Lost() from None
                if time.monotonic() > deadline:
                    raise Failure("timeout", f"{what} took longer than {timeout:.0f} s and was stopped") from None
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.communicate()
