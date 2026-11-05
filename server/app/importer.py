"""Import assistant for the old Podcaster (config.yml plus downloads directory).

Only reads from the source. Files are placed into ``data/media/<podcast>/`` under
their original names (hard link if possible, otherwise copy). Database rows are
written in one transaction after all files are in place, so a failed import
leaves no episode rows behind and can simply be started again.

The ``cookiefile`` entry of the old config is deliberately never looked at.
"""

import json
import logging
import os
import shutil
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

import yaml

from . import episodes, podcasts, settings
from .db import connect
from .validate import parse_url

log = logging.getLogger(__name__)

MAX_CONFIG_BYTES = 1_000_000
MAX_PROBLEMS = 200
SKIP_INFO = ("meta.info.json", "thumbnail.info.json")
THUMB_EXTS = (".webp", ".jpg", ".jpeg", ".png")


class ImportSourceError(Exception):
    """The source cannot be used at all; the message is meant for the admin."""


@dataclass
class EpisodeSrc:
    video_id: str
    title: str
    description: str
    author: str
    duration_s: int
    published_at: str
    audio: str
    info: str
    thumb: str | None = None
    square: str | None = None
    chapters: str | None = None
    chapters_data: list | None = None  # set when the chapters file has to be generated
    generated_chapters: str | None = None  # its name once generated; the plan itself stays unchanged
    audio_size: int = 0
    size: int = 0  # all files together, for progress


@dataclass
class PodcastPlan:
    id: str
    name: str
    description: str
    url: str
    image_url: str
    author: str
    exists: bool
    source_dir: Path | None
    episodes: list[EpisodeSrc] = field(default_factory=list)
    known_ids: list[str] = field(default_factory=list)  # in archive.txt but without files
    orphans: int = 0  # *.info.json without matching .m4a
    no_square: int = 0
    chapters_to_generate: int = 0

    @property
    def size(self) -> int:
        return sum(e.size for e in self.episodes)


@dataclass
class Plan:
    config_file: str
    downloads_dir: str
    base_url: str
    interval_min: int | None
    podcasts: list[PodcastPlan] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)
    problems_total: int = 0

    def problem(self, text: str) -> None:
        self.problems_total += 1
        if len(self.problems) < MAX_PROBLEMS:
            self.problems.append(text)

    @property
    def episode_count(self) -> int:
        return sum(len(p.episodes) for p in self.podcasts)

    @property
    def known_count(self) -> int:
        return sum(len(p.known_ids) for p in self.podcasts)

    @property
    def total_size(self) -> int:
        return sum(p.size for p in self.podcasts)


def inside(root: Path, user_path: str, *, kind: str) -> Path:
    """Resolve a user-supplied path and require it to be below the import root."""
    raw = (user_path or "").strip()
    if not raw:
        raise ImportSourceError(f"Please enter the {kind} path.")
    root_resolved = root.resolve()
    try:
        path = Path(raw).resolve()
    except (OSError, RuntimeError, ValueError):
        raise ImportSourceError(f"Invalid {kind} path.") from None
    if path != root_resolved and root_resolved not in path.parents:
        raise ImportSourceError(
            f"The {kind} must be below {root_resolved} (the import directory mounted into the container).")
    return path


def build_chapters_json(chapters: list) -> bytes:
    """Podcasting 2.0 chapters file, as the old Podcaster generated it (titles max. 45 characters)."""
    data = {"version": "1.2.0", "chapters": [
        {"startTime": c.get("start_time", 0), "title": t if len(t) <= 45 else t[:44].rstrip() + "…"}
        for c in chapters if isinstance(c, dict) and isinstance(t := c.get("title"), str) and t
    ]}
    return json.dumps(data).encode()


def _load_config(path: Path) -> dict:
    if not path.is_file():
        raise ImportSourceError(f"Config file not found: {path}")
    if path.stat().st_size > MAX_CONFIG_BYTES:
        raise ImportSourceError("The config file is unreasonably large.")
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (yaml.YAMLError, UnicodeDecodeError, OSError):
        raise ImportSourceError("The config file is not valid YAML.") from None
    if not isinstance(cfg, dict) or not isinstance(cfg.get("jobs"), list):
        raise ImportSourceError("The config file has no 'jobs' list.")
    return cfg


def _read_json(path: Path):
    with path.open(encoding="utf-8") as fh:
        return json.load(fh)


def _published(info: dict, audio: Path) -> str:
    for key in ("release_date", "upload_date"):
        raw = info.get(key)
        if isinstance(raw, str):
            try:
                return datetime.strptime(raw, "%Y%m%d").date().isoformat()
            except ValueError:
                pass
    return datetime.fromtimestamp(audio.stat().st_mtime, timezone.utc).date().isoformat()


def _largest_thumbnail(meta: dict) -> str:
    best, url = 0, ""
    for t in meta.get("thumbnails") or []:
        if isinstance(t, dict) and isinstance(t.get("url"), str) and (t.get("width") or 0) > best:
            best, url = t["width"], t["url"]
    return url


