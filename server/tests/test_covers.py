import re
import xml.etree.ElementTree as ET
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from app import covers, podcasts
from app.db import connect
from app.main import create_app

from helpers import csrf_of, do_login, make_config
from test_podcasts import valid

PNG = b"\x89PNG\r\n\x1a\n" + b"p" * 200
PNG2 = b"\x89PNG\r\n\x1a\n" + b"q" * 300
JPG = b"\xff\xd8\xff\xe0" + b"j" * 200
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"w" * 50
PUBLIC = [(None, None, None, "", ("93.184.216.34", 0))]


@pytest.fixture(autouse=True)
def public_dns(monkeypatch):
    monkeypatch.setattr(covers, "RESOLVE", lambda host, port: PUBLIC)


def serving(routes: dict, calls: list | None = None):
    """A fake web server: path -> (status, body, headers)."""
    def handler(request: httpx.Request) -> httpx.Response:
        if calls is not None:
            calls.append(str(request.url))
        status, body, headers = routes.get(request.url.path, (404, b"", {}))
        return httpx.Response(status, content=body, headers=headers)
    return httpx.MockTransport(handler)


# --- the building blocks ---------------------------------------------------------------------------------------------

def test_detect_and_check_bytes():
    assert (covers.detect(PNG), covers.detect(JPG), covers.detect(WEBP)) == ("png", "jpg", "webp")
    for bad in (b"GIF89a....", b"<svg xmlns='x'/>", b"<html>", b"RIFF\x00\x00\x00\x00WAVE"):
        with pytest.raises(covers.CoverError, match="only JPEG, PNG and WebP"):
            covers.check_bytes(bad)
    with pytest.raises(covers.CoverError, match="empty"):
        covers.check_bytes(b"")
    with pytest.raises(covers.CoverError, match="larger than 5 MB"):
        covers.check_bytes(PNG + b"x" * covers.MAX_BYTES)


@pytest.fixture
def stored(tmp_path):
    from helpers import make_config
    from qhelpers import add_podcast, make_db
    conn = connect(make_db(tmp_path))
    add_podcast(conn, "abgt", image_url="https://example.org/old.png")
    media = tmp_path / "media"
    return conn, media


def test_store_names_the_file_after_its_content_and_replaces_the_old_one(stored):
    conn, media = stored
    first = covers.store(conn, media, "abgt", PNG, "https://example.org/a.png")
    assert re.fullmatch(r"cover-[0-9a-f]{10}\.png", first) and (media / "abgt" / first).read_bytes() == PNG
    row = podcasts.get(conn, "abgt")
    assert (row["cover_file"], row["image_url"]) == (first, "https://example.org/a.png")
    assert covers.store(conn, media, "abgt", PNG) == first                      # same content: same name, nothing removed
    assert (media / "abgt" / first).exists()
    other = tmp_other = covers.store(conn, media, "abgt", JPG)
    assert other.endswith(".jpg") and other != first and not (media / "abgt" / first).exists()
    assert sorted(p.name for p in (media / "abgt").iterdir()) == [other]        # no temp files left
    assert podcasts.get(conn, "abgt")["image_url"] == ""                        # an uploaded cover has no source URL


def test_store_only_ever_removes_its_own_files(stored):
    conn, media = stored
    (media / "abgt").mkdir(parents=True)
    (media / "abgt" / "Episode 1.m4a").write_bytes(b"audio")
    (media / "abgt" / "thumbnail.jpg").write_bytes(JPG)
    conn.execute("UPDATE podcasts SET cover_file = 'Episode 1.m4a'")
    covers.store(conn, media, "abgt", PNG)
    covers.remove(conn, media, "abgt")
    assert (media / "abgt" / "Episode 1.m4a").exists() and (media / "abgt" / "thumbnail.jpg").exists()
    assert podcasts.get(conn, "abgt")["cover_file"] is None


def test_remove_drops_file_reference_and_source_url(stored):
    conn, media = stored
    name = covers.store(conn, media, "abgt", PNG, "https://example.org/a.png")
    covers.remove(conn, media, "abgt")
    row = podcasts.get(conn, "abgt")
    assert (row["cover_file"], row["image_url"]) == (None, "") and not (media / "abgt" / name).exists()


