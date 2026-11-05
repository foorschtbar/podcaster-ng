import logging
from datetime import timedelta

import httpx
import pytest

from app import notifications, scheduler, telegram, watchdog, workers
from app.db import connect

from qhelpers import NOW, add_podcast, add_worker, make_db
from tghelpers import CHAT, TOKEN, FakeTelegram, configure


@pytest.fixture
def conn(tmp_path):
    c = connect(make_db(tmp_path))
    yield c
    c.close()


def rows(conn):
    return [dict(r) for r in conn.execute("SELECT * FROM notifications ORDER BY id")]


# --- the Telegram client -------------------------------------------------------------------------------------

def test_send_message_request_shape():
    fake = FakeTelegram()
    telegram.send_message(TOKEN, CHAT, "hello <b>& world</b>", fake.transport)
    (call,) = fake.calls
    assert call["path"] == f"/bot{TOKEN}/sendMessage"
    assert call["body"] == {"chat_id": CHAT, "text": "hello <b>& world</b>", "disable_web_page_preview": True}   # plain text


def test_long_messages_are_cut_to_telegrams_limit():
    fake = FakeTelegram()
    telegram.send_message(TOKEN, CHAT, "x" * 9000, fake.transport)
    assert len(fake.calls[0]["body"]["text"]) == 4096


@pytest.mark.parametrize("token,chat", [("", CHAT), (TOKEN, ""), ("", "")])
def test_not_configured_is_a_permanent_error_and_sends_nothing(token, chat):
    fake = FakeTelegram()
    with pytest.raises(telegram.TelegramError) as exc:
        telegram.send_message(token, chat, "x", fake.transport)
    assert exc.value.permanent and fake.calls == []


@pytest.mark.parametrize("status,permanent", [(400, True), (401, True), (403, True), (404, True), (500, False), (502, False)])
def test_error_statuses(status, permanent):
    fake = FakeTelegram(lambda c: httpx.Response(status, json={"ok": False, "description": "Unauthorized"}))
    with pytest.raises(telegram.TelegramError) as exc:
        telegram.send_message(TOKEN, CHAT, "x", fake.transport)
    assert exc.value.permanent is permanent and f"{status}" in str(exc.value) and "Unauthorized" in str(exc.value)


def test_rate_limit_carries_retry_after():
    fake = FakeTelegram(lambda c: httpx.Response(429, json={"ok": False, "description": "Too Many Requests", "parameters": {"retry_after": 17}}))
    with pytest.raises(telegram.TelegramError) as exc:
        telegram.send_message(TOKEN, CHAT, "x", fake.transport)
    assert exc.value.retry_after == 17 and not exc.value.permanent


def test_the_token_never_appears_in_errors():
    def echo(call):
        return httpx.Response(401, json={"ok": False, "description": f"bad token {TOKEN} for {call['url']}"})

    with pytest.raises(telegram.TelegramError) as exc:
        telegram.send_message(TOKEN, CHAT, "x", FakeTelegram(echo).transport)
    assert TOKEN not in str(exc.value) and "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in str(exc.value)

    def down(request):
        raise httpx.ConnectError(f"cannot connect to {request.url}")

    with pytest.raises(telegram.TelegramError) as exc:
        telegram.send_message(TOKEN, CHAT, "x", httpx.MockTransport(down))
    assert TOKEN not in str(exc.value) and "ConnectError" in str(exc.value)


def test_nothing_secret_is_logged(caplog):
    caplog.set_level(logging.DEBUG)
    assert logging.getLogger("httpx").level >= logging.WARNING                # httpx would log the URL, which holds the token
    telegram.send_message(TOKEN, CHAT, "x", FakeTelegram().transport)
    with pytest.raises(telegram.TelegramError):
        telegram.send_message(TOKEN, CHAT, "x", FakeTelegram(lambda c: httpx.Response(500)).transport)
    assert TOKEN not in caplog.text and "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in caplog.text


# --- the outbox ---------------------------------------------------------------------------------------------------

def test_enqueue_is_idempotent_per_key(conn):
    assert notifications.enqueue(conn, "k", "a:1", "first", NOW) is True
    assert notifications.enqueue(conn, "k", "a:1", "second", NOW) is False
    assert notifications.enqueue(conn, "k", "a:2", "third", NOW) is True
    assert [r["text"] for r in rows(conn)] == ["first", "third"]