def _regular(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()


def _scan_podcast(plan: Plan, pp: PodcastPlan) -> None:
    pdir = pp.source_dir
    names = sorted(os.listdir(pdir))
    present = set(names)
    seen: dict[str, str] = {}
    for name in names:
        if not name.endswith(".info.json") or name in SKIP_INFO:
            continue
        base = name[: -len(".info.json")]
        audio_name = base + ".m4a"
        if audio_name not in present or not _regular(pdir / audio_name):
            pp.orphans += 1
            continue
        try:
            for n in (name, audio_name):
                n.encode("utf-8")
            info = _read_json(pdir / name)
        except (ValueError, OSError):
            plan.problem(f"{pp.id}: cannot read {name!r} (invalid JSON or file name)")
            continue
        vid = info.get("id") if isinstance(info, dict) else None
        if not isinstance(vid, str) or not vid.strip():
            plan.problem(f"{pp.id}: {name!r} has no video id")
            continue
        if vid in seen:
            plan.problem(f"{pp.id}: video {vid} appears twice ({seen[vid]!r} and {name!r}); second one skipped")
            continue
        seen[vid] = name
        thumb = next((base + e for e in THUMB_EXTS if base + e in present and _regular(pdir / (base + e))), None)
        square = base + ".square.jpg" if _regular(pdir / (base + ".square.jpg")) else None
        chapters = base + ".chapters.json" if _regular(pdir / (base + ".chapters.json")) else None
        ep = EpisodeSrc(
            video_id=vid, title=str(info.get("title") or base), description=str(info.get("description") or ""),
            author=str(info.get("channel") or info.get("uploader") or ""),
            duration_s=int(info["duration"]) if isinstance(info.get("duration"), (int, float)) else 0,
            published_at=_published(info, pdir / audio_name), audio=audio_name, info=name, thumb=thumb,
            square=square, chapters=chapters,
        )
        if not chapters and isinstance(info.get("chapters"), list) and info["chapters"]:
            ep.chapters_data = info["chapters"]
            pp.chapters_to_generate += 1
        if not square:
            pp.no_square += 1
        ep.audio_size = (pdir / audio_name).stat().st_size
        ep.size = sum((pdir / n).stat().st_size for n in (audio_name, name, thumb, square, chapters) if n)
        pp.episodes.append(ep)

    archive = pdir / "archive.txt"
    if _regular(archive):
        for line in archive.read_text(encoding="utf-8", errors="replace").splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[-1] not in seen and parts[-1] not in pp.known_ids:
                pp.known_ids.append(parts[-1])


def build_plan(config_file: str, downloads_dir: str, import_root: Path, conn: sqlite3.Connection) -> Plan:
    """Dry run: inspect the source and describe what an import would do. Writes nothing."""
    cfg_path = inside(import_root, config_file, kind="config file")
    downloads = inside(import_root, downloads_dir, kind="downloads directory")
    cfg = _load_config(cfg_path)
    if not downloads.is_dir():
        raise ImportSourceError(f"Downloads directory not found: {downloads}")

    base_url = cfg.get("baseurl") if isinstance(cfg.get("baseurl"), str) else ""
    interval = cfg.get("interval")
    plan = Plan(str(cfg_path), str(downloads), base_url.rstrip("/"),
                interval // 60 if isinstance(interval, int) and interval >= 300 else None)
    existing = {r["id"] for r in podcasts.list_all(conn)}
    used: set[str] = set()

    for job in cfg["jobs"]:
        if not isinstance(job, dict):
            plan.problem("A job entry in config.yml is not a mapping; skipped")
            continue
        pid = str(job.get("id") or "")
        if job.get("type") != "youtube":
            plan.problem(f"{pid or '?'}: type {job.get('type')!r} is not supported; skipped")
            continue
        if not podcasts.SLUG_RE.match(pid) or len(pid) > 64 or pid in podcasts.RESERVED_SLUGS:
            plan.problem(f"{pid!r}: not a valid podcast id (lowercase letters, digits, dashes); skipped")
            continue
        if pid in used:
            plan.problem(f"{pid}: defined twice in config.yml; second one skipped")
            continue
        url, err = parse_url(str(job.get("url") or ""))
        if err:
            plan.problem(f"{pid}: playlist URL {err}; skipped")
            continue
        used.add(pid)
        pdir = downloads / pid
        pp = PodcastPlan(id=pid, name=str(job.get("name") or pid), description=str(job.get("description") or ""),
                         url=url, image_url="", author="", exists=pid in existing,
                         source_dir=pdir if pdir.is_dir() and not pdir.is_symlink() else None)
        image, _ = parse_url(str(job.get("image") or ""), optional=True)
        meta = {}
        if pp.source_dir and _regular(pp.source_dir / "meta.info.json"):
            try:
                loaded = _read_json(pp.source_dir / "meta.info.json")
                meta = loaded if isinstance(loaded, dict) else {}
            except (ValueError, OSError):
                plan.problem(f"{pid}: meta.info.json is unreadable; author and image are not taken over")
        pp.image_url = image or _largest_thumbnail(meta)
        pp.author = str(meta.get("uploader") or "")
        if pp.source_dir:
            _scan_podcast(plan, pp)
        else:
            plan.problem(f"{pid}: no download directory found; the podcast is created without episodes")
        if pp.orphans:
            plan.problem(f"{pid}: {pp.orphans} metadata file(s) without a matching .m4a are ignored")
        plan.podcasts.append(pp)
    if not plan.podcasts:
        raise ImportSourceError("No importable podcasts found in the config file.")
    return plan


class ImportJob:
    """Runs one import in a background thread; the UI polls its state."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.state = "idle"  # idle | running | done | failed
        self.total_bytes = 0
        self.done_bytes = 0
        self.message = ""

    @property
    def running(self) -> bool:
        return self.state == "running"

    def start(self, plan: Plan, db_path: Path, media_dir: Path, *, take_base_url: bool,
              wait: bool = False) -> bool:
        with self.lock:
            if self.running:
                return False
            self.state, self.done_bytes, self.message = "running", 0, ""
            self.total_bytes = sum(e.size for p in plan.podcasts for e in p.episodes)
        thread = threading.Thread(target=self._run, args=(plan, db_path, media_dir, take_base_url), daemon=True)
        thread.start()
        if wait:
            thread.join()
        return True

    def _add(self, n: int) -> None:
        with self.lock:
            self.done_bytes += n

    def _run(self, plan: Plan, db_path: Path, media_dir: Path, take_base_url: bool) -> None:
        try:
            for pp in plan.podcasts:
                target = media_dir / pp.id
                target.mkdir(parents=True, exist_ok=True)
                for ep in pp.episodes:
                    for name in (ep.audio, ep.info, ep.thumb, ep.square, ep.chapters):
                        if name:
                            self._place(pp.source_dir / name, target / name)
                    if ep.chapters_data:
                        ep.generated_chapters = ep.audio[: -len(".m4a")] + ".chapters.json"
                        (target / ep.generated_chapters).write_bytes(build_chapters_json(ep.chapters_data))
            conn = connect(db_path)
            try:
                self._write_db(conn, plan, take_base_url)
            finally:
                conn.close()
            with self.lock:
                self.state = "done"
                self.message = f"Imported {plan.episode_count} episodes and {plan.known_count} known videos."
            log.info("Import finished: %s", self.message)
        except Exception as exc:  # noqa: BLE001 - reported to the admin, details in the log
            log.exception("Import failed")
            with self.lock:
                self.state = "failed"
                self.message = f"Import failed: {exc}. Nothing was written to the database; you can start again."

    def _place(self, src: Path, dst: Path) -> None:
        size = src.stat().st_size
        if dst.is_file() and dst.stat().st_size == size:
            self._add(size)
            return
        tmp = dst.parent / f".import-{uuid.uuid4().hex}"
        try:
            try:
                os.link(src, tmp)
                self._add(size)
            except OSError:  # different file system, read-only mount, ...
                with src.open("rb") as fin, tmp.open("wb") as fout:
                    while chunk := fin.read(1 << 20):
                        fout.write(chunk)
                        self._add(len(chunk))
            os.replace(tmp, dst)
        finally:
            tmp.unlink(missing_ok=True)

    @staticmethod
    def _write_db(conn: sqlite3.Connection, plan: Plan, take_base_url: bool) -> None:
        conn.execute("BEGIN")
        try:
            if episodes.count_all(conn):
                raise RuntimeError("the database already contains episodes")
            for pp in plan.podcasts:
                if not podcasts.get(conn, pp.id):
                    podcasts.create(conn, {"id": pp.id, "name": pp.name, "description": pp.description,
                                           "playlist_url": pp.url, "image_url": pp.image_url, "author": pp.author})
                for ep in pp.episodes:
                    episodes.insert(
                        conn, podcast_id=pp.id, video_id=ep.video_id, title=ep.title, description=ep.description,
                        author=ep.author, audio_file=ep.audio, thumb_file=ep.thumb, square_file=ep.square,
                        chapters_file=ep.chapters or ep.generated_chapters, info_file=ep.info, size_bytes=ep.audio_size,
                        duration_s=ep.duration_s, published_at=ep.published_at, state="available")
                for vid in pp.known_ids:
                    episodes.insert(conn, podcast_id=pp.id, video_id=vid, state="deleted")
            changes = {}
            if plan.interval_min is not None:
                changes["scan_interval_min"] = plan.interval_min
            if take_base_url and plan.base_url:
                changes["base_url"] = plan.base_url
            if changes:
                settings.save(conn, changes)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
