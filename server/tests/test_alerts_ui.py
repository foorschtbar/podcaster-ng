import re
from datetime import timedelta

import httpx
import pytest
from fastapi.testclient import TestClient

from app import queue, settings
from app.db import connect
from app.main import create_app

from helpers import csrf_of, do_login, make_config, settings_form
from qhelpers import NOW, add_download, add_podcast, add_worker, claim
from tghelpers import CHAT, TOKEN, FakeTelegram

PLAIN_TOKEN_PART = "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"


@pytest.fixture
def c(tmp_path):
    fake = FakeTelegram()
    config = make_config(tmp_path)
    with TestClient(create_app(config, telegram_transport=fake.transport), base_url="https://testserver", follow_redirects=False) as client:
        do_login(client)
        client.db, client.fake = config.db_path, fake
        conn = connect(config.db_path)
        add_podcast(conn, pause_min_s=0, pause_max_s=0, downloads_per_hour=1000)
        conn.close()
        yield client


def q(c, fn):
    conn = connect(c.db)
    try:
        return fn(conn)
    finally:
        conn.close()


def post(c, path, **fields):
    return c.post(path, data={"csrf_token": csrf_of(c), **fields})


def save_telegram(c, **extra):
    r = post(c, "/settings", **settings_form(telegram_token=TOKEN, telegram_chat_id=CHAT, **extra))
    assert r.status_code == 303, r.text


# --- settings ------------------------------------------------------------------------------------------------------

def test_new_settings_are_shown_with_defaults(c):
    page = c.get("/settings").text
    assert "A paused task reopens by itself after (hours)" in page and 'name="task_reopen_hours" type="text" value="6"' in page
    assert "Message when a worker has been offline for (minutes)" in page and 'name="worker_offline_alert_min" type="text" value="15"' in page
    assert 'type="checkbox" name="telegram_new_episode"' in page and "Send test message" in page
    assert "Counted per task" in page                                                         # the hint for N explains the weights


def test_the_new_episode_checkbox_roundtrip(c):
    assert "checked" not in re.search(r'<input type="checkbox" name="telegram_new_episode"[^>]*>', c.get("/settings").text).group(0)
    save_telegram(c, telegram_new_episode="1")
    assert q(c, lambda conn: settings.get(conn, "telegram_new_episode")) == 1
    assert "checked" in re.search(r'<input type="checkbox" name="telegram_new_episode"[^>]*>', c.get("/settings").text).group(0)
    post(c, "/settings", **settings_form())                                                    # unchecked: the field is absent
    assert q(c, lambda conn: settings.get(conn, "telegram_new_episode")) == 0


@pytest.mark.parametrize("field,value", [("task_reopen_hours", "0"), ("task_reopen_hours", "169"), ("worker_offline_alert_min", "0"),
                                         ("worker_offline_alert_min", "abc"), ("failure_threshold", "0")])
def test_invalid_values_are_refused(c, field, value):
    r = post(c, "/settings", **settings_form(**{field: value}))
    assert r.status_code == 422 and "Nothing was saved" in r.text


# --- the test message ---------------------------------------------------------------------------------------------------

def test_test_message_is_sent_with_the_saved_credentials(c):
    save_telegram(c)
    r = post(c, "/settings/telegram-test")
    assert r.status_code == 200 and "Test message sent." in r.text
    (call,) = c.fake.calls
    assert call["path"] == f"/bot{TOKEN}/sendMessage" and call["body"]["chat_id"] == CHAT and "test message" in call["body"]["text"]
    assert TOKEN not in r.text and PLAIN_TOKEN_PART not in r.text                              # the token is never shown


def test_test_message_without_configuration(c):
    r = post(c, "/settings/telegram-test")
    assert r.status_code == 502 and "not configured" in r.text and c.fake.calls == []


