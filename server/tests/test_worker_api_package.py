import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app import episodes, queue, settings, workers
from app.db import connect
from app.main import create_app

from helpers import make_config
from pkghelpers import JPEG, M4A, info, manifest, package, sha, upload_all
from qhelpers import add_download, add_podcast, attempts, task

OCTET = {"Content-Type": "application/octet-stream"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://pod.example.org") as c:
        c.config = c.app.state.config
        conn = connect(c.config.db_path)
        add_podcast(conn)
        conn.close()
        yield c


def q(c, fn):
    conn = connect(c.config.db_path)
    try:
        return fn(conn)
    finally:
        conn.close()


def worker(c, name="w1"):
    def make(conn):
        wid, token = workers.create(conn, {"name": name, "tier": 1, "active": 1, "pause_min_s": 0, "pause_max_s": None, "downloads_per_hour": 1000})
        return wid, {"Authorization": f"Bearer {token}"}
    return q(c, make)


def claimed_download(c, h, video_id="aaaaaaaaaaa", title="Group Therapy 701: Live & Direct"):
    q(c, lambda conn: conn.execute("UPDATE podcasts SET pause_min_s = 0"))        # no pacing pause in these tests
    tid = q(c, lambda conn: add_download(conn, "abgt", video_id, title=title))
    r = c.post("/api/worker/claim", json={"types": ["download"]}, headers=h)
    assert r.json()["task"]["id"] == tid
    return tid


def put(c, h, tid, role, data, ext="", **over):
    params = {"role": role, "size": len(data), "sha256": sha(data), "ext": ext, **over}
    return c.post(f"/api/worker/tasks/{tid}/upload", params=params, content=data, headers={**h, **OCTET})


def complete(c, h, tid, files):
    return c.post(f"/api/worker/tasks/{tid}/complete", json=manifest(files), headers=h)


def media(c):
    d = c.config.media_dir / "abgt"
    return sorted(p.name for p in d.iterdir()) if d.exists() else []


def staging(c):
    return sorted(p.name for p in c.config.staging_dir.iterdir()) if c.config.staging_dir.exists() else []


def episode_rows(c):
    return q(c, lambda conn: [dict(r) for r in conn.execute("SELECT * FROM episodes")])


def feed_items(c):
    return ET.fromstring(c.get("/abgt.xml").text).findall("channel/item")


# --- the happy path ------------------------------------------------------------------------------------------------

def test_full_package_becomes_an_episode_in_the_feed(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files)
    r = complete(client, h, tid, files)
    assert r.status_code == 200
    base = "Group Therapy 701： Live & Direct"
    assert r.json() == {"video_id": "aaaaaaaaaaa", "audio_file": f"{base}.m4a"}
    assert media(client) == sorted([f"{base}.m4a", f"{base}.info.json", f"{base}.chapters.json", f"{base}.square.jpg", f"{base}.webp"])
    (ep,) = episode_rows(client)
    assert (ep["state"], ep["title"], ep["author"], ep["duration_s"], ep["published_at"], ep["size_bytes"]) == (
        "available", "Group Therapy 701: Live & Direct", "Above & Beyond", 3600, "2026-01-05", len(M4A))
    assert ep["audio_file"] == f"{base}.m4a" and ep["thumb_file"] == f"{base}.webp" and ep["chapters_file"] == f"{base}.chapters.json"
    t = q(client, lambda c: task(c, tid))
    assert t["state"] == "done" and t["finished_at"] and t["claimed_by"] is None
    assert q(client, lambda c: attempts(c, tid))[0]["result"] == "ok"
    assert staging(client) == []                                                  # staging cleaned up
    # ... and the feed shows it with the right enclosure, artwork and chapters
    (item,) = feed_items(client)
    enc = item.find("enclosure")
    assert enc.get("length") == str(len(M4A)) and enc.get("type") == "audio/x-m4a"
    assert enc.get("url") == "https://pod.example.org/files/abgt/Group%20Therapy%20701%EF%BC%9A%20Live%20%26%20Direct.m4a"
    ns = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "podcast": "https://podcastindex.org/namespace/1.0"}
    assert item.find("itunes:image", ns).get("href").endswith(".square.jpg")
    assert item.find("podcast:chapters", ns).get("url").endswith(".chapters.json")
    assert item.findtext("itunes:duration", namespaces=ns) == "3600"
    # the published files are served, with range support
    audio = client.get(enc.get("url").replace("https://pod.example.org", ""))
    assert audio.content == M4A and audio.headers["content-type"] == "audio/x-m4a"
    assert client.get(enc.get("url").replace("https://pod.example.org", ""), headers={"Range": "bytes=0-9"}).status_code == 206


