"""A fake Telegram API for tests."""

import json
import logging

import httpx

TOKEN = "123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"
CHAT = "-1001234567890"


class FakeTelegram:
    """Records every sendMessage call; ``respond`` decides the answer (default: ok)."""

    def __init__(self, respond=None):
        self.calls = []
        self.respond = respond or (lambda call: httpx.Response(200, json={"ok": True, "result": {}}))
        self.transport = httpx.MockTransport(self)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        call = {"url": str(request.url), "path": request.url.path, "body": json.loads(request.content)}
        self.calls.append(call)
        return self.respond(call)

    @property
    def texts(self):
        return [c["body"]["text"] for c in self.calls]


def configure(conn, token=TOKEN, chat=CHAT):
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_token', ?)", (token,))
    conn.execute("INSERT OR REPLACE INTO settings (key, value) VALUES ('telegram_chat_id', ?)", (chat,))
