import os
import time
from pathlib import Path

import pytest

from app import packages
from app.db import connect
from app.migrations import migrate

from pkghelpers import JPEG, M4A, PNG, WEBP, info, manifest, package, sha


def stage(directory: Path, files: dict, skip=()):
    directory.mkdir(parents=True, exist_ok=True)
    for role, (data, ext) in files.items():
        if role in skip:
            continue
        path = directory / f"{role}.bin"
        path.write_bytes(data)
        packages.store_meta(directory, packages.Staged(role, path, len(data), sha(data), ext))


def man(files):
    return {r: (len(d), sha(d)) for r, (d, _) in files.items()}


# --- sniffing ------------------------------------------------------------------------------------

@pytest.mark.parametrize("role,data,ok", [
    ("audio", M4A, True), ("audio", b"RIFF....WAVEfmt ", False), ("audio", b"short", False), ("audio", b"", False),
    ("thumb", WEBP, True), ("thumb", JPEG, True), ("thumb", PNG, True), ("thumb", b"GIF89a....", False), ("thumb", M4A, False),
    ("square", JPEG, True), ("square", PNG, False), ("square", WEBP, False),
    ("info", b'{"id": "x"}', True), ("info", b"[1]", False), ("info", b"not json", False), ("info", b"\xff\xfe", False),
    ("chapters", b'{"chapters": []}', True), ("chapters", b'{"chapters": 5}', False), ("chapters", b"{}", False),
])
def test_sniff(tmp_path, role, data, ok):
    path = tmp_path / "f"
    path.write_bytes(data)
    if ok:
        packages.sniff(role, path)
    else:
        with pytest.raises(packages.PackageError):
            packages.sniff(role, path)


# --- verification --------------------------------------------------------------------------------------

def test_valid_package(tmp_path):
    files = package()
    stage(tmp_path / "s", files)
    staged, parsed = packages.verify_package(tmp_path / "s", man(files), "aaaaaaaaaaa")
    assert set(staged) == set(files) and parsed["title"].startswith("Group Therapy")


def test_minimal_package_needs_only_audio_and_info(tmp_path):
    files = package(thumb=False, square=False, chapters=False)
    stage(tmp_path / "s", files)
    assert set(packages.verify_package(tmp_path / "s", man(files), "aaaaaaaaaaa")[0]) == {"audio", "info"}


@pytest.mark.parametrize("missing,message", [("info", "info.json"), ("audio", "audio")])
def test_mandatory_files(tmp_path, missing, message):
    files = package()
    stage(tmp_path / "s", files, skip=(missing,))
    with pytest.raises(packages.PackageError, match=message):
        packages.verify_package(tmp_path / "s", {r: v for r, v in man(files).items() if r != missing}, "aaaaaaaaaaa")


def test_declared_but_never_uploaded(tmp_path):
    files = package()
    stage(tmp_path / "s", files, skip=("square",))
    with pytest.raises(packages.PackageError, match="never uploaded"):
        packages.verify_package(tmp_path / "s", man(files), "aaaaaaaaaaa")


def test_hash_mismatch_is_detected_even_if_the_staged_file_changed_after_upload(tmp_path):
    files = package()
    stage(tmp_path / "s", files)
    (tmp_path / "s" / "audio.bin").write_bytes(M4A[:-1] + b"X")                 # same size, other content
    with pytest.raises(packages.PackageError, match="SHA-256"):
        packages.verify_package(tmp_path / "s", man(files), "aaaaaaaaaaa")


def test_size_mismatch(tmp_path):
    files = package()
    stage(tmp_path / "s", files)
    wrong = {**man(files), "audio": (len(M4A) + 1, sha(M4A))}
    with pytest.raises(packages.PackageError, match="bytes"):
        packages.verify_package(tmp_path / "s", wrong, "aaaaaaaaaaa")


