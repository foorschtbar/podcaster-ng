import json
from pathlib import Path

import pytest

from app import filenames
from app.filenames import base_name, base_of, limit_bytes, sanitize_title

CORPUS = [
    "Plain title", "A: B", "Ratio 3:45 live", 'Q? or "quotes"', "slash/back\\pipe|star*lt<gt>", "-leading dash", ".hidden",
    "__under__score__", "trailing dot.", "Ünïcödé SØNIN", "Emoji 🎧 mix", "tab\there", "new\nline", "   spaces   ", "???",
    "Mix: Part 1 / Part 2 | 2024", "100% *real*", "C:\\path", "10:30:15 set", "x:y:z", "::", "a::b", "\n", "\nstart", "end\n",
    "A State of Trance Episode 1300: Amsterdam Dance Event Marathon (@astateoftrance)",
    "A State of Trance Episode 1301 UNFILTERED #009 – Live from Los Angeles @Ace*Mission",
    "Group Therapy 664 with Above & Beyond and SØNIN", "a" * 300, "日本語のタイトル: テスト",
]


def test_fullwidth_substitution_like_yt_dlp():
    assert sanitize_title("A: B") == "A： B"
    assert sanitize_title('Q? "x"') == "Q？ ＂x＂"
    assert sanitize_title("a/b\\c|d*e<f>g") == "a⧸b⧹c｜d＊e＜f＞g"
    assert sanitize_title("Live 10:30 set") == "Live 10_30 set"            # timestamps keep their digits
    assert sanitize_title("-leading") == "-leading" and sanitize_title(".hidden") == ".hidden"
    assert sanitize_title("new\nline") == "new line" and sanitize_title("tab\there") == "tabhere"


def test_never_empty_and_no_path_separators():
    for title in CORPUS + ["", "../..", "..", "/", "\\"]:
        name = sanitize_title(title)
        assert name and "/" not in name and "\\" not in name and "\0" not in name, repr(title)


def test_matches_yt_dlp_itself():
    """The server has no yt-dlp, so the port is compared with the real thing wherever yt-dlp is installed."""
    yt = pytest.importorskip("yt_dlp")
    ydl = yt.YoutubeDL({"outtmpl": "%(title)s.%(ext)s", "quiet": True})
    for title in CORPUS:
        expected = ydl.prepare_filename({"id": "x", "title": title, "ext": "m4a"})
        assert sanitize_title(title) + ".m4a" == expected, repr(title)


def test_empty_title_falls_back_to_the_video_id():
    assert base_name("", "abc12345678", set()) == "abc12345678"
    assert base_name("   ", "abc12345678", set()) == "abc12345678"


def test_leading_dots_never_make_a_hidden_file():
    assert sanitize_title(".hidden") == ".hidden"                         # as yt-dlp does ...
    assert base_name(".hidden", "id1", set()) == "hidden"                  # ... but the server never creates dotfiles
    assert base_name("...", "id1", set()) == "id1" and base_name("..", "id2", set()) == "id2"
    assert not base_name("../../evil", "id", set()).startswith(".")


def test_collision_adds_the_video_id():
    assert base_name("Episode 1", "id1", set()) == "Episode 1"
    assert base_name("Episode 1", "id2", {"Episode 1"}) == "Episode 1 [id2]"
    assert base_name("episode 1", "id2", {"Episode 1"}) == "episode 1 [id2]"      # case-insensitive file systems
    assert base_name("Episode 1", "id3", {"Episode 1", "Episode 1 [id3]"}) != "Episode 1"


def test_long_titles_are_shortened_on_character_boundaries():
    name = base_name("ä" * 300, "id1", set())
    assert len(name.encode()) <= filenames.MAX_BASE_BYTES and set(name) == {"ä"}
    with_id = base_name("ä" * 300, "id2", {name})
    assert with_id.endswith(" [id2]") and len(with_id.encode()) <= filenames.MAX_BASE_BYTES + 10
    assert limit_bytes("short") == "short"


def test_base_of():
    assert base_of("Title.m4a") == "Title" and base_of("Title.M4A") == "Title"
    assert base_of("Title.jpg") is None and base_of(None) is None


REAL = Path(__file__).resolve().parent / "data" / "real_titles.json"


@pytest.mark.skipif(not REAL.exists(), reason="no recorded real titles")
def test_real_titles_give_the_real_file_names():
    for title, base in json.loads(REAL.read_text()):
        assert sanitize_title(title) == base
