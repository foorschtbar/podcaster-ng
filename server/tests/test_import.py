import hashlib
import json
import re
import time
from pathlib import Path

import pytest

from app import episodes, importer, podcasts, settings
from app.db import connect
from app.importer import ImportJob, ImportSourceError, build_plan
from app.migrations import migrate

from fixtures import ABGT_1, ASOT_1, build_old_project
from helpers import csrf_of, do_login, make_client, make_config


@pytest.fixture
def old(tmp_path):
    root = tmp_path / "import"
    config, downloads = build_old_project(root / "podcaster")
    return root, config, downloads


@pytest.fixture
def conn(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    c = connect(tmp_path / "p.db")
    yield c
    c.close()


def snapshot(root: Path) -> dict:
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest(), p.stat().st_mtime_ns)
            for p in sorted(root.rglob("*")) if p.is_file()}


def run(plan, tmp_path, db="p.db", **kw):
    job = ImportJob()
    job.start(plan, tmp_path / db, tmp_path / "media", take_base_url=kw.get("take_base_url", False), wait=True)
    return job


# --- plan (dry run) ---------------------------------------------------------

def test_plan_summary(old, conn):
    root, config, downloads = old
    plan = build_plan(str(config), str(downloads), root, conn)
    assert [p.id for p in plan.podcasts] == ["abgt", "asot"]
    assert plan.base_url == "https://old.example.org" and plan.interval_min == 60
    abgt, asot = plan.podcasts
    assert (abgt.name, abgt.description, abgt.author) == ("ABGT", "Above & Beyond", "Anjunabeats")
    assert abgt.image_url == "https://img.example.org/big.jpg?a=1&b=2"  # largest thumbnail from meta.info.json
    assert asot.image_url == "https://img.example.org/asot.jpg"          # explicit config image wins
    assert len(abgt.episodes) == 2 and abgt.orphans == 1
    assert sorted(abgt.known_ids) == ["gone1", "orphan1"]  # in archive.txt, but no file
    assert plan.episode_count == 3 and plan.known_count == 2
    assert abgt.no_square == 1 and abgt.chapters_to_generate == 1
    problems = " ".join(plan.problems)
    assert "mix" in problems and "Bad_ID" in problems and "without a matching .m4a" in problems
    assert not asot.exists


def test_plan_episode_details(old, conn):
    root, config, downloads = old
    abgt = build_plan(str(config), str(downloads), root, conn).podcasts[0]
    e = next(e for e in abgt.episodes if e.video_id == "vid100")
    assert (e.title, e.author, e.duration_s, e.published_at) == ("ABGT 100 - Live & Direct", "Above & Beyond", 3600, "2024-01-05")
    assert (e.audio, e.thumb, e.square) == ("ABGT 100 - Live & Direct.m4a", "ABGT 100 - Live & Direct.webp",
                                            "ABGT 100 - Live & Direct.square.jpg")
    assert e.audio_size == 1000
    e2 = next(e for e in abgt.episodes if e.video_id == "vid101")
    assert e2.published_at == "2024-01-12"  # upload_date fallback
    assert e2.thumb.endswith(".jpg") and e2.square is None and e2.chapters.endswith(".chapters.json")


def test_plan_writes_nothing_and_never_touches_source(old, conn, tmp_path):
    root, config, downloads = old
    before = snapshot(root)
    build_plan(str(config), str(downloads), root, conn)
    assert snapshot(root) == before
    assert not (tmp_path / "media").exists() and episodes.count_all(conn) == 0


def test_existing_podcast_is_reported(old, conn):
    root, config, downloads = old
    podcasts.create(conn, {"id": "abgt", "name": "Mine", "playlist_url": "https://x.example/p"})
    plan = build_plan(str(config), str(downloads), root, conn)
    assert plan.podcasts[0].exists and not plan.podcasts[1].exists


@pytest.mark.parametrize("config_file,downloads", [
    ("/etc/passwd", "{dl}"), ("{cfg}", "/etc"), ("{root}/../outside.yml", "{dl}"),
    ("", "{dl}"), ("{cfg}", ""), ("{root}/missing.yml", "{dl}"), ("{cfg}", "{root}/missing-dir"),
])
def test_bad_paths_rejected(old, conn, config_file, downloads):
    root, config, dl = old
    fmt = lambda s: s.format(root=root, cfg=config, dl=dl)  # noqa: E731
    with pytest.raises(ImportSourceError):
        build_plan(fmt(config_file), fmt(downloads), root, conn)


def test_symlink_escape_rejected(old, conn, tmp_path):
    root, config, downloads = old
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "config.yml").write_text("jobs: []")
    (root / "link.yml").symlink_to(outside / "config.yml")
    with pytest.raises(ImportSourceError, match="below"):
        build_plan(str(root / "link.yml"), str(downloads), root, conn)


