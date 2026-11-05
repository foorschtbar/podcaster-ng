import importlib.util
import threading
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "compare_feeds.py"
pytestmark = pytest.mark.skipif(not SCRIPT.exists(), reason="scripts/ not available")


def load():
    spec = importlib.util.spec_from_file_location("compare_feeds", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def feed(base, items):
    body = "".join(f'<item><guid>{base}/files/a/{n}.m4a</guid><enclosure url="{base}/files/a/{n}.m4a" '
                   f'length="{ln}" type="audio/x-m4a"/></item>' for n, ln in items)
    return f'<?xml version="1.0"?><rss version="2.0"><channel><title>x</title>{body}</channel></rss>'


class Handler(BaseHTTPRequestHandler):
    def __init__(self, feeds, *args):
        self.feeds = feeds
        super().__init__(*args)

    def do_GET(self):
        body = self.feeds.get(self.path)
        if body is None:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.end_headers(); self.wfile.write(body.encode())

    def log_message(self, *a):
        pass


@pytest.fixture
def servers():
    started = []

    def start(feeds):
        srv = HTTPServer(("127.0.0.1", 0), partial(Handler, feeds))
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        started.append(srv)
        return f"http://127.0.0.1:{srv.server_port}", srv

    yield start
    for s in started:
        s.shutdown()


def test_identical_across_different_hosts(servers, capsys):
    cf = load()
    # each side's feed refers to its own base URL; the comparison ignores the host
    holder_old, holder_new = {}, {}
    old_base, _ = servers(holder_old)
    new_base, _ = servers(holder_new)
    holder_old["/a.xml"] = feed(old_base, [("one", 10), ("two", 20)])
    holder_new["/a.xml"] = feed(new_base, [("two", 20), ("one", 10)])  # different order is only a note
    assert cf.main([old_base, new_base, "a"]) == 0
    out = capsys.readouterr().out
    assert "identical: 2 items" in out and "different order" in out


def test_differences_are_reported(servers, capsys):
    cf = load()
    old_f, new_f = {}, {}
    old_base, _ = servers(old_f)
    new_base, _ = servers(new_f)
    old_f["/a.xml"] = feed(old_base, [("one", 10), ("two", 20), ("gone", 5)])
    new_f["/a.xml"] = feed(new_base, [("one", 11), ("two", 20), ("extra", 5)])
    assert cf.main([old_base, new_base, "a"]) == 1
    out = capsys.readouterr().out
    assert "item count" not in out  # same count
    assert "missing in new: /files/a/gone.m4a" in out and "only in new: /files/a/extra.m4a" in out
    assert "length differs for /files/a/one.m4a" in out


def test_count_mismatch_and_fetch_errors(servers, capsys):
    cf = load()
    old_f, new_f = {}, {}
    old_base, _ = servers(old_f)
    new_base, _ = servers(new_f)
    old_f["/a.xml"] = feed(old_base, [("one", 10)])
    new_f["/a.xml"] = feed(new_base, [])
    assert cf.main([old_base, new_base, "a"]) == 1
    assert "item count differs: old 1, new 0" in capsys.readouterr().out
    assert cf.main([old_base, new_base, "nope"]) == 2
    assert "ERROR" in capsys.readouterr().out
