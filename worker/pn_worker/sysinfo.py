"""What the worker can tell about its machine (stdlib only, best effort: a missing value is simply left out)."""

import os
import platform
import shutil
import socket
from pathlib import Path
from urllib.parse import urlsplit

from pn_shared.api import Device

MB = 1 << 20


def _read(path: str | Path) -> str | None:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def os_name(os_release: str = "/etc/os-release") -> str | None:
    """The distribution's pretty name, else the system name."""
    for line in (_read(os_release) or "").splitlines():
        if line.startswith("PRETTY_NAME="):
            return line.split("=", 1)[1].strip().strip('"')[:100] or None
    return platform.system() or None


def meminfo(path: str = "/proc/meminfo") -> tuple[int | None, int | None]:
    """``(total, available)`` in MB from /proc/meminfo."""
    values = {}
    for line in (_read(path) or "").splitlines():
        key, _, rest = line.partition(":")
        parts = rest.split()
        if key in ("MemTotal", "MemAvailable") and parts and parts[0].isdigit():
            values[key] = int(parts[0]) * 1024 // MB
    return values.get("MemTotal"), values.get("MemAvailable")


def cgroup_memory(root: str = "/sys/fs/cgroup") -> tuple[int, int] | None:
    """``(limit, free)`` in MB if the container has a memory limit (cgroup v2), else None."""
    limit, used = _read(f"{root}/memory.max"), _read(f"{root}/memory.current")
    if not limit or not used or not limit.strip().isdigit() or not used.strip().isdigit():
        return None
    return int(limit) // MB, max(0, int(limit) - int(used)) // MB


def cpu_count(root: str = "/sys/fs/cgroup") -> float | None:
    """CPUs available to this process: the container's CPU quota if there is one, else the usable cores."""
    quota = (_read(f"{root}/cpu.max") or "").split()
    if len(quota) == 2 and quota[0].isdigit() and quota[1].isdigit() and int(quota[1]) > 0:
        return round(int(quota[0]) / int(quota[1]), 2)
    try:
        return float(len(os.sched_getaffinity(0)))
    except (AttributeError, OSError):
        return float(os.cpu_count()) if os.cpu_count() else None


def uptime(path: str = "/proc/uptime") -> int | None:
    first = (_read(path) or "").split()
    try:
        return int(float(first[0]))
    except (IndexError, ValueError):
        return None


def local_ip(server_url: str | None) -> str | None:
    """The address of the interface used to reach the server (no packet is sent)."""
    host = urlsplit(server_url or "").hostname
    if not host:
        return None
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.settimeout(2)
            probe.connect((host, 9))
            return probe.getsockname()[0]
    except OSError:
        return None


def collect(work_dir: str, server_url: str | None = None, *, probe=local_ip) -> Device:
    """A snapshot of the machine. Never raises."""
    fields: dict = {}

    def put(key, getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001 - information is optional
            return
        if value is not None:
            fields[key] = value

    put("hostname", lambda: socket.gethostname()[:100])
    put("os", os_name)
    put("kernel", lambda: platform.release()[:100])
    put("arch", lambda: platform.machine()[:30])
    put("cpus", cpu_count)
    total, available = meminfo()
    limited = cgroup_memory()
    if limited and (total is None or limited[0] < total):   # a container limit is what the worker really has
        total, available = limited
    put("mem_total_mb", lambda: total)
    put("mem_available_mb", lambda: available)

    def disk():
        usage = shutil.disk_usage(work_dir if os.path.isdir(work_dir) else "/")
        return usage.total // MB, usage.free // MB
    try:
        fields["disk_total_mb"], fields["disk_free_mb"] = disk()
    except OSError:
        pass
    put("uptime_s", uptime)
    put("load1", lambda: round(os.getloadavg()[0], 2))
    put("local_ip", lambda: probe(server_url))
    return Device(**fields)