def test_delivery_marks_messages_sent_in_order(conn):
    configure(conn)
    for n in range(3):
        notifications.enqueue(conn, "k", f"a:{n}", f"message {n}", NOW)
    fake = FakeTelegram()
    result = notifications.deliver_pending(conn, NOW, fake.transport)
    assert result["sent"] == 3 and fake.texts == ["message 0", "message 1", "message 2"]
    assert [(r["status"], r["attempts"], r["sent_at"] is not None) for r in rows(conn)] == [("sent", 1, True)] * 3
    assert notifications.deliver_pending(conn, NOW, fake.transport)["sent"] == 0       # nothing is ever sent twice
    assert len(fake.calls) == 3


def test_failed_deliveries_are_retried_with_a_growing_delay(conn):
    configure(conn)
    notifications.enqueue(conn, "k", "a:1", "hello", NOW)
    fake = FakeTelegram(lambda c: httpx.Response(502))
    t = NOW
    delays = []
    for _ in range(4):
        notifications.deliver_pending(conn, t, fake.transport)
        r = rows(conn)[0]
        delays.append(int((notifications_parse(r["next_try_at"]) - t).total_seconds()))
        assert r["status"] == "queued" and "502" in r["last_error"]
        assert notifications.deliver_pending(conn, t + timedelta(seconds=5), fake.transport)["retry"] == 0   # not due yet
        t = notifications_parse(r["next_try_at"])
    assert delays == [60, 120, 240, 480]
    assert len(fake.calls) == 4


def notifications_parse(stamp):
    from app.queue import parse
    return parse(stamp)


def test_a_message_that_finally_gets_through_is_sent_once(conn):
    configure(conn)
    notifications.enqueue(conn, "k", "a:1", "hello", NOW)
    state = {"n": 0}

    def flaky(call):
        state["n"] += 1
        return httpx.Response(500) if state["n"] < 3 else httpx.Response(200, json={"ok": True})

    fake = FakeTelegram(flaky)
    t = NOW
    for _ in range(5):
        notifications.deliver_pending(conn, t, fake.transport)
        t += timedelta(hours=1)
    r = rows(conn)[0]
    assert r["status"] == "sent" and r["attempts"] == 3 and r["last_error"] == "" and len(fake.calls) == 3


def test_giving_up_after_the_maximum_attempts(conn):
    configure(conn)
    notifications.enqueue(conn, "k", "a:1", "hello", NOW)
    fake = FakeTelegram(lambda c: httpx.Response(401, json={"description": "Unauthorized"}))
    t = NOW
    for _ in range(notifications.MAX_ATTEMPTS + 3):
        notifications.deliver_pending(conn, t, fake.transport)
        t += timedelta(hours=2)
    assert rows(conn)[0]["status"] == "failed" and len(fake.calls) == notifications.MAX_ATTEMPTS


def test_retry_after_from_telegram_is_respected(conn):
    configure(conn)
    notifications.enqueue(conn, "k", "a:1", "hello", NOW)
    fake = FakeTelegram(lambda c: httpx.Response(429, json={"description": "slow down", "parameters": {"retry_after": 90}}))
    notifications.deliver_pending(conn, NOW, fake.transport)
    assert notifications_parse(rows(conn)[0]["next_try_at"]) == NOW + timedelta(seconds=90)


def test_without_configuration_messages_are_skipped_and_not_kept(conn):
    notifications.enqueue(conn, "k", "a:1", "hello", NOW)
    fake = FakeTelegram()
    assert notifications.deliver_pending(conn, NOW, fake.transport)["skipped"] == 1
    configure(conn)                                                         # configured later: the old message stays skipped
    notifications.deliver_pending(conn, NOW + timedelta(minutes=5), fake.transport)
    assert fake.calls == [] and rows(conn)[0]["status"] == "skipped"


def test_stored_errors_never_contain_the_token(conn):
    configure(conn)
    notifications.enqueue(conn, "k", "a:1", "hello", NOW)
    fake = FakeTelegram(lambda c: httpx.Response(400, json={"description": f"chat not found ({TOKEN})"}))
    notifications.deliver_pending(conn, NOW, fake.transport)
    stored = rows(conn)[0]
    assert "chat not found" in stored["last_error"]
    assert TOKEN not in stored["last_error"] and "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw" not in stored["last_error"]


def test_the_scheduler_tick_delivers_the_outbox(tmp_path):
    db = make_db(tmp_path)
    c = connect(db)
    configure(c)
    notifications.enqueue(c, "k", "a:1", "from the tick", NOW)
    c.close()
    fake = FakeTelegram()
    assert scheduler.tick(db, transport=fake.transport)["messages"]["sent"] == 1
    assert fake.texts == ["from the tick"]