@pytest.mark.parametrize("content", ["", "just text", "- a\n- b", "jobs: notalist", "a: [unclosed", "jobs: []"])
def test_unusable_config_rejected(old, conn, content):
    root, config, downloads = old
    config.write_text(content)
    with pytest.raises(ImportSourceError):
        build_plan(str(config), str(downloads), root, conn)


def test_python_yaml_tags_are_not_executed(old, conn):
    root, config, downloads = old
    config.write_text("jobs: !!python/object/apply:os.system ['echo pwned']")
    with pytest.raises(ImportSourceError, match="valid YAML"):
        build_plan(str(config), str(downloads), root, conn)


def test_cookie_file_is_ignored(old, conn):
    root, config, downloads = old
    plan = build_plan(str(config), str(downloads), root, conn)
    assert "SECRET-COOKIE-VALUE" not in repr(plan)


def test_broken_episode_files_become_problems(old, conn):
    root, config, downloads = old
    (downloads / "abgt" / "Bad.info.json").write_text("{not json")
    (downloads / "abgt" / "Bad.m4a").write_bytes(b"x")
    (downloads / "abgt" / "NoId.info.json").write_text(json.dumps({"title": "x"}))
    (downloads / "abgt" / "NoId.m4a").write_bytes(b"x")
    (downloads / "abgt" / "Dup.info.json").write_text(json.dumps({"id": "vid100", "title": "dup"}))
    (downloads / "abgt" / "Dup.m4a").write_bytes(b"x")
    plan = build_plan(str(config), str(downloads), root, conn)
    text = " ".join(plan.problems)
    assert "Bad.info.json" in text and "no video id" in text and "appears twice" in text
    assert len(plan.podcasts[0].episodes) == 2


# --- import -----------------------------------------------------------------

def test_import_creates_everything(old, conn, tmp_path):
    root, config, downloads = old
    before = snapshot(root)
    plan = build_plan(str(config), str(downloads), root, conn)
    job = run(plan, tmp_path, take_base_url=True)
    assert job.state == "done", job.message
    assert job.done_bytes == job.total_bytes > 0

    assert {p["id"] for p in podcasts.list_all(conn)} == {"abgt", "asot"}
    abgt = podcasts.get(conn, "abgt")
    assert (abgt["author"], abgt["playlist_url"]) == ("Anjunabeats", "https://www.youtube.com/playlist?list=PL1")
    rows = {r["video_id"]: r for r in conn.execute("SELECT * FROM episodes WHERE podcast_id='abgt'")}
    assert set(rows) == {"vid100", "vid101", "gone1", "orphan1"}  # the last two are known-only
    assert rows["vid100"]["state"] == "available" and rows["vid100"]["size_bytes"] == 1000
    assert rows["gone1"]["state"] == "deleted" and rows["gone1"]["audio_file"] is None
    assert settings.get(conn, "base_url") == "https://old.example.org"
    assert settings.get(conn, "scan_interval_min") == 60

    # files are in place with unchanged names; chapters were generated where missing
    media = tmp_path / "media" / "abgt"
    for name in ("ABGT 100 - Live & Direct.m4a", "ABGT 100 - Live & Direct.webp", "ABGT 100 - Live & Direct.square.jpg",
                 "ABGT 100 - Live & Direct.info.json", "ABGT 101.m4a", "ABGT 101.chapters.json"):
        assert (media / name).is_file(), name
    assert (media / "ABGT 100 - Live & Direct.m4a").read_bytes() == b"A" * 1000
    chapters = json.loads((media / "ABGT 100 - Live & Direct.chapters.json").read_text())
    assert chapters["version"] == "1.2.0" and chapters["chapters"][0] == {"startTime": 0, "title": "Intro"}
    assert chapters["chapters"][1]["title"] == "x" * 44 + "…"  # titles are limited to 45 characters
    assert rows["vid100"]["chapters_file"] == "ABGT 100 - Live & Direct.chapters.json"
    assert not [p for p in media.iterdir() if p.name.startswith(".import-")]  # no temp files left
    assert (tmp_path / "media" / "asot" / ASOT_1.replace(".info.json", ".m4a")).is_file()

    assert snapshot(root) == before  # the old project was not modified in any way


def test_base_url_only_taken_when_asked(old, conn, tmp_path):
    root, config, downloads = old
    plan = build_plan(str(config), str(downloads), root, conn)
    assert run(plan, tmp_path, take_base_url=False).state == "done"
    assert settings.get(conn, "base_url") == ""


def test_import_is_blocked_when_episodes_exist(old, conn, tmp_path):
    root, config, downloads = old
    plan = build_plan(str(config), str(downloads), root, conn)
    assert run(plan, tmp_path).state == "done"
    job = run(build_plan(str(config), str(downloads), root, conn), tmp_path)
    assert job.state == "failed" and "already contains episodes" in job.message
    assert episodes.count_all(conn) == 5


