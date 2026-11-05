import re
import time
from datetime import date, timedelta

import pytest

from app import charts, stats
from app.db import connect

from helpers import do_login, make_client
from seed import seed_hits, seed_podcasts


@pytest.fixture
def c(tmp_path):
    with make_client(tmp_path) as client:
        do_login(client)
        client.db = client.app.state.config.db_path
        yield client


# --- chart geometry ------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [(0, 1), (1, 1), (2, 2), (3, 4), (5, 6), (7, 8), (9, 10), (11, 20),
                                            (45, 60), (81, 100), (1234, 2000), (99999, 100000)])
def test_nice_max(value, expected):
    assert charts.nice_max(value) == expected
    assert charts.nice_max(value) % 2 == 0 or charts.nice_max(value) == 1


def test_ticks_are_whole_numbers():
    for v in (0, 1, 2, 7, 33, 1234, 98765):
        top = charts.nice_max(v)
        assert all(t == int(t) for t in charts.ticks_for(top))


def days(n=30, end=date(2026, 11, 5)):
    return [end - timedelta(days=n - 1 - i) for i in range(n)]


def test_column_chart_marks_follow_the_specs():
    values = [0, 5, 10] + [3] * 27
    svg = str(charts.column_chart(days(), values, label="Downloads", unit="downloads"))
    assert svg.count('class="bar"') == 29                       # zero days draw no bar
    assert svg.count('class="grid"') == 2 and svg.count('class="axis"') == 1   # hairline, solid, no dashes
    assert "dasharray" not in svg and "stroke-dash" not in svg
    # bars are at most 24 px thick and keep a 2 px gap
    slot = charts.PLOT_W / 30
    assert charts.MAX_BAR >= min(charts.MAX_BAR, slot - charts.GAP) and slot - charts.GAP < 24
    paths = re.findall(r'class="bar" data-i="\d+" d="([^"]+)"', svg)
    assert all(" a" in p and p.startswith("M") for p in paths)  # rounded data end
    assert 'role="img"' in svg and 'aria-label="Downloads"' in svg and 'tabindex="0"' in svg
    assert 'fill="none" pointer-events="all"' in svg           # hit layer covers the plot
    assert "7 Oct" in svg and "5 Nov" in svg                    # first and last day labelled


def test_column_chart_all_zero_and_single_value():
    svg = str(charts.column_chart(days(), [0] * 30, label="x", unit="u"))
    assert 'class="bar"' not in svg and "<svg" in svg
    one = str(charts.column_chart(days(), [0] * 29 + [1], label="x", unit="u"))
    assert one.count('class="bar"') == 1


def test_bar_never_exceeds_plot_area():
    svg = str(charts.column_chart(days(), [7] * 30, label="x", unit="u"))
    ys = [float(m) for m in re.findall(r'V([\d.]+) a', svg)]
    assert all(charts.TOP <= y <= charts.TOP + charts.PLOT_H for y in ys)


def test_rank_bar_and_meter():
    assert 'width="100.00%"' in str(charts.rank_bar(1.0))
    assert 'width="1.50%"' in str(charts.rank_bar(0.0))         # tiny values stay visible
    assert 'class="fill ok"' in str(charts.meter(0.5))
    assert 'class="fill warn"' in str(charts.meter(0.85))
    assert 'class="fill critical"' in str(charts.meter(0.97))


def test_chart_data_attribute_is_escaped_json():
    svg = str(charts.column_chart(days(), [1] * 30, label='a"b<c', unit='x"y'))
    assert 'a&quot;b&lt;c' in svg and 'x&quot;y' in svg and '<c' not in svg


# --- dashboard page --------------------------------------------------------------

def test_empty_database_renders(c):
    r = c.get("/dashboard")
    assert r.status_code == 200
    for text in ("Downloads per day", "Feed requests per day", "Top episodes", "No downloads in the last 30 days",
                 "Disk space", "No podcasts yet", "No data yet. Worker status", "Table view"):
        assert text in r.text, text
    assert "col-chart" in r.text and "/static/dashboard.js" in r.text


def test_populated_database_renders_and_shows_numbers(c):
    conn = connect(c.db)
    seed_podcasts(conn)
    seed_hits(conn, days=60)
    conn.close()
    r = c.get("/dashboard")
    assert r.status_code == 200
    assert "ABGT Episode" in r.text or "ASOT Episode" in r.text
    for group in ("Apple Podcasts", "Overcast", "Pocket Casts"):
        assert group in r.text
    assert "Downloads by app" in r.text and "Feed requests by app" in r.text
    assert r.text.count('class="bar"') > 30
    assert "40 episodes" in r.text and "ABGT" in r.text          # storage per podcast
    assert "peak" in r.text
    assert re.search(r"<th scope=\"row\">\d{4}-\d{2}-\d{2}</th>", r.text)  # table view twin


def test_values_in_page_match_the_database(c):
    conn = connect(c.db)
    seed_podcasts(conn, ("abgt",), 3)
    for i in range(4):
        stats.record_hit(conn, day=stats.today() - timedelta(days=i), podcast_id="abgt", episode_id="abgt0",
                         kind="file", app_group="Overcast", count=10, nbytes=1000)
    stats.record_hit(conn, day=stats.today(), podcast_id="abgt", episode_id=None, kind="feed", app_group="Spotify",
                     count=7, nbytes=70)
    conn.close()
    html = c.get("/dashboard").text
    assert re.search(r"Downloads, last 30 days</div><div class=\"tile-value\">40<", html)
    assert re.search(r"Feed requests, last 30 days</div><div class=\"tile-value\">7<", html)
    assert "Transferred, last 30 days</div><div class=\"tile-value\">4.0 KB" in html
    assert "peak 10" in html


def test_dashboard_escapes_episode_titles(c):
    conn = connect(c.db)
    seed_podcasts(conn, ("abgt",), 1)
    conn.execute("UPDATE episodes SET title = '<script>alert(1)</script>'")
    stats.record_hit(conn, day=stats.today(), podcast_id="abgt", episode_id="abgt0", kind="file",
                     app_group="Other", count=1, nbytes=1)
    conn.close()
    html = c.get("/dashboard").text
    assert "<script>alert(1)</script>" not in html and "&lt;script&gt;" in html


def test_dashboard_is_fast_with_two_years_of_data(c):
    conn = connect(c.db)
    seed_podcasts(conn)
    rows = seed_hits(conn, days=730)
    conn.close()
    assert rows > 50_000
    started = time.perf_counter()
    r = c.get("/dashboard")
    elapsed = time.perf_counter() - started
    assert r.status_code == 200 and elapsed < 1.0, elapsed


def test_dashboard_requires_login(tmp_path):
    with make_client(tmp_path) as anon:
        assert anon.get("/dashboard").status_code == 303


def test_dashboard_script_is_csp_compatible(c):
    csp = c.get("/dashboard").headers["content-security-policy"]
    assert "script-src" not in csp and "default-src 'self'" in csp          # only same-origin scripts run
    html = c.get("/dashboard").text
    assert not re.search(r"<script(?![^>]*\ssrc=)[^>]*>", html)               # no inline scripts
    assert ' style="' not in html                                              # no inline styles
    js = c.get("/static/dashboard.js").text
    assert "innerHTML" not in js and "textContent" in js


def test_retention_setting_in_ui(c):
    page = c.get("/settings").text
    assert "Keep daily hit counters for (days)" in page and 'value="730"' in page