def test_minimal_package_without_artwork_or_chapters(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package(thumb=False, square=False, chapters=False)
    upload_all(client, h, tid, files)
    assert complete(client, h, tid, files).status_code == 200
    (ep,) = episode_rows(client)
    assert ep["thumb_file"] is None and ep["square_file"] is None and ep["chapters_file"] is None
    assert len(feed_items(client)) == 1


def test_a_known_deleted_video_comes_back_to_life(client):
    _, h = worker(client)
    q(client, lambda c: episodes.insert(c, podcast_id="abgt", video_id="aaaaaaaaaaa", state="deleted"))
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files)
    assert complete(client, h, tid, files).status_code == 200
    (ep,) = episode_rows(client)
    assert ep["state"] == "available" and ep["audio_file"] and ep["title"].startswith("Group Therapy")


def test_an_existing_episode_is_never_overwritten(client):
    _, h = worker(client)
    q(client, lambda c: episodes.insert(c, podcast_id="abgt", video_id="aaaaaaaaaaa", title="orig", audio_file="orig.m4a", state="available"))
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files)
    r = complete(client, h, tid, files)
    assert r.status_code == 409
    assert episode_rows(client)[0]["audio_file"] == "orig.m4a" and media(client) == []       # files moved in were removed again


def test_same_title_for_a_different_video_gets_the_video_id_in_its_name(client):
    _, h = worker(client)
    for vid in ("aaaaaaaaaaa", "bbbbbbbbbbb"):
        tid = claimed_download(client, h, vid)
        files = package(video_id=vid)
        upload_all(client, h, tid, files)
        assert complete(client, h, tid, files).status_code == 200
        q(client, lambda c: c.execute("UPDATE podcasts SET pause_min_s = 0"))
    names = sorted(e["audio_file"] for e in episode_rows(client))
    assert names == ["Group Therapy 701： Live & Direct [bbbbbbbbbbb].m4a", "Group Therapy 701： Live & Direct.m4a"]
    assert len(media(client)) == 10


# --- corrupted uploads never produce an episode -----------------------------------------------------------------------

def nothing_published(c, tid=None):
    return episode_rows(c) == [] and media(c) == [] and feed_items(c) == []


