"""Telegram Bot API client. The token is part of the request URL, so it must never reach a log or a stored error."""

import hmac
import logging

import httpx

from .scrub import clean

# httpx logs every request URL at INFO, and the bot token is part of that URL.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

API = "https://api.telegram.org"
TIMEOUT = httpx.Timeout(connect=10.0, read=15.0, write=15.0, pool=10.0)
MAX_TEXT = 4096


class TelegramError(Exception):
    """Sending failed; the message is safe to store and show (the token is removed)."""

    def __init__(self, message: str, retry_after: int | None = None, permanent: bool = False):
        super().__init__(message)
        self.retry_after, self.permanent = retry_after, permanent


def _call(token: str, method: str, payload: dict, transport: httpx.BaseTransport | None, base: str):
    """Call a Bot API method and return its ``result``. Raises TelegramError."""
    if not token:
        raise TelegramError("Telegram is not configured (bot token)", permanent=True)
    try:
        with httpx.Client(timeout=TIMEOUT, transport=transport, trust_env=False) as client:
            response = client.post(f"{base}/bot{token}/{method}", json=payload)
    except httpx.HTTPError as exc:  # the exception text may contain the URL: only its type is kept
        raise TelegramError(f"Could not reach Telegram ({type(exc).__name__})") from None
    if response.status_code == 200:
        try:
            return response.json().get("result")
        except ValueError:
            return None
    detail, retry_after = "", None
    try:
        body = response.json()
        detail = str(body.get("description", ""))
        retry_after = (body.get("parameters") or {}).get("retry_after")
    except ValueError:
        pass
    message = clean(f"Telegram answered {response.status_code}: {detail}", 300, (token,))
    if response.status_code == 429:
        raise TelegramError(message, retry_after=int(retry_after) if retry_after else 30)
    # 400/401/403/404 mean a wrong token or chat id: retrying does not help until the settings are fixed
    raise TelegramError(message, permanent=response.status_code in (400, 401, 403, 404))


def send_message(token: str, chat_id: str, text: str, transport: httpx.BaseTransport | None = None,
                 base: str = API) -> None:
    """Send a plain-text message (no parse mode, so no escaping surprises). Raises TelegramError."""
    if not token or not chat_id:
        raise TelegramError("Telegram is not configured (bot token and chat id)", permanent=True)
    _call(token, "sendMessage", {"chat_id": chat_id, "text": text[:MAX_TEXT], "disable_web_page_preview": True},
          transport, base)


def bot_username(token: str, transport: httpx.BaseTransport | None = None, base: str = API) -> str:
    """The bot's @name (without the @), from getMe. Also proves the token works."""
    result = _call(token, "getMe", {}, transport, base)
    name = (result or {}).get("username") if isinstance(result, dict) else None
    if not name:
        raise TelegramError("Telegram did not return a bot username")
    return str(name)


def find_start(token: str, code: str, transport: httpx.BaseTransport | None = None, base: str = API) -> str | None:
    """Look through the bot's pending updates for ``/start <code>`` and return that chat's id (newest match).

    Handled updates are confirmed so Telegram does not keep them. Raises TelegramError (a 409 means a webhook is set).
    """
    updates = _call(token, "getUpdates", {"timeout": 0, "allowed_updates": ["message"]}, transport, base) or []
    found, last = None, None
    for update in updates:
        last = update.get("update_id", last)
        message = update.get("message") or {}
        parts = (message.get("text") or "").split()
        # "/start CODE" or, in groups, "/start@botname CODE"
        if len(parts) == 2 and parts[0].split("@")[0] == "/start" and hmac.compare_digest(parts[1], code):
            chat = message.get("chat") or {}
            if chat.get("id") is not None:
                found = str(chat["id"])
    if last is not None:
        _call(token, "getUpdates", {"offset": last + 1, "timeout": 0}, transport, base)
    return found
