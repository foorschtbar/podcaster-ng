"""Episode packages: staging, verification and the move into the media directory (no web code).

A package has five possible files (roles): ``audio`` (.m4a) and ``info`` (info.json) are mandatory,
``thumb``, ``square`` and ``chapters`` are optional. Files arrive one by one into a staging directory
``data/staging/<task>-<attempt>/``. ``verify_package`` checks them against the manifest the worker sends
with *complete*; only a complete, verified package is moved into ``data/media/<podcast>/`` (under
server-assigned names), and the episode row is created afterwards, so nothing half-finished is ever
visible in a feed.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from . import filenames

log = logging.getLogger(__name__)

ROLES = ("audio", "info", "thumb", "square", "chapters")
MANDATORY = ("audio", "info")
THUMB_EXTS = {"webp", "jpg", "jpeg", "png"}
SMALL_LIMITS = {"info": 20 << 20, "thumb": 10 << 20, "square": 10 << 20, "chapters": 1 << 20}
STALE_AFTER = 12 * 3600
CHUNK = 1 << 20
_STAGING_NAME = re.compile(r"^(\d+)-(\d+)$")


class PackageError(Exception):
    """The package is not acceptable; the message is meant for the attempt history."""


@dataclass
class Staged:
    role: str
    path: Path
    size: int
    sha256: str
    ext: str = ""


def size_limit(role: str, upload_limit_mb: int) -> int:
    return upload_limit_mb << 20 if role == "audio" else SMALL_LIMITS[role]


def staging_dir_for(staging_root: Path, task_id: int, attempt_id: int) -> Path:
    return staging_root / f"{task_id}-{attempt_id}"


def sniff(role: str, path: Path, ext: str = "") -> None:
    """Cheap content checks, so an obviously wrong file is refused at upload time."""
    with path.open("rb") as fh:
        head = fh.read(16)
    if role == "audio" and head[4:8] != b"ftyp":
        raise PackageError("the audio file is not an MP4/M4A container")
    if role in ("thumb", "square"):
        jpeg, png, webp = head[:3] == b"\xff\xd8\xff", head[:8] == b"\x89PNG\r\n\x1a\n", head[:4] == b"RIFF" and head[8:12] == b"WEBP"
        if not (jpeg or png or webp) or (role == "square" and not jpeg):
            raise PackageError(f"the {role} file is not a valid image")
    if role in ("info", "chapters"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise PackageError(f"the {role} file is not valid JSON") from None
        if not isinstance(data, dict) or (role == "chapters" and not isinstance(data.get("chapters"), list)):
            raise PackageError(f"the {role} file has an unexpected structure")


def store_meta(directory: Path, staged: Staged) -> None:
    (directory / f"{staged.role}.json").write_text(json.dumps({"size": staged.size, "sha256": staged.sha256, "ext": staged.ext}))


def load_staged(directory: Path) -> dict[str, Staged]:
    found = {}
    for role in ROLES:
        data, meta = directory / f"{role}.bin", directory / f"{role}.json"
        if data.is_file() and meta.is_file():
            try:
                m = json.loads(meta.read_text())
                found[role] = Staged(role, data, int(m["size"]), str(m["sha256"]), str(m.get("ext") or ""))
            except (ValueError, KeyError, OSError):
                continue
    return found


def hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            h.update(chunk)
    return h.hexdigest()


def verify_package(directory: Path, manifest: dict[str, tuple[int, str]], video_id: str) -> tuple[dict[str, Staged], dict]:
    """Check the staged files against the manifest and each other. Returns ``(files, parsed info.json)``.

    Hashes are recomputed from disk: what is moved is exactly what the worker declared.
    """
    files = load_staged(directory)
    for role in MANDATORY:
        if role not in files:
            raise PackageError(f"the package has no {'info.json' if role == 'info' else role} file")
    for role, (size, digest) in manifest.items():
        if role not in files:
            raise PackageError(f"the declared {role} file was never uploaded")
        staged = files[role]
        actual = staged.path.stat().st_size
        if actual != size or staged.size != size:
            raise PackageError(f"the {role} file has {actual} bytes, the worker declared {size}")
        if hash_file(staged.path) != digest:
            raise PackageError(f"the {role} file does not match its SHA-256")
    extra = set(files) - set(manifest)
    for role in extra:  # uploaded but not declared: not part of this package
        files.pop(role)
    for role, staged in files.items():
        sniff(role, staged.path, staged.ext)
    info = json.loads(files["info"].path.read_text(encoding="utf-8"))
    if str(info.get("id") or "") != video_id:
        raise PackageError("the info.json belongs to a different video")
    if not str(info.get("title") or "").strip():
        raise PackageError("the info.json has no title")
    return files, info


def published_at(info: dict, today: date | None = None) -> str:
    for key in ("release_date", "upload_date"):
        raw = info.get(key)
        if isinstance(raw, str):
            try:
                return datetime.strptime(raw, "%Y%m%d").date().isoformat()
            except ValueError:
                pass
    ts = info.get("release_timestamp") or info.get("timestamp")
    if isinstance(ts, (int, float)):
        return datetime.fromtimestamp(ts, timezone.utc).date().isoformat()
    return (today or datetime.now(timezone.utc).date()).isoformat()


def plan_names(title: str, video_id: str, files: dict[str, Staged], taken: set[str]) -> dict[str, str]:
    """Final file names per role, derived from the title like the old project did."""
    base = filenames.base_name(title, video_id, taken)
    names = {"audio": f"{base}.m4a", "info": f"{base}.info.json", "chapters": f"{base}.chapters.json",
             "square": f"{base}.square.jpg"}
    if "thumb" in files:
        ext = (files["thumb"].ext or "jpg").lower()
        names["thumb"] = f"{base}.{ext if ext in THUMB_EXTS else 'jpg'}"
    return {role: name for role, name in names.items() if role in files}


def taken_bases(conn: sqlite3.Connection, podcast_id: str, directory: Path) -> set[str]:
    taken = {b for (name,) in conn.execute("SELECT audio_file FROM episodes WHERE podcast_id = ? AND audio_file IS NOT NULL",
                                           (podcast_id,)) if (b := filenames.base_of(name))}
    if directory.is_dir():
        taken |= {b for n in os.listdir(directory) if (b := filenames.base_of(n))}
    return taken


def rollback_move(moved: list[tuple[Path, Path]]) -> None:
    """Put moved files back into staging (so the worker can simply call *complete* again)."""
    for source, dest in reversed(moved):
        try:
            os.replace(dest, source)
        except OSError:
            dest.unlink(missing_ok=True)


def move_into_media(files: dict[str, Staged], names: dict[str, str], target: Path) -> list[tuple[Path, Path]]:
    """Move the staged files into the media directory (atomic rename on the same volume).

    Returns ``(staged path, new path)`` pairs. If anything fails, the files already moved go back to staging.
    """
    target.mkdir(parents=True, exist_ok=True)
    moved: list[tuple[Path, Path]] = []
    try:
        for role in ("info", "chapters", "thumb", "square", "audio"):  # the audio file last
            if role in files:
                dest = target / names[role]
                if dest.exists():
                    raise PackageError(f"{names[role]!r} already exists")
                os.replace(files[role].path, dest)
                moved.append((files[role].path, dest))
    except BaseException:
        rollback_move(moved)
        raise
    return moved


def episode_fields(info: dict, names: dict[str, str], files: dict[str, Staged]) -> dict:
    duration = info.get("duration")
    return {
        "title": str(info["title"]).strip(), "description": str(info.get("description") or ""),
        "author": str(info.get("channel") or info.get("uploader") or ""),
        "audio_file": names["audio"], "info_file": names["info"], "thumb_file": names.get("thumb"),
        "square_file": names.get("square"), "chapters_file": names.get("chapters"),
        "size_bytes": files["audio"].size, "duration_s": int(duration) if isinstance(duration, (int, float)) else 0,
        "published_at": published_at(info),
    }


def remove(directory: Path) -> None:
    shutil.rmtree(directory, ignore_errors=True)


def _last_activity(directory: Path) -> float:
    latest = directory.stat().st_mtime
    for child in directory.iterdir():
        try:
            latest = max(latest, child.stat().st_mtime)
        except OSError:
            pass
    return latest


def cleanup_staging(conn: sqlite3.Connection, staging_root: Path, now: float | None = None) -> int:
    """Remove staging directories of finished or abandoned attempts, and anything untouched for 12 hours."""
    if not staging_root.is_dir():
        return 0
    now = now or time.time()
    removed = 0
    for entry in staging_root.iterdir():
        if not entry.is_dir() and not entry.is_symlink():
            continue
        m = _STAGING_NAME.match(entry.name)
        keep = False
        if m and not entry.is_symlink():
            row = conn.execute("SELECT result FROM attempts WHERE id = ? AND task_id = ?", (int(m.group(2)), int(m.group(1)))).fetchone()
            keep = bool(row) and row["result"] is None and now - _last_activity(entry) < STALE_AFTER
        if not keep:
            if entry.is_symlink():
                entry.unlink()
            else:
                shutil.rmtree(entry, ignore_errors=True)
            removed += 1
    if removed:
        log.info("Removed %d stale staging director%s", removed, "y" if removed == 1 else "ies")
    return removed