def test_import_file_works_like_an_upload(stored, tmp_path):
    conn, media = stored
    source = tmp_path / "thumbnail.webp"
    source.write_bytes(WEBP)
    name = covers.import_file(conn, media, "abgt", source)
    assert name.endswith(".webp") and (media / "abgt" / name).read_bytes() == WEBP and source.exists()   # the original stays
    assert podcasts.get(conn, "abgt")["cover_file"] == name


# --- fetching ----------------------------------------------------------------------------------------------------------

def test_fetch_downloads_an_image():
    assert covers.fetch("https://img.example.org/a.png", serving({"/a.png": (200, PNG, {})})) == PNG


def test_fetch_follows_redirects_up_to_a_limit():
    t = serving({"/a": (302, b"", {"location": "/b"}), "/b": (301, b"", {"location": "https://cdn.example.org/c.jpg"}),
                 "/c.jpg": (200, JPG, {})})
    assert covers.fetch("https://img.example.org/a", t) == JPG
    loop = serving({"/a": (302, b"", {"location": "/a"})})
    with pytest.raises(covers.CoverError, match="too many redirects"):
        covers.fetch("https://img.example.org/a", loop)


@pytest.mark.parametrize("status,body,expect", [
    (404, b"", "answered 404"), (200, b"<html>no</html>", "only JPEG, PNG and WebP"), (200, b"", "empty"),
    (200, PNG + b"x" * (6 << 20), "larger than 5 MB")])
def test_fetch_rejects_bad_answers(status, body, expect):
    with pytest.raises(covers.CoverError, match=expect):
        covers.fetch("https://img.example.org/a.png", serving({"/a.png": (status, body, {})}))


def test_fetch_rejects_a_declared_oversize_without_reading_it():
    t = serving({"/a.png": (200, PNG, {"content-length": str(covers.MAX_BYTES + 1)})})
    with pytest.raises(covers.CoverError, match="larger than 5 MB"):
        covers.fetch("https://img.example.org/a.png", t)


@pytest.mark.parametrize("url", ["ftp://example.org/a.png", "file:///etc/passwd", "javascript:alert(1)", "https:///a.png"])
def test_fetch_rejects_other_schemes(url):
    with pytest.raises(covers.CoverError, match="http:// or https://"):
        covers.fetch(url, serving({}))


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "192.168.0.5", "169.254.169.254", "172.16.0.1", "::1", "fe80::1", "0.0.0.0"])
def test_fetch_refuses_private_and_local_addresses(monkeypatch, address):
    monkeypatch.setattr(covers, "RESOLVE", lambda host, port: [(None, None, None, "", (address, 0))])
    calls = []
    with pytest.raises(covers.CoverError, match="private or local network"):
        covers.fetch("https://internal.example.org/a.png", serving({"/a.png": (200, PNG, {})}, calls))
    assert calls == []                                                           # nothing was even requested


def test_a_redirect_into_a_private_network_is_refused(monkeypatch):
    monkeypatch.setattr(covers, "RESOLVE", lambda host, port: [(None, None, None, "", ("127.0.0.1" if host == "intranet" else "93.184.216.34", 0))])
    t = serving({"/a": (302, b"", {"location": "http://intranet/secret.png"}), "/secret.png": (200, PNG, {})})
    with pytest.raises(covers.CoverError, match="private or local"):
        covers.fetch("https://img.example.org/a", t)


