import pytest

from app.scrub import clean, scrub


@pytest.mark.parametrize("raw,must_not_contain", [
    ("token pnw_abcdefghijklmnop leaked", "pnw_abcdefghijklmnop"),
    ("Authorization: Bearer abcdefghijklmnop1234", "abcdefghijklmnop1234"),
    ("GET https://user:s3cret@host.example/path", "s3cret"),
    ("proxy http://proxyuser:proxypass@vpn.example:3128", "proxyuser:proxypass"),
    ("bot 123456789:AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw", "AAHdqTcvCH1vGWJxfSeofSAs0K5PALDsaw"),
    ("password=hunter2&x=1", "hunter2"), ("cookie: SID=abc", "SID=abc"), ("api_key = 12345", "12345"),
])
def test_secrets_are_removed(raw, must_not_contain):
    assert must_not_contain not in scrub(raw) and "***" in scrub(raw)


def test_extra_secrets_and_control_characters():
    assert "my-telegram-token" not in scrub("x my-telegram-token y", ("my-telegram-token",))
    assert scrub("a\x00b\x1bc\x07d") == "abcd" and scrub("line1\nline2\ttab") == "line1\nline2\ttab"


def test_clean_trims_and_collapses():
    assert clean("a   b \t c") == "a b c"
    out = clean("x" * 1000, limit=100)
    assert len(out) == 100 and out.endswith("…")
    assert clean(None) == "" and clean("   ") == ""


def test_harmless_text_unchanged():
    text = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot"
    assert scrub(text) == text
