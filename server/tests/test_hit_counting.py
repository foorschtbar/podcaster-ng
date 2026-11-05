"""The middleware: what is counted, what is not, and that nothing identifying is stored."""

import pytest
from fastapi.testclient import TestClient

from app import episodes, podcasts
from app.db import connect
from app.main import create_app

from helpers import csrf_of, do_login, make_client, make_config

AUDIO = bytes(range(256)) * 40  # 10240 bytes
UA_OVERCAST = "Overcast/3.0 (+http://overcast.fm/; iOS podcast app)"
SECRET_UA = "Overcast/3.0 SecretDevice-12345 build=XYZ-987"


@pytest.fixture
def client(tmp_path):
    config = make_config(tmp_path)
    with TestClient(create_app(config), base_url="https://pod.example.org", follow_redirects=False) as c:
        conn = connect(config.db_path)
        podcasts.create(conn, {"id": "abgt", "name": "ABGT", "playlist_url": "https://x.example/p"})
        episodes.insert(conn, podcast_id="abgt", video_id="v1", title="Ep 1", audio_file="ep1.m4a",
                        square_file="ep1.square.jpg", size_bytes=len(AUDIO))
        conn.close()
        media = config.media_dir / "abgt"
        media.mkdir(parents=True)
        (media / "ep1.m4a").write_bytes(AUDIO)
        (media / "ep1.square.jpg").write_bytes(b"jpeg")
        (media / "ep1.chapters.json").write_text("{}")
        c.db = config.db_path
        yield c


def table(client):
    conn = connect(client.db)
    out = [tuple(r) for r in conn.execute(
        "SELECT podcast_id, episode_id, kind, app_group, count, bytes FROM hits_daily ORDER BY kind, app_group")]
    conn.close()
    return out


def get(client, path, ua=UA_OVERCAST, **headers):
    return client.get(path, headers={"User-Agent": ua, **headers})


def test_feed_request_counted_with_response_size(client):
    r = get(client, "/abgt.xml")
    assert r.status_code == 200
    assert table(client) == [("abgt", None, "feed", "Overcast", 1, len(r.content))]
    get(client, "/abgt.xml")
    assert table(client)[0][4] == 2


def test_full_download_counted_once_with_bytes(client):
    get(client, "/files/abgt/ep1.m4a")
    assert table(client) == [("abgt", "v1", "file", "Overcast", 1, len(AUDIO))]


def test_range_requests_count_once_but_add_bytes(client):
    get(client, "/files/abgt/ep1.m4a", Range="bytes=0-99")        # first range: counts
    get(client, "/files/abgt/ep1.m4a", Range="bytes=100-1099")    # continuation: bytes only
    get(client, "/files/abgt/ep1.m4a", Range="bytes=-50")         # tail probe: bytes only
    assert table(client) == [("abgt", "v1", "file", "Overcast", 1, 100 + 1000 + 50)]


def test_not_counted(client):
    get(client, "/nope.xml")                                      # 404
    get(client, "/files/abgt/missing.m4a")                        # 404
    get(client, "/files/abgt/../../x.m4a")
    client.head("/abgt.xml")                                      # HEAD
    client.head("/files/abgt/ep1.m4a")
    first = get(client, "/files/abgt/ep1.m4a", Range="bytes=0-9")  # counted...
    get(client, "/files/abgt/ep1.m4a", **{"If-None-Match": first.headers["etag"]})  # ...304 is not
    get(client, "/files/abgt/ep1.square.jpg")                     # artwork
    get(client, "/files/abgt/ep1.chapters.json")                  # chapters
    get(client, "/healthz")
    get(client, "/static/style.css")
    get(client, "/login")
    get(client, "/dashboard")                                     # redirect, admin
    get(client, "/")
    assert table(client) == [("abgt", "v1", "file", "Overcast", 1, 10)]


def test_admin_activity_is_not_counted(client):
    do_login(client)
    for path in ("/dashboard", "/podcasts", "/settings", "/podcasts/abgt", "/settings/import"):
        assert client.get(path).status_code == 200
    csrf = csrf_of(client)
    client.post("/podcasts/abgt/pause", data={"csrf_token": csrf})
    assert table(client) == []


def test_groups_are_separated(client):
    get(client, "/abgt.xml", ua="Spotify/1.0")
    get(client, "/abgt.xml", ua="curl/8")
    get(client, "/abgt.xml", ua="")
    groups = sorted(r[3] for r in table(client))
    assert groups == ["Other", "Spotify"]
    assert [r for r in table(client) if r[3] == "Other"][0][4] == 2


def test_no_ip_address_or_user_agent_anywhere_in_the_database(client):
    get(client, "/abgt.xml", ua=SECRET_UA, **{"X-Forwarded-For": "203.0.113.77"})
    get(client, "/files/abgt/ep1.m4a", ua=SECRET_UA, **{"X-Forwarded-For": "203.0.113.77", "Referer": "https://spy.example/"})
    conn = connect(client.db)
    dump = "\n".join(conn.iterdump())
    conn.close()
    for needle in ("SecretDevice", "XYZ-987", "203.0.113.77", "testclient", "127.0.0.1", "spy.example", "3.0 "):
        assert needle not in dump, needle
    # and the table has no column that could hold them
    conn = connect(client.db)
    cols = [r[1] for r in conn.execute("PRAGMA table_info(hits_daily)")]
    conn.close()
    assert cols == ["day", "podcast_id", "episode_id", "kind", "app_group", "count", "bytes"]


def test_counting_failure_never_breaks_serving(client):
    conn = connect(client.db)
    conn.execute("DROP INDEX hits_daily_key")  # the upsert can no longer work
    conn.close()
    r = get(client, "/abgt.xml")
    assert r.status_code == 200 and "<rss" in r.text
    assert get(client, "/files/abgt/ep1.m4a").content == AUDIO


def test_security_headers_still_applied(client):
    r = get(client, "/abgt.xml")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "content-security-policy" not in r.headers
    do_login(client)
    h = client.get("/dashboard").headers
    assert h["cache-control"] == "no-store" and "default-src 'self'" in h["content-security-policy"]


def test_large_download_streams_unmodified(client, tmp_path):
    big = bytes(range(256)) * 20000  # ~5 MB
    (client.app.state.config.media_dir / "abgt" / "ep1.m4a").write_bytes(big)
    r = get(client, "/files/abgt/ep1.m4a")
    assert r.content == big
    assert table(client) == [("abgt", "v1", "file", "Overcast", 1, len(big))]