def test_wrong_hash_on_upload_is_refused_and_leaves_nothing_in_staging(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    r = put(client, h, tid, "audio", M4A, sha256=sha(b"something else"))
    assert r.status_code == 422 and "SHA-256" in r.json()["detail"]
    assert all(not n.endswith(".part") for d in client.config.staging_dir.iterdir() for n in [p.name for p in d.iterdir()])
    assert not any(list(d.glob("audio.bin")) for d in client.config.staging_dir.iterdir())
    assert nothing_published(client)


@pytest.mark.parametrize("kw,status", [({"size": 10}, 400), ({"size": len(M4A) + 5}, 400)])   # Content-Length disagrees
def test_wrong_declared_size_is_refused(client, kw, status):
    _, h = worker(client)
    tid = claimed_download(client, h)
    r = client.post(f"/api/worker/tasks/{tid}/upload", params={"role": "audio", "sha256": sha(M4A), **{"size": len(M4A), **kw}},
                    content=M4A, headers={**h, **OCTET})
    assert r.status_code == status
    assert nothing_published(client)


def test_missing_info_json_is_rejected_as_incomplete_and_retried(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files, only={"audio", "thumb"})
    r = client.post(f"/api/worker/tasks/{tid}/complete", json={"files": [{"role": "audio", "size": len(M4A), "sha256": sha(M4A)}]}, headers=h)
    assert r.status_code == 422 and "info.json" in r.json()["detail"]
    t = q(client, lambda c: task(c, tid))
    assert t["state"] == "pending" and t["failures"] == 1 and t["claimed_by"] is None and t["not_before"]
    a = q(client, lambda c: attempts(c, tid))[0]
    assert (a["result"], a["error_class"]) == ("failed", "incomplete") and "info.json" in a["error_text"]
    assert nothing_published(client) and staging(client) == []


def test_declaring_an_info_json_that_was_never_uploaded(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files, only={"audio"})
    r = complete(client, h, tid, files)
    assert r.status_code == 422 and nothing_published(client)
    assert q(client, lambda c: attempts(c, tid))[0]["error_class"] == "incomplete"


def test_manifest_with_a_wrong_hash_never_publishes(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files)
    bad = manifest(files)
    bad["files"][0]["sha256"] = "0" * 64
    r = client.post(f"/api/worker/tasks/{tid}/complete", json=bad, headers=h)
    assert r.status_code == 422 and nothing_published(client) and q(client, lambda c: task(c, tid))["state"] == "pending"


def test_info_json_of_another_video_is_rejected(client):
    _, h = worker(client)
    tid = claimed_download(client, h, "aaaaaaaaaaa")
    files = package(video_id="zzzzzzzzzzz")
    upload_all(client, h, tid, files)
    r = complete(client, h, tid, files)
    assert r.status_code == 422 and "different video" in r.json()["detail"] and nothing_published(client)


def test_file_that_is_not_what_its_role_says_is_refused_at_upload(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    assert put(client, h, tid, "audio", b"this is plain text, not audio").status_code == 422
    assert put(client, h, tid, "thumb", b"<html>not an image</html>", ext="jpg").status_code == 422
    assert put(client, h, tid, "square", b"\x89PNG\r\n\x1a\n....", ext="").status_code == 422     # the square image must be a JPEG
    assert put(client, h, tid, "info", b"{broken").status_code == 422
    assert put(client, h, tid, "chapters", b'{"chapters": "x"}').status_code == 422
    assert nothing_published(client)


def test_a_failure_while_publishing_leaves_no_files_behind(client, monkeypatch):
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files)

    def boom(*a, **k):
        raise RuntimeError("database exploded")

    monkeypatch.setattr(queue, "complete_download", boom)
    with pytest.raises(RuntimeError):
        complete(client, h, tid, files)
    assert media(client) == [] and episode_rows(client) == [] and feed_items(client) == []
    assert q(client, lambda c: task(c, tid))["state"] == "claimed"
    monkeypatch.undo()
    assert complete(client, h, tid, files).status_code == 200                 # the package went back to staging: retry works
    assert len(feed_items(client)) == 1


def test_a_move_failure_leaves_nothing_half_visible(client, monkeypatch):
    import os
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files)
    real, n = os.replace, {"n": 0}

    def flaky(a, b):
        n["n"] += 1
        if n["n"] == 3:
            raise OSError("disk full")
        return real(a, b)

    monkeypatch.setattr(os, "replace", flaky)
    with pytest.raises(OSError):
        complete(client, h, tid, files)
    monkeypatch.setattr(os, "replace", real)
    assert media(client) == [] and feed_items(client) == [] and episode_rows(client) == []
    assert complete(client, h, tid, files).status_code == 200                  # a retry succeeds
    assert len(feed_items(client)) == 1


def test_the_feed_never_lists_an_episode_before_complete(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files)                                          # everything uploaded, not yet completed
    assert feed_items(client) == [] and media(client) == []
    complete(client, h, tid, files)
    assert len(feed_items(client)) == 1


# --- authorisation and limits ------------------------------------------------------------------------------------------------

def test_only_the_claiming_worker_can_upload_or_complete(client):
    _, h1 = worker(client, "w1")
    _, h2 = worker(client, "w2")
    tid = claimed_download(client, h1)
    files = package()
    assert put(client, h2, tid, "audio", M4A).status_code == 409
    assert complete(client, h2, tid, files).status_code == 409
    assert put(client, {}, tid, "audio", M4A).status_code == 401
    assert put(client, h1, 9999, "audio", M4A).status_code == 404
    assert nothing_published(client)


def test_a_worker_that_lost_the_lease_cannot_publish(client):
    _, dead = worker(client, "dead")
    _, alive = worker(client, "alive")
    tid = claimed_download(client, dead)
    files = package()
    upload_all(client, dead, tid, files)
    q(client, lambda c: c.execute("UPDATE tasks SET lease_until = '2000-01-01T00:00:00Z'"))
    q(client, lambda c: queue.expire_leases(c))
    assert client.post("/api/worker/claim", json={"types": ["download"]}, headers=alive).json()["task"]["id"] == tid
    assert complete(client, dead, tid, files).status_code == 409                # the old attempt is gone
    assert put(client, dead, tid, "audio", M4A).status_code == 409
    assert nothing_published(client)


def test_uploads_only_for_claimed_download_tasks(client):
    _, h = worker(client)
    scan = q(client, lambda c: queue.create_scan(c, "abgt"))
    client.post("/api/worker/claim", json={"types": ["scan"]}, headers=h)
    assert put(client, h, scan, "audio", M4A).status_code == 409
    pending = q(client, lambda c: add_download(c, "abgt", "ccccccccccc"))
    assert put(client, h, pending, "audio", M4A).status_code == 409


