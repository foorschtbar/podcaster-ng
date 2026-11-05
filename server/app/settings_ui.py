"""Admin UI: global settings."""

import secrets

from fastapi import Depends, Request
from fastapi.responses import HTMLResponse

from . import notifications, settings, telegram
from .web import Session, db, env_defaults, protected, redirect, render, require_session


def _page(request: Request, session: Session, values: dict, errors: dict, *, secrets_set: dict,
          flash: str | None = None, status_code: int = 200, notice_error: str | None = None):
    with db(request) as conn:
        link = dict(conn.execute("SELECT key, value FROM settings WHERE key IN "
                                 "('telegram_link_code', 'telegram_bot_username')").fetchall())
    link_url = (f"https://t.me/{link['telegram_bot_username']}?start={link['telegram_link_code']}"
                if link.get("telegram_link_code") and link.get("telegram_bot_username") else None)
    return render(request, "settings.html", session, link_url=link_url, bot_name=link.get("telegram_bot_username"), status_code=status_code, active="settings",
                  title="Settings", groups=settings.groups(), v=values, errors=errors,
                  secrets_set=secrets_set, flash=flash, notice_error=notice_error)


def _display(values: dict) -> dict:
    # Secrets are never rendered back; None becomes an empty field.
    return {k: ("" if val is None or settings.BY_KEY[k].kind == "secret" else val) for k, val in values.items()}


@protected.get("/settings", response_class=HTMLResponse, include_in_schema=False)
def settings_form(request: Request, saved: int = 0, session: Session = Depends(require_session)):
    with db(request) as conn:
        values = settings.get_all(conn, env_defaults(request))
    return _page(request, session, _display(values), {}, flash="Settings saved." if saved else None,
                 secrets_set={k: bool(values[k]) for k, f in settings.BY_KEY.items() if f.kind == "secret"})


@protected.post("/settings", response_class=HTMLResponse, include_in_schema=False)
async def settings_save(request: Request, session: Session = Depends(require_session)):
    form = await request.form()
    with db(request) as conn:
        changes, errors = settings.validate(form, env_defaults(request))
        if not errors:
            settings.save(conn, changes)
            return redirect("/settings?saved=1")
        current = settings.get_all(conn, env_defaults(request))
    values = {f.key: str(form.get(f.key) or "") for f in settings.FIELDS if f.kind != "secret"}
    return _page(request, session, values, errors, status_code=422,
                 secrets_set={k: bool(current[k]) for k, f in settings.BY_KEY.items() if f.kind == "secret"})


@protected.post("/settings/telegram-test", response_class=HTMLResponse, include_in_schema=False)
def telegram_test(request: Request, session: Session = Depends(require_session)):
    """Send a test message with the saved token and chat id."""
    with db(request) as conn:
        values = settings.get_all(conn, env_defaults(request))
        token, chat = notifications.credentials(conn)
    secrets_set = {k: bool(values[k]) for k, f in settings.BY_KEY.items() if f.kind == "secret"}
    try:
        telegram.send_message(token, chat, "✅ PodcasterNG: test message. Telegram alerts are set up.",
                              request.app.state.telegram_transport)
    except telegram.TelegramError as exc:
        return _page(request, session, _display(values), {}, secrets_set=secrets_set, status_code=502,
                     notice_error=f"The test message could not be sent: {exc}")
    return _page(request, session, _display(values), {}, secrets_set=secrets_set, flash="Test message sent.")


def _again(request, session, *, flash=None, error=None, status_code=200):
    with db(request) as conn:
        values = settings.get_all(conn, env_defaults(request))
    return _page(request, session, _display(values), {},
                 secrets_set={k: bool(values[k]) for k, f in settings.BY_KEY.items() if f.kind == "secret"},
                 flash=flash, notice_error=error, status_code=status_code)


@protected.post("/settings/telegram-link", response_class=HTMLResponse, include_in_schema=False)
def telegram_link(request: Request, session: Session = Depends(require_session)):
    """Create a one-time /start link for the saved bot; opening it in Telegram links that chat."""
    with db(request) as conn:
        token, _ = notifications.credentials(conn)
    try:
        name = telegram.bot_username(token, request.app.state.telegram_transport)
    except telegram.TelegramError as exc:
        return _again(request, session, error=f"Could not look up the bot: {exc}", status_code=502)
    with db(request) as conn:
        settings.save(conn, {"telegram_bot_username": name, "telegram_link_code": secrets.token_urlsafe(12)})
    return redirect("/settings#telegram")


@protected.post("/settings/telegram-link/check", response_class=HTMLResponse, include_in_schema=False)
def telegram_link_check(request: Request, session: Session = Depends(require_session)):
    """Pick up the chat that pressed Start on the link and store it as the alert chat."""
    with db(request) as conn:
        token, _ = notifications.credentials(conn)
        code = conn.execute("SELECT value FROM settings WHERE key = 'telegram_link_code'").fetchone()
    if not code:
        return _again(request, session, error="Create a link first.", status_code=409)
    transport = request.app.state.telegram_transport
    try:
        chat = telegram.find_start(token, code[0], transport)
        if chat is None:
            return _again(request, session, error="No chat has pressed Start on the link yet. "
                          "Open the link in Telegram, press Start, then check again.", status_code=409)
        telegram.send_message(token, chat, "✅ PodcasterNG is linked to this chat. Alerts will arrive here.", transport)
    except telegram.TelegramError as exc:
        return _again(request, session, error=f"Could not check Telegram: {exc}", status_code=502)
    with db(request) as conn:
        settings.save(conn, {"telegram_chat_id": chat, "telegram_link_code": None})
    return _again(request, session, flash="Chat linked. Alerts will be sent there.")