def test_info_of_another_video_or_without_title(tmp_path):
    files = package(video_id="bbbbbbbbbbb")
    stage(tmp_path / "s", files)
    with pytest.raises(packages.PackageError, match="different video"):
        packages.verify_package(tmp_path / "s", man(files), "aaaaaaaaaaa")
    files = package(title="  ")
    stage(tmp_path / "t", files)
    with pytest.raises(packages.PackageError, match="no title"):
        packages.verify_package(tmp_path / "t", man(files), "aaaaaaaaaaa")


def test_undeclared_files_are_left_out(tmp_path):
    files = package()
    stage(tmp_path / "s", files)
    declared = {r: v for r, v in man(files).items() if r != "thumb"}
    staged, _ = packages.verify_package(tmp_path / "s", declared, "aaaaaaaaaaa")
    assert "thumb" not in staged


def test_content_is_checked_at_complete_too(tmp_path):
    files = package()
    files["audio"] = (b"not an audio file at all, just text", "")
    stage(tmp_path / "s", files)
    with pytest.raises(packages.PackageError, match="MP4"):
        packages.verify_package(tmp_path / "s", man(files), "aaaaaaaaaaa")


# --- names, dates ---------------------------------------------------------------------------------------

def test_plan_names(tmp_path):
    files = package()
    stage(tmp_path / "s", files)
    staged = packages.load_staged(tmp_path / "s")
    names = packages.plan_names("Group Therapy 701: Live & Direct", "aaaaaaaaaaa", staged, set())
    base = "Group Therapy 701： Live & Direct"
    assert names == {"audio": f"{base}.m4a", "info": f"{base}.info.json", "chapters": f"{base}.chapters.json",
                     "square": f"{base}.square.jpg", "thumb": f"{base}.webp"}
    assert packages.plan_names("T", "id", {"audio": staged["audio"]}, set()) == {"audio": "T.m4a"}
    evil = packages.Staged("thumb", Path("x"), 1, "h", "php")
    assert packages.plan_names("T", "id", {"thumb": evil}, set())["thumb"] == "T.jpg"   # unknown extensions never survive


def test_published_at_rules():
    assert packages.published_at({"release_date": "20260105", "upload_date": "20250101"}) == "2026-01-05"
    assert packages.published_at({"upload_date": "20250101"}) == "2025-01-01"
    assert packages.published_at({"release_date": "garbage", "upload_date": "20250102"}) == "2025-01-02"
    assert packages.published_at({"timestamp": 1767571200}).startswith("2026-01-0")
    from datetime import date
    assert packages.published_at({}, date(2026, 2, 3)) == "2026-02-03"


def test_taken_bases_from_database_and_directory(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    conn = connect(tmp_path / "p.db")
    from app import episodes, podcasts
    podcasts.create(conn, {"id": "abgt", "name": "A", "playlist_url": "https://x.example/p"})
    episodes.insert(conn, podcast_id="abgt", video_id="v1", audio_file="From DB.m4a")
    episodes.insert(conn, podcast_id="abgt", video_id="v2", state="deleted")
    media = tmp_path / "media"
    media.mkdir()
    (media / "On Disk.m4a").write_bytes(b"x")
    (media / "On Disk.info.json").write_bytes(b"{}")
    assert packages.taken_bases(conn, "abgt", media) == {"From DB", "On Disk"}
    conn.close()


# --- moving into the media directory ------------------------------------------------------------------------

def test_move_is_complete_and_audio_comes_last(tmp_path, monkeypatch):
    files = package()
    stage(tmp_path / "s", files)
    staged = packages.load_staged(tmp_path / "s")
    names = packages.plan_names("Title", "aaaaaaaaaaa", staged, set())
    order = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda a, b: (order.append(Path(b).name), real(a, b))[1])
    moved = packages.move_into_media(staged, names, tmp_path / "media" / "abgt")
    assert order[-1] == "Title.m4a" and len(moved) == 5 and moved[-1][1].name == "Title.m4a"
    assert sorted(p.name for p in (tmp_path / "media" / "abgt").iterdir()) == sorted(names.values())
    assert not any((tmp_path / "s").glob("*.bin"))                         # moved, not copied


