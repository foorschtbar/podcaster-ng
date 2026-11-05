"""Podcasting 2.0 chapters file (as the old Podcaster generated it)."""

import json

MAX_TITLE = 45  # Apple recommends chapter titles of 45 characters or fewer


def build_chapters_json(chapters: list) -> bytes:
    data = {"version": "1.2.0", "chapters": [
        {"startTime": c.get("start_time", 0), "title": t if len(t) <= MAX_TITLE else t[: MAX_TITLE - 1].rstrip() + "…"}
        for c in chapters if isinstance(c, dict) and isinstance(t := c.get("title"), str) and t
    ]}
    return json.dumps(data).encode()
