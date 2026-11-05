import os
import xml.etree.ElementTree as ET

import pytest
from fastapi.testclient import TestClient

from app import episodes, podcasts
from app.db import connect
from app.main import create_app
from app.public import media_type_of, safe_media_path

from helpers import make_client, make_config

AUDIO = bytes(range(256)) * 40  # 10240 bytes


@pytest.fixture
def client(tmp_path):
    config = make_config(tmp_path)
    with TestClient(create_app(config), base_url="https://pod.example.org", follow_redirects=False) as c:
        conn = connect(config.db_path)
        podcasts.create(conn, {"id": "abgt", "name": "ABGT", "playlist_url": "https://x.example/p"})
        podcasts.create(conn, {"id": "asot", "name": "ASOT", "playlist_url": "https://x.example/q"})
        episodes.insert(conn, podcast_id="abgt", video_id="v1", title="Ep 1", audio_file="Ep 1 #?.m4a",
                        square_file="Ep 1 #?.square.jpg", size_bytes=len(AUDIO), duration_s=60,
                        published_at="2024-01-01")
        conn.close()
        media = config.media_dir / "abgt"
        media.mkdir(parents=True)
        (media / "Ep 1 #?.m4a").write_bytes(AUDIO)
        (media / "Ep 1 #?.square.jpg").write_bytes(b"jpeg")
        (media / "Ep 1 #?.chapters.json").write_text("{}")
        (media / "Ep 1 #?.info.json").write_text("{}")
        (media / "notes.txt").write_text("secret")
        (media / ".hidden.m4a").write_bytes(b"x")
        (config.media_dir / "asot").mkdir()
        (config.media_dir / "asot" / "other.m4a").write_bytes(b"other")
        (tmp_path / "outside.m4a").write_bytes(b"outside")
        c.media = media
        c.tmp = tmp_path
        yield c


# --- feed -------------------------------------------------------------------

def test_feed_is_public_and_uses_request_host_without_base_url(client):
    r = client.get("/abgt.xml")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/xml; charset=utf-8"
    item = ET.fromstring(r.text).find("channel/item")
    assert item.find("enclosure").get("url") == "https://pod.example.org/files/abgt/Ep%201%20%23%3F.m4a"


def test_feed_uses_configured_base_url(tmp_path):
    with TestClient(create_app(make_config(tmp_path, base_url="https://env.example.org/")),
                    base_url="https://other.example") as c:
        conn = connect(tmp_path / "podcaster.db")
        podcasts.create(conn, {"id": "abgt", "name": "A", "playlist_url": "https://x.example/p"})
        episodes.insert(conn, podcast_id="abgt", video_id="v", title="T", audio_file="t.m4a", size_bytes=1)
        conn.close()
        assert "https://env.example.org/files/abgt/t.m4a" in c.get("/abgt.xml").text


def test_unknown_feed_is_404(client):
    for path in ["/nope.xml", "/Bad_Slug.xml", "/.xml"]:
        assert client.get(path).status_code == 404, path


def test_head_feed(client):
    r = client.head("/abgt.xml")
    assert r.status_code == 200 and r.content == b""


def test_paused_podcast_still_has_feed(client):
    conn = connect(client.app.state.config.db_path)
    podcasts.set_active(conn, "abgt", False)
    conn.close()
    assert client.get("/abgt.xml").status_code == 200


# --- files ------------------------------------------------------------------

def test_file_served_with_content_type_and_ranges(client):
    r = client.get("/files/abgt/Ep%201%20%23%3F.m4a")
    assert r.status_code == 200 and r.content == AUDIO
    assert r.headers["content-type"] == "audio/x-m4a"
    assert r.headers["accept-ranges"] == "bytes"
    assert int(r.headers["content-length"]) == len(AUDIO)
    part = client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"Range": "bytes=100-199"})
    assert part.status_code == 206 and part.content == AUDIO[100:200]
    assert part.headers["content-range"] == f"bytes 100-199/{len(AUDIO)}"
    tail = client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"Range": "bytes=-50"})
    assert tail.status_code == 206 and tail.content == AUDIO[-50:]
    open_end = client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"Range": "bytes=10000-"})
    assert open_end.status_code == 206 and open_end.content == AUDIO[10000:]
    bad = client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"Range": "bytes=99999-"})
    assert bad.status_code == 416


