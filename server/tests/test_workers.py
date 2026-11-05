import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from app import auth, settings, workers
from app.db import connect
from app.migrations import migrate

DEFAULTS = {"pause_min_s": 5, "pause_max_s": 20}


@pytest.fixture
def conn(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    c = connect(tmp_path / "p.db")
    yield c
    c.close()


def form(**over):
    return {"name": "home", "tier": "2", "active": "1", "pause_min_s": "", "pause_max_s": "",
            "downloads_per_hour": "", **over}


# --- tokens ---------------------------------------------------------------------

def test_token_format_and_entropy():
    tokens = {workers.new_token() for _ in range(200)}
    assert len(tokens) == 200
    t = next(iter(tokens))
    assert t.startswith("pnw_") and len(t) - 4 >= 43  # 32 random bytes, URL-safe base64


def test_token_is_stored_only_as_hash(conn):
    worker_id, token = workers.create(conn, workers.validate(form(), DEFAULTS)[0])
    row = workers.get(conn, worker_id)
    assert row["token_hash"] == workers.hash_token(token) and len(row["token_hash"]) == 64
    assert token not in row["token_hash"]
    dump = "\n".join(conn.iterdump())
    assert token not in dump and token[4:] not in dump


def test_find_by_token(conn):
    worker_id, token = workers.create(conn, workers.validate(form(), DEFAULTS)[0])
    assert workers.find_by_token(conn, token)["id"] == worker_id
    for wrong in (token + "x", token[:-1], token.upper(), "", "pnw_", workers.hash_token(token)):
        assert workers.find_by_token(conn, wrong) is None


def test_regenerate_invalidates_old_token(conn):
    worker_id, old = workers.create(conn, workers.validate(form(), DEFAULTS)[0])
    new = workers.regenerate_token(conn, worker_id)
    assert new != old
    assert workers.find_by_token(conn, old) is None and workers.find_by_token(conn, new)["id"] == worker_id
    assert workers.regenerate_token(conn, 999) is None


# --- validation / CRUD ------------------------------------------------------------

def test_valid_form():
    data, errors = workers.validate(form(), DEFAULTS)
    assert errors == {} and data["tier"] == 2 and data["active"] == 1 and data["pause_min_s"] is None


@pytest.mark.parametrize("field,value", [
    ("name", ""), ("name", " "), ("name", "-bad"), ("name", "a/b"), ("name", "x" * 61), ("name", "<script>"),
    ("tier", "0"), ("tier", "100"), ("tier", "-1"), ("tier", "abc"), ("pause_min_s", "-1"), ("pause_max_s", "3601"),
    ("downloads_per_hour", "0"), ("downloads_per_hour", "x"),
])
def test_invalid_fields(field, value):
    _, errors = workers.validate(form(**{field: value}), DEFAULTS)
    assert field in errors


def test_pause_comparison_uses_defaults():
    assert "pause_max_s" in workers.validate(form(pause_min_s="30"), DEFAULTS)[1]
    assert "pause_max_s" in workers.validate(form(pause_min_s="9", pause_max_s="3"), DEFAULTS)[1]
    assert workers.validate(form(pause_min_s="9"), DEFAULTS)[1] == {}


def test_empty_tier_defaults_to_one():
    assert workers.validate(form(tier=""), DEFAULTS)[0]["tier"] == 1


def test_unique_name_is_case_insensitive(conn):
    workers.create(conn, workers.validate(form(name="Home"), DEFAULTS)[0])
    with pytest.raises(workers.NameExists):
        workers.create(conn, workers.validate(form(name="home"), DEFAULTS)[0])
    other, _ = workers.create(conn, workers.validate(form(name="other"), DEFAULTS)[0])
    with pytest.raises(workers.NameExists):
        workers.update(conn, other, workers.validate(form(name="HOME"), DEFAULTS)[0])


def test_update_activate_delete(conn):
    worker_id, token = workers.create(conn, workers.validate(form(), DEFAULTS)[0])
    assert workers.update(conn, worker_id, workers.validate(form(name="renamed", tier="3", pause_min_s="1"), DEFAULTS)[0])
    row = workers.get(conn, worker_id)
    assert (row["name"], row["tier"], row["pause_min_s"]) == ("renamed", 3, 1)
    assert workers.find_by_token(conn, token)  # editing keeps the token
    assert workers.set_active(conn, worker_id, False) and not workers.get(conn, worker_id)["active"]
    assert workers.delete(conn, worker_id) and workers.get(conn, worker_id) is None
    assert workers.find_by_token(conn, token) is None
    assert not workers.delete(conn, worker_id)


def test_database_constraints(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO workers (name, token_hash, tier, created_at, updated_at) VALUES ('x', 'h', 0, 'a', 'a')")


# --- online / offline -----------------------------------------------------------------

def test_status_calculation(conn):
    worker_id, _ = workers.create(conn, workers.validate(form(), DEFAULTS)[0])
    now = datetime(2026, 11, 5, 12, 0, 0, tzinfo=timezone.utc)
    row = workers.get(conn, worker_id)
    assert workers.status(row, 120, now) == "never" and workers.seconds_since_seen(row, now) is None
    for seconds, expected in ((0, "online"), (119, "online"), (120, "online"), (121, "offline"), (3600, "offline")):
        workers.touch(conn, worker_id, now - timedelta(seconds=seconds))
        assert workers.status(workers.get(conn, worker_id), 120, now) == expected, seconds
    assert workers.status(workers.get(conn, worker_id), 3700, now) == "online"  # threshold is configurable
    workers.set_active(conn, worker_id, False)
    assert workers.status(workers.get(conn, worker_id), 120, now) == "inactive"


def test_online_threshold_setting(conn):
    assert settings.get(conn, "worker_online_seconds") == 120


def test_record_hello_roundtrip(conn):
    worker_id, _ = workers.create(conn, workers.validate(form(), DEFAULTS)[0])
    workers.record_hello(conn, worker_id, "1.2.3", ["ytdlp", "ffmpeg"], {"yt-dlp": "2026.1.1"})
    row = workers.get(conn, worker_id)
    assert row["version"] == "1.2.3" and workers.capabilities_of(row) == ["ytdlp", "ffmpeg"]
    assert workers.tools_of(row) == {"yt-dlp": "2026.1.1"}
    conn.execute("UPDATE workers SET capabilities = 'garbage', tools = 'garbage' WHERE id = ?", (worker_id,))
    row = workers.get(conn, worker_id)
    assert workers.capabilities_of(row) == [] and workers.tools_of(row) == {}


# --- rate limiter ---------------------------------------------------------------------

def test_rate_limiter_window():
    now = [0.0]
    lim = auth.RateLimiter(limit=3, window=60, clock=lambda: now[0])
    assert [lim.allow("a") for _ in range(4)] == [True, True, True, False]
    assert lim.allow("b")                      # keys are independent
    now[0] = 61
    assert lim.allow("a")                      # the window slid
