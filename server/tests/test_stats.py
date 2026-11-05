from datetime import date, datetime, timedelta, timezone

import pytest

from app import app_groups, hits, podcasts, settings, stats
from app.db import connect
from app.migrations import migrate

from seed import seed_hits, seed_podcasts


@pytest.fixture
def conn(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    c = connect(tmp_path / "p.db")
    yield c
    c.close()


def rows(conn):
    return [tuple(r) for r in conn.execute(
        "SELECT day, podcast_id, episode_id, kind, app_group, count, bytes FROM hits_daily ORDER BY 1,2,3,4,5")]


# --- app group mapping -------------------------------------------------------

@pytest.mark.parametrize("ua,group", [
    ("Podcasts/1530.1 CFNetwork/1331.0.7 Darwin/21.4.0", "Apple Podcasts"),
    ("AppleCoreMedia/1.0.0.19H349 (iPhone; U; CPU OS 15_7 like Mac OS X; en_us)", "Apple Podcasts"),
    ("iTunes/12.12.4 (Windows; Microsoft Windows 10 x64)", "Apple Podcasts"),
    ("Overcast/3.0 (+http://overcast.fm/; iOS podcast app)", "Overcast"),
    ("PocketCasts/1.0 (Pocket Casts Feed Parser; +http://pocketcasts.com/)", "Pocket Casts"),
    ("Pocket Casts", "Pocket Casts"),
    ("Spotify/1.0", "Spotify"),
    ("Mozilla/5.0 (Windows NT 10.0) Chrome/120 Safari/537 Spotify/1.2.3", "Spotify"),  # app rules beat browser rule
    ("AntennaPod/3.2.0", "AntennaPod"),
    ("Podcast Addict/v5 (+http://podcastaddict.com/)", "Podcast Addict"),
    ("CastBox/9.9 (iPhone)", "Castbox"),
    ("Castro 2", "Castro"),
    ("Downcast/2.9.40", "Downcast"),
    ("Googlebot/2.1 (+http://www.google.com/bot.html)", "Bots"),
    ("Mozilla/5.0 (compatible; bingbot/2.0; +http://www.bing.com/bingbot.htm)", "Bots"),
    ("Mozilla/5.0+(compatible; UptimeRobot/2.0; http://www.uptimerobot.com/)", "Bots"),
    ("facebookexternalhit/1.1", "Bots"),
    ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/120.0 Safari/537.36", "Browsers"),
    ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Gecko/20100101 Firefox/121.0", "Browsers"),
    ("curl/8.4.0", "Other"), ("python-requests/2.31", "Other"), ("okhttp/4.12.0", "Other"), ("", "Other"), (None, "Other"),
])
def test_app_group_mapping(ua, group):
    assert app_groups.classify(ua) == group


def test_app_group_is_case_insensitive_and_extensible():
    assert app_groups.classify("OVERCAST/3") == "Overcast"
    assert all(isinstance(g, str) and p.pattern for g, p in app_groups.RULES)


# --- classification and counting rule ----------------------------------------

@pytest.mark.parametrize("path,expected", [
    ("/abgt.xml", ("feed", "abgt", None)),
    ("/a-b-1.xml", ("feed", "a-b-1", None)),
    ("/files/abgt/Ep 1.m4a", ("file", "abgt", "Ep 1.m4a")),
    ("/files/abgt/Ep.M4A", ("file", "abgt", "Ep.M4A")),
    ("/files/abgt/Ep.square.jpg", None), ("/files/abgt/Ep.chapters.json", None),   # not downloads
    ("/healthz", None), ("/", None), ("/login", None), ("/dashboard", None), ("/podcasts", None),
    ("/static/style.css", None), ("/settings/import", None), ("/Bad_Slug.xml", None), ("/files/abgt/a/b.m4a", None),
])
def test_classify(path, expected):
    assert hits.classify(path) == expected


@pytest.mark.parametrize("header,expected", [
    (None, True), ("", True), ("bytes=0-", True), ("bytes=0-1", True), ("bytes=0-99,200-299", True),
    ("bytes=100-", False), ("bytes=100-199", False), ("bytes=-500", False), ("bytes=1-", False),
    ("BYTES=0-", True), ("items=5-", True),  # not a byte range: the server ignores it, a full download
])
def test_download_counted_once_rule(header, expected):
    assert hits.starts_download(header) is expected


# --- upsert --------------------------------------------------------------------

def test_upsert_accumulates_per_dimension(conn):
    d = date(2026, 1, 1)
    kw = dict(day=d, podcast_id="abgt", kind="feed", app_group="Overcast")
    stats.record_hit(conn, episode_id=None, count=1, nbytes=100, **kw)
    stats.record_hit(conn, episode_id=None, count=1, nbytes=50, **kw)         # NULL episode must collide with itself
    stats.record_hit(conn, episode_id=None, count=1, nbytes=7, **{**kw, "app_group": "Spotify"})
    stats.record_hit(conn, episode_id=None, count=1, nbytes=7, **{**kw, "day": date(2026, 1, 2)})
    stats.record_hit(conn, episode_id="v1", count=1, nbytes=9, **{**kw, "kind": "file"})
    stats.record_hit(conn, episode_id="v1", count=0, nbytes=9, **{**kw, "kind": "file"})  # later range: bytes only
    assert rows(conn) == [
        ("2026-01-01", "abgt", None, "feed", "Overcast", 2, 150),
        ("2026-01-01", "abgt", None, "feed", "Spotify", 1, 7),
        ("2026-01-01", "abgt", "v1", "file", "Overcast", 1, 18),
        ("2026-01-02", "abgt", None, "feed", "Overcast", 1, 7),
    ]


# --- pruning -------------------------------------------------------------------

def test_prune_removes_only_old_rows(conn):
    today = date(2026, 11, 5)
    for back in (0, 100, 729, 730, 731, 2000):
        stats.record_hit(conn, day=today - timedelta(days=back), podcast_id="p", episode_id=None, kind="feed",
                         app_group="Other", count=1, nbytes=1)
    assert stats.prune(conn, 730, today) == 2  # 731 and 2000 days ago
    left = [r[0] for r in rows(conn)]
    assert (today - timedelta(days=730)).isoformat() in left and len(left) == 4
    assert stats.prune(conn, 730, today) == 0


def test_prune_once_uses_retention_setting(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    c = connect(tmp_path / "p.db")
    stats.record_hit(c, day=stats.today() - timedelta(days=40), podcast_id="p", episode_id=None, kind="feed",
                     app_group="Other", count=1, nbytes=1)
    stats.record_hit(c, day=stats.today() - timedelta(days=10), podcast_id="p", episode_id=None, kind="feed",
                     app_group="Other", count=1, nbytes=1)
    assert stats.prune_once(tmp_path / "p.db") == 0  # default: 2 years
    settings.save(c, {"stats_retention_days": 30})
    assert stats.prune_once(tmp_path / "p.db") == 1
    assert len(rows(c)) == 1
    c.close()


def test_default_retention_is_two_years(conn):
    assert settings.get(conn, "stats_retention_days") == 730


def test_seconds_until_next_run():
    now = datetime(2026, 11, 5, 2, 0, tzinfo=timezone.utc)
    assert stats.seconds_until(3, 30, now) == 90 * 60
    assert stats.seconds_until(3, 30, datetime(2026, 11, 5, 3, 30, tzinfo=timezone.utc)) == 24 * 3600
    assert stats.seconds_until(3, 30, datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc)) == 4.5 * 3600


# --- queries -------------------------------------------------------------------

def test_daily_series_fills_gaps(conn):
    today = date(2026, 11, 5)
    stats.record_hit(conn, day=today, podcast_id="p", episode_id=None, kind="feed", app_group="A", count=3, nbytes=30)
    stats.record_hit(conn, day=today, podcast_id="q", episode_id="e", kind="file", app_group="A", count=2, nbytes=500)
    stats.record_hit(conn, day=today - timedelta(days=29), podcast_id="p", episode_id="e", kind="file", app_group="B",
                     count=1, nbytes=5)
    stats.record_hit(conn, day=today - timedelta(days=30), podcast_id="p", episode_id="e", kind="file", app_group="B",
                     count=9, nbytes=5)  # outside the window
    s = stats.daily_series(conn, 30, today)
    assert len(s) == 30 and s[0]["day"] == today - timedelta(days=29) and s[-1]["day"] == today
    assert (s[-1]["feed"], s[-1]["file"], s[-1]["bytes"]) == (3, 2, 530)
    assert (s[0]["file"], s[0]["bytes"]) == (1, 5)
    assert all(r["feed"] == r["file"] == 0 for r in s[1:-1])


def test_top_episodes_and_app_shares(conn):
    seed_podcasts(conn, ("abgt",), 3)
    today = date(2026, 11, 5)
    for ep, n in (("abgt0", 5), ("abgt1", 9), ("abgt2", 0)):
        stats.record_hit(conn, day=today, podcast_id="abgt", episode_id=ep, kind="file", app_group="Overcast",
                         count=n, nbytes=1)
    stats.record_hit(conn, day=today - timedelta(days=45), podcast_id="abgt", episode_id="abgt2", kind="file",
                     app_group="Overcast", count=99, nbytes=1)
    top = stats.top_episodes(conn, 30, 10, today)
    assert [(t["title"], t["count"]) for t in top] == [("ABGT Episode 1", 9), ("ABGT Episode 0", 5)]  # no zero rows, no old
    assert stats.app_shares(conn, "file", 30, today) == [("Overcast", 14)]
    assert stats.app_shares(conn, "feed", 30, today) == []


def test_fold_tail_merges_small_groups_into_other():
    rows_ = [(f"g{i}", 100 - i) for i in range(12)] + [("Other", 3)]
    folded = stats.fold_tail(rows_, keep=7)
    assert len(folded) == 8 and folded[-1][0] == "Other"
    assert folded[-1][1] == sum(n for _, n in rows_[7:12]) + 3
    assert stats.fold_tail([("a", 1), ("b", 2)]) == [("b", 2), ("a", 1)]


def test_storage_overview(conn, tmp_path):
    seed_podcasts(conn, ("abgt", "asot"), 2)
    conn.execute("UPDATE episodes SET state = 'hidden' WHERE video_id = 'asot0'")
    o = stats.storage_overview(conn, tmp_path)
    by_id = {p["id"]: p for p in o["podcasts"]}
    assert by_id["abgt"]["episodes"] == 2 and by_id["asot"]["episodes"] == 1   # only available episodes
    assert o["total_episodes"] == 3 and o["disk"]["free"] > 0 and o["disk"]["total"] >= o["disk"]["free"]


# --- seed script + speed -------------------------------------------------------

def test_seeded_database_queries_are_fast(conn):
    import time
    seed_podcasts(conn)
    n = seed_hits(conn, days=730)
    assert n > 50_000
    started = time.perf_counter()
    stats.daily_series(conn)
    stats.top_episodes(conn)
    stats.app_shares(conn, "file")
    stats.app_shares(conn, "feed")
    assert time.perf_counter() - started < 0.5
