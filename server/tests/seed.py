"""Seed helpers for statistics tests: fill hits_daily with plausible data."""

import random
from datetime import timedelta

from app import episodes, podcasts, stats

GROUPS = ["Apple Podcasts", "Overcast", "Pocket Casts", "Spotify", "Browsers", "Bots", "Other"]


def seed_podcasts(conn, podcast_ids=("abgt", "asot"), episodes_per_podcast=40):
    for pid in podcast_ids:
        podcasts.create(conn, {"id": pid, "name": pid.upper(), "playlist_url": "https://x.example/p"})
        for i in range(episodes_per_podcast):
            episodes.insert(conn, podcast_id=pid, video_id=f"{pid}{i}", title=f"{pid.upper()} Episode {i}",
                            audio_file=f"{pid}{i}.m4a", size_bytes=100_000_000 + i, duration_s=3600,
                            published_at="2024-01-01")


def seed_hits(conn, days=730, podcast_ids=("abgt", "asot"), episodes_per_podcast=40, seed=1):
    """Roughly what a busy instance accumulates: feed rows per app group and file rows per episode."""
    rnd = random.Random(seed)
    today = stats.today()
    rows = []
    for back in range(days):
        day = (today - timedelta(days=back)).isoformat()
        for pid in podcast_ids:
            for g in GROUPS:
                rows.append((day, pid, None, "feed", g, rnd.randint(1, 40), rnd.randint(1, 40) * 9000))
            for i in rng_sample(rnd, episodes_per_podcast, 10):
                for g in GROUPS[:4]:
                    rows.append((day, pid, f"{pid}{i}", "file", g, rnd.randint(1, 5), rnd.randint(1, 5) * 90_000_000))
    conn.execute("BEGIN")  # the helper's connections autocommit; one transaction instead of one per row
    conn.executemany("INSERT INTO hits_daily (day, podcast_id, episode_id, kind, app_group, count, bytes) "
                     "VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.execute("COMMIT")
    return len(rows)


def rng_sample(rnd, n, k):
    return rnd.sample(range(n), k)