def test_fetch_reports_unresolvable_hosts_and_network_errors_without_the_url(monkeypatch):
    def boom(host, port):
        raise OSError("no such host")
    monkeypatch.setattr(covers, "RESOLVE", boom)
    with pytest.raises(covers.CoverError, match="cannot be resolved"):
        covers.fetch("https://nowhere.example.org/a.png", serving({}))
    monkeypatch.setattr(covers, "RESOLVE", lambda h, p: PUBLIC)

    def refuse(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")
    with pytest.raises(covers.CoverError) as info:
        covers.fetch("https://secret-token.example.org/a.png?key=abc", httpx.MockTransport(refuse))
    assert "ConnectError" in str(info.value) and "secret-token" not in str(info.value) and "key=abc" not in str(info.value)


# --- the podcast form ---------------------------------------------------------------------------------------------------

@pytest.fixture
def c(tmp_path):
    calls: list = []
    web = serving({"/a.png": (200, PNG, {}), "/b.png": (200, PNG2, {}), "/bad": (200, b"nope", {}), "/j.jpg": (200, JPG, {})}, calls)
    config = make_config(tmp_path)
    with TestClient(create_app(config, http_transport=web), base_url="https://testserver", follow_redirects=False) as client:
        do_login(client)
        client.config, client.calls = config, calls
        yield client


def save(c, path="/podcasts/abgt", files=None, **over):
    return c.post(path, data={"csrf_token": csrf_of(c), **valid(**over)}, files=files)


def row(c):
    conn = connect(c.config.db_path)
    try:
        return conn.execute("SELECT * FROM podcasts WHERE id = 'abgt'").fetchone()
    finally:
        conn.close()


def media(c):
    folder = c.config.media_dir / "abgt"
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


def make(c, **over):
    assert save(c, "/podcasts", **over).status_code == 303


def feed_image(c):
    root = ET.fromstring(c.get("/abgt.xml").text)
    return root.find("channel/image/url").text


def test_upload_on_create_and_edit_is_served_in_the_feed(c):
    r = save(c, "/podcasts", files={"cover_upload": ("logo.png", PNG, "image/png")})
    assert r.status_code == 303
    first = row(c)["cover_file"]
    assert first.endswith(".png") and media(c) == [first]
    assert feed_image(c) == f"/files/abgt/{first}" or feed_image(c).endswith(f"/files/abgt/{first}")
    assert c.get(f"/files/abgt/{first}").content == PNG
    assert f'/files/abgt/{first}' in c.get("/podcasts/abgt").text and "Remove the cover image" in c.get("/podcasts/abgt").text
    # a new upload replaces it and the old file goes
    assert save(c, files={"cover_upload": ("logo.jpg", JPG, "image/jpeg")}).status_code == 303
    second = row(c)["cover_file"]
    assert second.endswith(".jpg") and second != first and media(c) == [second]
    assert c.get(f"/files/abgt/{first}").status_code == 404


def test_a_bad_upload_saves_nothing(c):
    make(c)
    r = save(c, name="Changed", files={"cover_upload": ("x.gif", b"GIF89a....", "image/gif")})
    assert r.status_code == 422 and "cannot be used: only JPEG, PNG and WebP" in r.text
    assert row(c)["name"] == "ABGT" and row(c)["cover_file"] is None
    r = save(c, files={"cover_upload": ("big.png", PNG + b"x" * (5 << 20), "image/png")})
    assert r.status_code == 422 and "larger than 5 MB" in r.text and media(c) == []


def test_a_remote_url_is_fetched_once_and_served_locally(c):
    make(c, image_url="https://img.example.org/a.png")
    name = row(c)["cover_file"]
    assert name and row(c)["image_url"] == "https://img.example.org/a.png" and c.calls == ["https://img.example.org/a.png"]
    assert feed_image(c).endswith(f"/files/abgt/{name}")                          # the feed does not point at the remote host
    save(c, name="Renamed", image_url="https://img.example.org/a.png")           # unchanged address: no new download
    assert len(c.calls) == 1 and row(c)["name"] == "Renamed" and row(c)["cover_file"] == name
    save(c, image_url="https://img.example.org/b.png")                           # changed address: fetched again
    assert len(c.calls) == 2 and row(c)["cover_file"] != name and len(media(c)) == 1


def test_a_failing_url_blocks_the_save_with_a_message(c):
    make(c)
    r = save(c, name="Changed", image_url="https://img.example.org/bad")
    assert r.status_code == 422 and "could not be fetched: only JPEG, PNG and WebP" in r.text
    assert row(c)["name"] == "ABGT" and row(c)["image_url"] == ""
    r = save(c, image_url="https://img.example.org/missing.png")
    assert r.status_code == 422 and "answered 404" in r.text
    r = save(c, "/podcasts", id="other", image_url="https://img.example.org/bad")
    assert r.status_code == 422 and connect(c.config.db_path).execute("SELECT COUNT(*) FROM podcasts WHERE id = 'other'").fetchone()[0] == 0


def test_a_file_wins_over_the_url_and_clears_it(c):
    make(c)
    save(c, image_url="https://img.example.org/a.png", files={"cover_upload": ("l.jpg", JPG, "image/jpeg")})
    r = row(c)
    assert r["cover_file"].endswith(".jpg") and r["image_url"] == "" and c.calls == []


def test_remove_by_checkbox_or_by_emptying_the_fetched_address(c):
    make(c, image_url="https://img.example.org/a.png")
    r = c.post("/podcasts/abgt", data={"csrf_token": csrf_of(c), **valid(image_url="https://img.example.org/a.png"), "cover_remove": "1"})
    assert r.status_code == 303 and row(c)["cover_file"] is None and row(c)["image_url"] == "" and media(c) == []
    make_again = save(c, image_url="https://img.example.org/a.png")
    assert row(c)["cover_file"] and make_again.status_code == 303
    save(c, image_url="")                                                        # emptied: the fetched cover goes
    assert row(c)["cover_file"] is None and media(c) == []


def test_an_uploaded_cover_survives_saving_with_an_empty_address(c):
    make(c, files=None)
    save(c, files={"cover_upload": ("l.png", PNG, "image/png")})
    name = row(c)["cover_file"]
    save(c, name="Other name")
    assert row(c)["cover_file"] == name and media(c) == [name]


def test_a_legacy_address_is_left_alone_until_it_changes(c):
    make(c)
    conn = connect(c.config.db_path)
    conn.execute("UPDATE podcasts SET image_url = 'https://old.example.org/x.jpg'")
    conn.close()
    assert save(c, image_url="https://old.example.org/x.jpg", name="Renamed").status_code == 303
    assert c.calls == [] and row(c)["cover_file"] is None
    assert feed_image(c) == "https://old.example.org/x.jpg"                      # the feed still uses the old address


def test_refresh_fetches_again(c):
    make(c, image_url="https://img.example.org/a.png")
    assert c.post("/podcasts/abgt/cover/refresh", data={"csrf_token": csrf_of(c)}).status_code == 303
    assert len(c.calls) == 2
    make2 = c.post("/podcasts/abgt", data={"csrf_token": csrf_of(c), **valid(image_url="")})
    r = c.post("/podcasts/abgt/cover/refresh", data={"csrf_token": csrf_of(c)})
    assert r.status_code == 502 and "no image URL" in r.text
    assert c.post("/podcasts/nope/cover/refresh", data={"csrf_token": csrf_of(c)}).status_code == 404


def test_episodes_without_artwork_use_the_cover(c):
    from app import episodes
    make(c)
    save(c, files={"cover_upload": ("l.png", PNG, "image/png")})
    conn = connect(c.config.db_path)
    episodes.insert(conn, podcast_id="abgt", video_id="v1", title="E1", audio_file="E1.m4a", size_bytes=5, published_at="2026-01-01")
    conn.close()
    name = row(c)["cover_file"]
    images = [i.get("href") for i in ET.fromstring(c.get("/abgt.xml").text).iter("{http://www.itunes.com/dtds/podcast-1.0.dtd}image")]
    assert images and all(i.endswith(f"/files/abgt/{name}") for i in images)


def test_deleting_a_podcast_removes_its_cover_file(c):
    make(c)
    save(c, files={"cover_upload": ("l.png", PNG, "image/png")})
    assert media(c)
    assert c.post("/podcasts/abgt/delete", data={"csrf_token": csrf_of(c)}).status_code == 303
    assert media(c) == []


def test_body_limits_for_the_form(c):
    make(c)
    big_ok = save(c, files={"cover_upload": ("l.png", PNG + b"x" * (4 << 20), "image/png")})
    assert big_ok.status_code == 303
    too_big = save(c, files={"cover_upload": ("l.png", PNG + b"x" * (7 << 20), "image/png")})
    assert too_big.status_code == 413
    # other forms keep the small limit
    r = c.post("/settings", data={"csrf_token": csrf_of(c)}, files={"x": ("f", b"x" * (2 << 20))})
    assert r.status_code == 413


def test_migration_adds_the_column(tmp_path):
    from qhelpers import make_db
    conn = connect(make_db(tmp_path))
    assert "cover_file" in [r[1] for r in conn.execute("PRAGMA table_info(podcasts)")]