# --- the worker watchdog -----------------------------------------------------------------------------------------------

LONG_AGO = NOW - timedelta(days=1)


def seen(conn, w, minutes_ago, now=NOW):
    conn.execute("UPDATE workers SET last_seen = ? WHERE id = ?", ((now - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ"), w["id"]))


def check(conn, now, started=LONG_AGO):
    return watchdog.check(conn, now, offline_minutes=15, online_seconds=120, started_at=started)


def kinds(conn):
    return [r["kind"] for r in rows(conn)]


def test_a_worker_gone_for_too_long_is_reported_exactly_once(conn):
    w = add_worker(conn, "home", tier=2)
    seen(conn, w, 10)
    assert check(conn, NOW) == {"offline": 0, "online": 0}                                  # 10 min: still fine
    seen(conn, w, 16)
    assert check(conn, NOW)["offline"] == 1
    assert check(conn, NOW + timedelta(minutes=1))["offline"] == 0                          # not again
    assert check(conn, NOW + timedelta(hours=5))["offline"] == 0
    assert kinds(conn) == ["worker_offline"]
    text = rows(conn)[0]["text"]
    assert "'home'" in text and "tier 2" in text and "offline for 16 min" in text


def test_a_returning_worker_is_reported_once_and_can_go_offline_again(conn):
    w = add_worker(conn, "home")
    seen(conn, w, 20)
    check(conn, NOW)
    seen(conn, w, 0, NOW + timedelta(minutes=30))
    assert check(conn, NOW + timedelta(minutes=30))["online"] == 1
    assert check(conn, NOW + timedelta(minutes=31))["online"] == 0
    assert kinds(conn) == ["worker_offline", "worker_online"]
    assert "back online" in rows(conn)[1]["text"]
    assert conn.execute("SELECT offline_alerted_at FROM workers").fetchone()[0] is None
    # a second outage is a new condition with its own message
    later = NOW + timedelta(hours=2)
    assert check(conn, later)["offline"] == 1
    assert kinds(conn) == ["worker_offline", "worker_online", "worker_offline"]
    assert len({r["key"] for r in rows(conn)}) == 3


def test_a_worker_is_not_reported_back_while_it_is_still_silent(conn):
    w = add_worker(conn, "home")
    seen(conn, w, 20)
    check(conn, NOW)
    assert check(conn, NOW + timedelta(minutes=10))["online"] == 0                          # still offline
    assert kinds(conn) == ["worker_offline"]


def test_the_threshold_is_respected(conn):
    w = add_worker(conn, "home")
    seen(conn, w, 5)
    assert watchdog.check(conn, NOW, offline_minutes=3, online_seconds=120, started_at=LONG_AGO)["offline"] == 1


def test_workers_that_never_connected_or_are_inactive_are_ignored(conn):
    add_worker(conn, "never")
    off = add_worker(conn, "off", active=0)
    seen(conn, off, 600)
    assert check(conn, NOW) == {"offline": 0, "online": 0} and kinds(conn) == []


def test_no_alarm_right_after_a_server_start(conn):
    w = add_worker(conn, "home")
    seen(conn, w, 60)
    assert check(conn, NOW, started=NOW - timedelta(minutes=1)) == {"offline": 0, "online": 0}     # they need a moment to reconnect
    assert check(conn, NOW, started=NOW - timedelta(minutes=4))["offline"] == 1


def test_every_worker_gets_its_own_message(conn):
    a, b = add_worker(conn, "a"), add_worker(conn, "b")
    seen(conn, a, 30)
    seen(conn, b, 40)
    assert check(conn, NOW)["offline"] == 2 and len({r["key"] for r in rows(conn)}) == 2


def test_the_watchdog_through_the_scheduler(tmp_path):
    db = make_db(tmp_path)
    c = connect(db)
    configure(c)
    w = add_worker(c, "home")
    seen(c, w, 60, now=NOW)
    c.close()
    fake = FakeTelegram()
    from datetime import datetime, timezone
    now = NOW
    started = NOW - timedelta(days=1)
    assert scheduler.tick(db, transport=fake.transport, now=now, started_at=started)["alerts"]["offline"] == 1
    scheduler.tick(db, transport=fake.transport, now=now + timedelta(seconds=15), started_at=started)
    assert len(fake.calls) == 1 and "offline" in fake.texts[0]