def test_size_limits(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    q(client, lambda c: settings.save(c, {"upload_limit_mb": 1}))
    big = M4A + b"\0" * (1 << 20)
    r = put(client, h, tid, "audio", big)
    assert r.status_code == 413 and "1 MB" in r.json()["detail"]
    assert put(client, h, tid, "chapters", b"{}" + b" " * (1 << 20)).status_code == 413     # 1 MB cap for small roles
    assert put(client, h, tid, "audio", M4A).status_code == 200                              # within the limit


def test_upload_parameter_validation(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    base = {"role": "audio", "size": len(M4A), "sha256": sha(M4A)}
    for bad in ({"role": "script"}, {"sha256": "xyz"}, {"sha256": sha(M4A).upper()}, {"size": 0}, {"size": -1}, {"size": "x"}):
        r = client.post(f"/api/worker/tasks/{tid}/upload", params={**base, **bad}, content=M4A, headers={**h, **OCTET})
        assert r.status_code == 422, bad
    assert put(client, h, tid, "thumb", JPEG, ext="").status_code == 422
    assert put(client, h, tid, "thumb", JPEG, ext="php").status_code == 422
    assert put(client, h, tid, "thumb", JPEG, ext="../x").status_code == 422
    assert client.post(f"/api/worker/tasks/{tid}/upload", params=base, content=M4A, headers={**h, **OCTET, "Content-Length": "5"}).status_code in (400, 422)


def test_reupload_replaces_a_file(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    assert put(client, h, tid, "audio", M4A, sha256=sha(b"bad")).status_code == 422
    assert put(client, h, tid, "audio", M4A).status_code == 200
    assert put(client, h, tid, "audio", M4A + b"more").status_code == 200
    files = package()
    files["audio"] = (M4A + b"more", "")
    upload_all(client, h, tid, files, only={"info"})
    assert complete(client, h, tid, {"audio": files["audio"], "info": files["info"]}).status_code == 200


def test_client_cannot_choose_file_names_or_paths(client):
    _, h = worker(client)
    tid = claimed_download(client, h, title="../../etc/passwd")
    files = package(title="../../evil/../../name")        # the title comes from info.json: the worker is not trusted
    upload_all(client, h, tid, files)
    r = complete(client, h, tid, files)
    assert r.status_code == 200
    names = media(client)
    assert all("/" not in n and not n.startswith(".") for n in names)
    assert {p.name for p in client.config.media_dir.iterdir()} == {"abgt"}                  # nothing escaped the podcast directory
    assert not (client.config.data_dir / "etc").exists()


def test_progress_is_stored_with_the_heartbeat_and_cleared_afterwards(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    client.post(f"/api/worker/tasks/{tid}/heartbeat", json={"progress": "uploading audio 40%"}, headers=h)
    assert q(client, lambda c: task(c, tid))["progress"] == "uploading audio 40%"
    client.post(f"/api/worker/tasks/{tid}/heartbeat", json={}, headers=h)
    assert q(client, lambda c: task(c, tid))["progress"] == "uploading audio 40%"      # a heartbeat without text keeps it
    page_login = None
    files = package()
    upload_all(client, h, tid, files)
    complete(client, h, tid, files)
    assert q(client, lambda c: task(c, tid))["progress"] == ""


def test_staging_is_cleaned_on_server_start(tmp_path):
    config = make_config(tmp_path)
    (config.staging_dir / "99-99").mkdir(parents=True)
    (config.staging_dir / "99-99" / "audio.part").write_bytes(b"x" * 100)
    with TestClient(create_app(config)):
        assert list(config.staging_dir.iterdir()) == []


def test_large_upload_is_streamed_to_disk(client):
    _, h = worker(client)
    tid = claimed_download(client, h)
    q(client, lambda c: settings.save(c, {"upload_limit_mb": 64}))
    big = M4A + bytes(range(256)) * (8 << 12)           # ~8 MB
    assert put(client, h, tid, "audio", big).status_code == 200
    d = next(client.config.staging_dir.iterdir())
    assert (d / "audio.bin").stat().st_size == len(big)


def test_podcast_page_shows_the_newest_episodes(client):
    from helpers import do_login
    _, h = worker(client)
    tid = claimed_download(client, h)
    files = package()
    upload_all(client, h, tid, files)
    complete(client, h, tid, files)
    do_login(client)
    page = client.get("/podcasts/abgt").text
    assert "Newest episodes" in page and "Group Therapy 701: Live &amp; Direct" in page and "2026-01-05" in page and "60 min" in page
