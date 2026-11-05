"""Maps a user agent to a coarse app group. The user agent itself is never stored.

First match wins, so specific apps come before the generic bot and browser rules.
Extend ``RULES`` to recognise more apps.
"""

import re

OTHER = "Other"

RULES: list[tuple[str, re.Pattern]] = [(group, re.compile(pattern, re.I)) for group, pattern in [
    ("Apple Podcasts", r"applecoremedia|\bpodcasts/\d|applepodcasts|itunes|itms"),
    ("Overcast", r"overcast"),
    ("Pocket Casts", r"pocket ?casts"),
    ("Spotify", r"spotify"),
    ("Castro", r"castro"),
    ("AntennaPod", r"antennapod"),
    ("Podcast Addict", r"podcast ?addict"),
    ("Castbox", r"castbox"),
    ("Downcast", r"downcast"),
    ("Podverse", r"podverse"),
    ("Fountain", r"fountain"),
    ("Google Podcasts", r"google[ -]?podcasts"),
    ("YouTube Music", r"youtube ?music"),
    ("Amazon Music", r"amazon ?music"),
    ("Deezer", r"deezer"),
    ("Bots", r"bot\b|bot/|crawl|spider|slurp|facebookexternalhit|feedfetcher|uptime|monitor|headless|preview"),
    ("Browsers", r"mozilla/|opera|safari|chrome|firefox|edg/"),
]]


def classify(user_agent: str | None) -> str:
    ua = user_agent or ""
    for group, pattern in RULES:
        if pattern.search(ua):
            return group
    return OTHER
