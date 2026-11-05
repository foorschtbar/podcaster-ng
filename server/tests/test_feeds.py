import os
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from app import episodes, feeds, podcasts
from app.db import connect
from app.migrations import migrate

GOLDEN = Path(__file__).parent / "golden" / "feed.xml"
NS = {"itunes": "http://www.itunes.com/dtds/podcast-1.0.dtd", "podcast": "https://podcastindex.org/namespace/1.0"}
STAMP = "2024-02-01T10:00:00Z"


@pytest.fixture
def conn(tmp_path):
    migrate(tmp_path / "p.db", tmp_path / "b")
    c = connect(tmp_path / "p.db")
    podcasts.create(c, {"id": "abgt", "name": "ABGT & Friends", "description": "Above & Beyond <weekly>",
                        "playlist_url": "https://www.youtube.com/playlist?list=PL1&x=2",
                        "image_url": "https://img.example.org/big.jpg?a=1&b=2", "author": "Anjunabeats"})
    ep = dict(podcast_id="abgt", author="Above & Beyond")
    episodes.insert(c, **ep, video_id="v1", title="Episode 100 - Live & Direct", audio_file="Episode 100 - Live & Direct.m4a",
                    description="Tracklist <b>bold</b> & ]]> tricky \x01 control", square_file="Episode 100 - Live & Direct.square.jpg",
                    thumb_file="Episode 100 - Live & Direct.webp", chapters_file="Episode 100 - Live & Direct.chapters.json",
                    size_bytes=123456, duration_s=3600, published_at="2024-01-05")
    episodes.insert(c, **ep, video_id="v2", title="Episode 101", audio_file="Episode 101.m4a", description="plain",
                    thumb_file="Episode 101.jpg", size_bytes=2000, duration_s=1800, published_at="2024-01-12")
    episodes.insert(c, podcast_id="abgt", video_id="v3", title="No artwork", audio_file="No artwork.m4a",
                    size_bytes=5, duration_s=0, published_at=None)
    episodes.insert(c, **ep, video_id="hidden", title="Hidden", audio_file="Hidden.m4a", state="hidden")
    episodes.insert(c, **ep, video_id="gone", title="Deleted", audio_file=None, state="deleted")
    c.execute("UPDATE podcasts SET created_at = ?, updated_at = ?", (STAMP, STAMP))
    c.execute("UPDATE episodes SET created_at = ?, updated_at = ?", ("2024-01-13T08:00:00Z",) * 2)
    yield c
    c.close()


def render(conn, base="https://pod.example.org"):
    return feeds.build_feed(podcasts.get(conn, "abgt"), episodes.list_available(conn, "abgt"), base)


def test_matches_golden_file(conn):
    xml = render(conn)
    if os.environ.get("UPDATE_GOLDEN"):
        GOLDEN.write_text(xml, encoding="utf-8")
    assert xml == GOLDEN.read_text(encoding="utf-8")


def test_well_formed_and_structure(conn):
    root = ET.fromstring(render(conn))
    assert root.tag == "rss" and root.get("version") == "2.0"
    ch = root.find("channel")
    assert ch.findtext("title") == "ABGT & Friends"
    assert ch.findtext("link") == "https://www.youtube.com/playlist?list=PL1&x=2"
    assert ch.find("image/url").text == "https://img.example.org/big.jpg?a=1&b=2"
    assert ch.find("itunes:image", NS).get("href") == "https://img.example.org/big.jpg?a=1&b=2"
    assert ch.findtext("itunes:author", namespaces=NS) == "Anjunabeats"
    assert ch.findtext("language") == "en" and ch.findtext("itunes:explicit", namespaces=NS) == "no"
    assert ch.find("itunes:category", NS).get("text") == "Music"


def test_only_available_episodes_newest_first(conn):
    items = ET.fromstring(render(conn)).findall("channel/item")
    # an episode without a publish date counts as published when it was added (2024-01-13)
    assert [i.findtext("title") for i in items] == ["No artwork", "Episode 101", "Episode 100 - Live & Direct"]


def test_item_fields_follow_old_rules(conn):
    base = "https://pod.example.org"
    items = {i.findtext("title"): i for i in ET.fromstring(render(conn, base)).findall("channel/item")}
    e = items["Episode 100 - Live & Direct"]
    url = f"{base}/files/abgt/Episode%20100%20-%20Live%20%26%20Direct.m4a"
    assert e.findtext("guid") == url                        # GUID = file URL
    enc = e.find("enclosure")
    assert (enc.get("url"), enc.get("length"), enc.get("type")) == (url, "123456", "audio/x-m4a")
    assert e.find("itunes:image", NS).get("href") == f"{base}/files/abgt/Episode%20100%20-%20Live%20%26%20Direct.square.jpg"
    ch = e.find("podcast:chapters", NS)
    assert ch.get("url").endswith("Direct.chapters.json") and ch.get("type") == "application/json+chapters"
    assert e.findtext("itunes:duration", namespaces=NS) == "3600"
    assert e.findtext("pubDate") == "Fri, 05 Jan 2024 00:00:00 +0000"
    assert e.findtext("itunes:author", namespaces=NS) == "Above & Beyond"
    assert e.findtext("itunes:subtitle", namespaces=NS) == e.findtext("title")


def test_artwork_fallbacks(conn):
    items = {i.findtext("title"): i for i in ET.fromstring(render(conn)).findall("channel/item")}
    # no square image: the original thumbnail, then the podcast image
    assert items["Episode 101"].find("itunes:image", NS).get("href").endswith("/Episode%20101.jpg")
    assert items["No artwork"].find("itunes:image", NS).get("href") == "https://img.example.org/big.jpg?a=1&b=2"
    assert items["No artwork"].find("podcast:chapters", NS) is None
    assert items["No artwork"].find("pubDate") is None


def test_special_characters_are_escaped_not_corrupting(conn):
    xml = render(conn)
    root = ET.fromstring(xml)  # would raise on broken XML
    tricky = next(i for i in root.findall("channel/item") if i.findtext("title").startswith("Episode 100"))
    assert tricky.findtext("description") == "Tracklist <b>bold</b> & ]]> tricky  control"  # \x01 dropped
    assert "<b>" not in xml and "\x01" not in xml


def test_empty_podcast_feed_is_valid(conn):
    podcasts.create(conn, {"id": "empty", "name": "Empty", "playlist_url": "https://x.example/p"})
    xml = feeds.build_feed(podcasts.get(conn, "empty"), [], "https://pod.example.org")
    ch = ET.fromstring(xml).find("channel")
    assert ch.findall("item") == [] and ch.find("image") is None
    assert ch.findtext("description") == "Empty" and ch.findtext("itunes:author", namespaces=NS) == "Empty"


def test_last_build_date_is_stable(conn):
    assert render(conn) == render(conn)
    assert "<lastBuildDate>Thu, 01 Feb 2024 10:00:00 +0000</lastBuildDate>" in render(conn)  # newest change
    conn.execute("UPDATE episodes SET updated_at = '2024-03-01T00:00:00Z' WHERE video_id = 'v1'")
    assert "<lastBuildDate>Fri, 01 Mar 2024 00:00:00 +0000</lastBuildDate>" in render(conn)