def test_head_and_conditional_requests(client):
    h = client.head("/files/abgt/Ep%201%20%23%3F.m4a")
    assert h.status_code == 200 and int(h.headers["content-length"]) == len(AUDIO) and h.content == b""
    full = client.get("/files/abgt/Ep%201%20%23%3F.m4a")
    again = client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"If-None-Match": full.headers["etag"]})
    assert again.status_code == 304 and again.content == b""
    assert client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"If-None-Match": '"nope"'}).status_code == 200
    since = client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"If-Modified-Since": full.headers["last-modified"]})
    assert since.status_code == 304
    old = client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"If-Modified-Since": "Mon, 01 Jan 2001 00:00:00 GMT"})
    assert old.status_code == 200
    assert client.get("/files/abgt/Ep%201%20%23%3F.m4a", headers={"If-Modified-Since": "garbage"}).status_code == 200


@pytest.mark.parametrize("name,expected", [
    ("a.m4a", "audio/x-m4a"), ("a.M4A", "audio/x-m4a"), ("a.square.jpg", "image/jpeg"), ("a.png", "image/png"),
    ("a.webp", "image/webp"), ("a.chapters.json", "application/json+chapters"),
    ("a.info.json", None), ("archive.txt", None), ("a.html", None), ("a", None),
])
def test_media_types(name, expected):
    assert media_type_of(name) == expected


def test_chapters_and_images_served(client):
    assert client.get("/files/abgt/Ep%201%20%23%3F.chapters.json").headers["content-type"] == "application/json+chapters"
    assert client.get("/files/abgt/Ep%201%20%23%3F.square.jpg").headers["content-type"] == "image/jpeg"


@pytest.mark.parametrize("path", [
    "/files/abgt/Ep%201%20%23%3F.info.json",   # not a served type
    "/files/abgt/notes.txt",
    "/files/abgt/.hidden.m4a",
    "/files/abgt/missing.m4a",
    "/files/nope/x.m4a",
    "/files/asot/Ep%201%20%23%3F.m4a",          # other podcast's directory has no such file
    "/files/abgt/..%2F..%2Foutside.m4a",
    "/files/abgt/%2E%2E%2Foutside.m4a",
    "/files/abgt/..%2fasot%2fother.m4a",
    "/files/abgt/..%5C..%5Coutside.m4a",
    "/files/..%2Fabgt/Ep%201%20%23%3F.m4a",
    "/files/abgt/%2e%2e",
    "/files/abgt/..",
    "/files/abgt/%00.m4a",
    "/files/abgt/Ep%201%20%23%3F.m4a%00.jpg",
    "/files/abgt//etc/passwd",
    "/files/../outside.m4a",
    "/files/%2e%2e/outside.m4a",
    "/files/abgt/",
    "/files/",
])
def test_traversal_and_unserved_files_are_404(client, path):
    assert client.get(path).status_code == 404, path


def test_symlinks_are_not_followed(client):
    os.symlink(client.tmp / "outside.m4a", client.media / "link.m4a")
    os.symlink(client.tmp / "outside.m4a", client.media / "dirlink.m4a")
    assert client.get("/files/abgt/link.m4a").status_code == 404
    # symlinked directory
    os.symlink(client.tmp, client.app.state.config.media_dir / "evil")
    assert client.get("/files/evil/outside.m4a").status_code == 404


def test_safe_media_path_unit(tmp_path):
    (tmp_path / "p").mkdir()
    (tmp_path / "p" / "a.m4a").write_bytes(b"x")
    assert safe_media_path(tmp_path, "p", "a.m4a") == tmp_path / "p" / "a.m4a"
    for pid, name in [("p", "../p/a.m4a"), ("p", "a/b.m4a"), ("..", "a.m4a"), ("P", "a.m4a"), ("p", ""), ("p", "."),
                      ("p", "\\a.m4a"), ("p/../p", "a.m4a")]:
        assert safe_media_path(tmp_path, pid, name) is None, (pid, name)


def test_public_routes_need_no_session_and_no_csrf(client):
    assert client.get("/abgt.xml").status_code == 200
    assert client.get("/files/abgt/Ep%201%20%23%3F.m4a").status_code == 200
    # admin routes stay protected
    assert client.get("/podcasts").status_code == 303
    assert "content-security-policy" not in client.get("/abgt.xml").headers


def test_post_to_public_routes_not_allowed(client):
    assert client.post("/abgt.xml").status_code in (404, 405)
    assert client.post("/files/abgt/Ep%201%20%23%3F.m4a").status_code in (404, 405)