def test_failed_import_leaves_no_rows_and_can_be_repeated(old, conn, tmp_path, monkeypatch):
    root, config, downloads = old
    plan = build_plan(str(config), str(downloads), root, conn)
    calls = {"n": 0}
    real = ImportJob._place

    def flaky(self, src, dst):
        calls["n"] += 1
        if calls["n"] == 5:
            raise OSError("disk full")
        return real(self, src, dst)

    monkeypatch.setattr(ImportJob, "_place", flaky)
    job = run(plan, tmp_path)
    assert job.state == "failed" and "disk full" in job.message
    assert episodes.count_all(conn) == 0 and podcasts.list_all(conn) == []
    monkeypatch.setattr(ImportJob, "_place", real)
    assert run(plan, tmp_path).state == "done"
    assert episodes.count_all(conn) == 5


def test_only_one_import_at_a_time(old, conn, tmp_path):
    root, config, downloads = old
    plan = build_plan(str(config), str(downloads), root, conn)
    job = ImportJob()
    job.state = "running"
    assert job.start(plan, tmp_path / "p.db", tmp_path / "media", take_base_url=False) is False


def test_import_then_feed_roundtrip(old, tmp_path):
    root, config, downloads = old
    with make_client(tmp_path, import_dir=root) as c:
        conn = connect(c.app.state.config.db_path)
        plan = build_plan(str(config), str(downloads), root, conn)
        conn.close()
        assert run(plan, tmp_path, db="podcaster.db").state == "done"
        xml = c.get("/abgt.xml").text
        assert "https://testserver/files/abgt/ABGT%20100%20-%20Live%20%26%20Direct.m4a" in xml
        assert c.get("/files/abgt/ABGT%20101.m4a").content == b"B" * 2000
        assert c.get("/files/asot/ASOT%201%20%23%3F%25%20test.m4a").status_code == 200
        assert "gone1" not in xml and "orphan1" not in xml  # known-only videos stay out of the feed


# --- UI ---------------------------------------------------------------------

def wait_done(c, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        if not c.app.state.import_job.running:
            return
        time.sleep(0.05)
    raise AssertionError("import did not finish")


def test_ui_flow_preview_then_run(old, tmp_path):
    root, config, downloads = old
    with make_client(tmp_path, import_dir=root) as c:
        do_login(c)
        page = c.get("/settings/import").text
        assert "Check (dry run)" in page and "/import/podcaster/config.yml" in page
        assert "/settings/import" in c.get("/settings").text

        form = {"csrf_token": csrf_of(c), "config_file": str(config), "downloads_dir": str(downloads)}
        r = c.post("/settings/import/preview", data=form)
        assert r.status_code == 200
        assert "Dry run" in r.text and "<strong>2</strong> podcast(s)" in r.text and "<strong>3</strong> episode(s)" in r.text
        assert "Problems" in r.text and "Import now" in r.text
        assert 'name="take_base_url" value="1">' in r.text  # never pre-selected
        assert episodes.count_all(connect(c.app.state.config.db_path)) == 0  # dry run wrote nothing

        r = c.post("/settings/import/run", data={**form, "take_base_url": "1"})
        assert (r.status_code, r.headers["location"]) == (303, "/settings/import")
        wait_done(c)
        status = c.get("/settings/import/status").text
        assert "Imported 3 episodes and 2 known videos." in status
        assert "ABGT" in c.get("/podcasts").text and "2 episodes" in c.get("/podcasts").text

        # now blocked: page, preview and run
        assert "The import is blocked" in c.get("/settings/import").text
        assert c.post("/settings/import/preview", data=form).status_code == 409
        assert c.post("/settings/import/run", data=form).status_code == 409


def test_ui_shows_error_for_bad_paths(old, tmp_path):
    root, config, downloads = old
    with make_client(tmp_path, import_dir=root) as c:
        do_login(c)
        r = c.post("/settings/import/preview", data={"csrf_token": csrf_of(c), "config_file": "/etc/passwd",
                                                       "downloads_dir": str(downloads)})
        assert r.status_code == 422 and "must be below" in r.text
        assert "root:" not in r.text


def test_import_requires_login_and_csrf(old, tmp_path):
    root, config, downloads = old
    with make_client(tmp_path, import_dir=root) as c:
        for path in ("/settings/import", "/settings/import/status"):
            assert c.get(path).status_code == 303
        for path in ("/settings/import/preview", "/settings/import/run"):
            assert c.post(path, data={"config_file": str(config), "downloads_dir": str(downloads)}).status_code == 303
        do_login(c)
        for path in ("/settings/import/preview", "/settings/import/run"):
            assert c.post(path, data={"config_file": str(config), "downloads_dir": str(downloads)}).status_code == 403
        assert episodes.count_all(connect(c.app.state.config.db_path)) == 0