def test_test_message_failure_is_explained_without_leaking_the_token(c):
    save_telegram(c)
    c.fake.respond = lambda call: httpx.Response(401, json={"ok": False, "description": f"Unauthorized {TOKEN}"})
    r = post(c, "/settings/telegram-test")
    assert r.status_code == 502 and "could not be sent" in r.text and "401" in r.text and "Unauthorized" in r.text
    assert TOKEN not in r.text and PLAIN_TOKEN_PART not in r.text


def test_test_message_needs_login_and_csrf(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://testserver", follow_redirects=False) as anon:
        assert anon.post("/settings/telegram-test").status_code == 303
        do_login(anon)
        assert anon.post("/settings/telegram-test").status_code == 403


# --- tasks pages -------------------------------------------------------------------------------------------------------------

def paused_task(c):
    def make(conn):
        a, b = add_worker(conn, "local", tier=1, pause_min_s=0), add_worker(conn, "home", tier=2, pause_min_s=0)
        tid = add_download(conn, "abgt", "zPMoD167_14", now=NOW + timedelta(minutes=44), title="Group Therapy 701")
        t = NOW
        for w in (a, b, a, b):
            t += timedelta(minutes=45)
            claim(conn, w, now=t)
            queue.fail(conn, tid, w["id"], "bot_check", "Sign in to confirm you’re not a bot", now=t)
        return tid
    return q(c, make)


def test_paused_tasks_are_highlighted_in_the_list(c):
    tid = paused_task(c)
    page = c.get("/tasks").text
    assert 'class="card item paused"' in page and 'class="badge warn">paused' in page
    assert "<strong>Paused:</strong> 4 failed attempts from 2 worker(s)" in page
    assert f"#{tid} " in c.get("/tasks?state=paused").text and "No tasks" in c.get("/tasks?state=claimed").text


def test_task_details_show_the_escalation_history_and_the_pause(c):
    tid = paused_task(c)
    page = c.get(f"/tasks/{tid}").text
    assert "Escalation history" in page and "now visible to tier 2" in page and "a tier 1 worker failed it (bot_check)" in page
    assert ">paused<" in page and "4 failed attempts from 2 worker(s)" in page and "reopens" in page
    assert "Release (reopen)" in page and "Close" in page and "visible to workers up to tier 2" in page
    assert page.count("not a bot") >= 4                                                         # every attempt with its error


def test_release_on_the_details_page_reopens_the_task(c):
    tid = paused_task(c)
    r = post(c, f"/tasks/{tid}/release")
    assert (r.status_code, r.headers["location"]) == (303, f"/tasks/{tid}?msg=released")
    page = c.get(f"/tasks/{tid}").text
    assert ">pending<" in page and "reopened" in page and "released manually" in page and "Escalation history" in page
    assert "visible to workers up to tier 1" in page


def test_close_on_a_paused_task(c):
    tid = paused_task(c)
    assert post(c, f"/tasks/{tid}/close").headers["location"].endswith("msg=closed")
    assert q(c, lambda conn: conn.execute("SELECT state FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == "closed"


# --- dashboard -------------------------------------------------------------------------------------------------------------------

def test_dashboard_shows_attempt_statistics_and_bot_check_rate(c):
    html = c.get("/dashboard").text
    assert "Download attempts per worker" in html and "No download attempts yet." in html and "failures per day" in html
    assert "None yet. Messages are sent when a task is paused" in html
    paused_task(c)
    html = c.get("/dashboard").text
    assert "<th scope=\"row\">local</th><td>0</td><td>2</td><td>0 %</td>" in html                  # 0 of 2 succeeded
    assert "4 in total" in html and html.count('class="col-chart"') == 3
    assert "1 paused" in html or "paused</a>" in html


def test_dashboard_lists_the_telegram_messages_and_their_status(c):
    save_telegram(c)
    paused_task(c)
    html = c.get("/dashboard").text
    assert "task_paused" in html and 'class="badge off">queued' in html
    from app import notifications
    q(c, lambda conn: notifications.deliver_pending(conn, transport=c.fake.transport))
    assert 'class="badge on">sent' in c.get("/dashboard").text
    q(c, lambda conn: conn.execute("UPDATE notifications SET status = 'queued', last_error = 'Telegram answered 502: Bad Gateway'"))
    assert "Telegram answered 502" in c.get("/dashboard").text


# --- linking a chat through a t.me link -----------------------------------------------------------------------------------

def tg_link_api(updates, username="podcaster_ng_bot"):
    """Fake Bot API: getMe, getUpdates (returns ``updates`` once) and sendMessage."""
    state = {"updates": list(updates)}

    def respond(call):
        method = call["path"].rsplit("/", 1)[-1]
        if method == "getMe":
            return httpx.Response(200, json={"ok": True, "result": {"username": username}})
        if method == "getUpdates":
            if "offset" in call["body"]:
                state["updates"] = []
                return httpx.Response(200, json={"ok": True, "result": []})
            return httpx.Response(200, json={"ok": True, "result": state["updates"]})
        return httpx.Response(200, json={"ok": True, "result": {}})
    return respond


def start_update(code, chat=4711, uid=1, command="/start"):
    return {"update_id": uid, "message": {"text": f"{command} {code}", "chat": {"id": chat}}}


def link_url(c):
    m = re.search(r'href="(https://t\.me/[^"]+)"', c.get("/settings").text)
    return m.group(1) if m else None


def test_link_flow_shows_a_t_me_link_and_stores_the_chat(c):
    post(c, "/settings", **settings_form(telegram_token=TOKEN))
    assert link_url(c) is None and "Create link to the bot" in c.get("/settings").text
    c.fake.respond = tg_link_api([])
    assert post(c, "/settings/telegram-link").status_code == 303
    url = link_url(c)
    assert url.startswith("https://t.me/podcaster_ng_bot?start=")
    code = url.split("start=")[1]
    c.fake.respond = tg_link_api([start_update(code, chat=4711)])
    r = post(c, "/settings/telegram-link/check")
    assert r.status_code == 200 and "Chat linked" in r.text
    assert q(c, lambda conn: settings.get(conn, "telegram_chat_id")) == "4711"
    assert link_url(c) is None                                                                  # the code is used up
    assert any(call["body"].get("chat_id") == "4711" and "linked" in call["body"].get("text", "") for call in c.fake.calls)
    assert TOKEN not in r.text


def test_link_check_ignores_a_wrong_code_and_says_so(c):
    post(c, "/settings", **settings_form(telegram_token=TOKEN))
    c.fake.respond = tg_link_api([])
    post(c, "/settings/telegram-link")
    c.fake.respond = tg_link_api([start_update("not-the-code"), start_update("x", command="/help")])
    r = post(c, "/settings/telegram-link/check")
    assert r.status_code == 409 and "pressed Start" in r.text
    assert q(c, lambda conn: settings.get(conn, "telegram_chat_id")) == ""
    assert link_url(c) is not None                                                              # still waiting


def test_link_check_without_a_link_and_link_with_a_bad_token(c):
    post(c, "/settings", **settings_form(telegram_token=TOKEN))
    assert post(c, "/settings/telegram-link/check").status_code == 409
    c.fake.respond = lambda call: httpx.Response(401, json={"ok": False, "description": f"Unauthorized {TOKEN}"})
    r = post(c, "/settings/telegram-link")
    assert r.status_code == 502 and "Could not look up the bot" in r.text and TOKEN not in r.text


def test_link_needs_login_and_csrf(tmp_path):
    with TestClient(create_app(make_config(tmp_path)), base_url="https://testserver", follow_redirects=False) as anon:
        for path in ("/settings/telegram-link", "/settings/telegram-link/check"):
            assert anon.post(path).status_code == 303
        do_login(anon)
        for path in ("/settings/telegram-link", "/settings/telegram-link/check"):
            assert anon.post(path).status_code == 403