def test_move_failure_removes_what_was_already_moved(tmp_path, monkeypatch):
    files = package()
    stage(tmp_path / "s", files)
    staged = packages.load_staged(tmp_path / "s")
    names = packages.plan_names("Title", "aaaaaaaaaaa", staged, set())
    real, calls = os.replace, {"n": 0}

    def flaky(a, b):
        calls["n"] += 1
        if calls["n"] == 4:
            raise OSError("disk full")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        packages.move_into_media(staged, names, tmp_path / "media" / "abgt")
    assert list((tmp_path / "media" / "abgt").iterdir()) == []             # nothing half-published
    assert len(list((tmp_path / "s").glob("*.bin"))) == 5                  # ... and the package is intact in staging again
    monkeypatch.setattr(os, "replace", real)
    assert len(packages.move_into_media(packages.load_staged(tmp_path / "s"), names, tmp_path / "media" / "abgt")) == 5


def test_move_refuses_to_overwrite(tmp_path):
    files = package()
    stage(tmp_path / "s", files)
    staged = packages.load_staged(tmp_path / "s")
    names = packages.plan_names("Title", "aaaaaaaaaaa", staged, set())
    target = tmp_path / "media" / "abgt"
    target.mkdir(parents=True)
    (target / "Title.info.json").write_text("someone else's")
    with pytest.raises(packages.PackageError, match="already exists"):
        packages.move_into_media(staged, names, target)
    assert (target / "Title.info.json").read_text() == "someone else's" and sorted(p.name for p in target.iterdir()) == ["Title.info.json"]


# --- staging cleanup ---------------------------------------------------------------------------------------------

def test_cleanup_removes_stale_and_finished_staging(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    conn = connect(tmp_path / "p.db")
    from app import podcasts
    podcasts.create(conn, {"id": "abgt", "name": "A", "playlist_url": "https://x.example/p"})
    now = "2026-11-05T12:00:00Z"
    conn.execute("INSERT INTO tasks (id, type, podcast_id, video_id, state, created_at, updated_at) VALUES (1, 'download', 'abgt', 'v1', 'claimed', ?, ?)", (now, now))
    conn.execute("INSERT INTO tasks (id, type, podcast_id, video_id, state, created_at, updated_at) VALUES (2, 'download', 'abgt', 'v2', 'pending', ?, ?)", (now, now))
    conn.execute("INSERT INTO attempts (id, task_id, started_at) VALUES (10, 1, ?)", (now,))                      # running
    conn.execute("INSERT INTO attempts (id, task_id, started_at, ended_at, result) VALUES (11, 2, ?, ?, 'failed')", (now, now))
    root = tmp_path / "staging"
    for name in ("1-10", "2-11", "9-99", "garbage"):
        (root / name).mkdir(parents=True)
        (root / name / "audio.part").write_bytes(b"x")
    (root / "stray-file").write_text("x")
    (tmp_path / "elsewhere").mkdir()
    (root / "link").symlink_to(tmp_path / "elsewhere")
    assert packages.cleanup_staging(conn, root) == 4            # 2-11 (finished), 9-99 and garbage (unknown), the symlink
    left = sorted(p.name for p in root.iterdir())
    assert left == ["1-10", "stray-file"]                       # only the running attempt's directory survives
    assert (tmp_path / "elsewhere").exists()                    # a symlink is removed, never followed
    old = time.time() - 13 * 3600                               # 13 hours without activity: abandoned even if "running"
    for p in [root / "1-10", *(root / "1-10").iterdir()]:
        os.utime(p, (old, old))
    packages.cleanup_staging(conn, root)
    assert sorted(p.name for p in root.iterdir()) == ["stray-file"]
    assert packages.cleanup_staging(conn, tmp_path / "does-not-exist") == 0
    conn.close()
