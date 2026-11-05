"""One chronological list of what happened, built from the tables that already record it (no extra bookkeeping)."""

import sqlite3

KINDS = ("task", "escalation", "retention", "message")

# Every part yields the same columns: at, kind, podcast_id, subject, what, detail, link, level ('ok' | 'warn' | 'bad' | '').
_PARTS = {
    "task": """
        SELECT COALESCE(a.ended_at, a.started_at) AS at, 'task' AS kind, t.podcast_id AS podcast_id,
               COALESCE(NULLIF(t.title, ''), t.video_id, 'scan') AS subject,
               t.type || ' ' || COALESCE(a.result, 'running') || CASE WHEN a.error_class IS NOT NULL THEN ' (' || a.error_class || ')' ELSE '' END AS what,
               TRIM(CASE WHEN a.worker_name != '' THEN a.worker_name ELSE 'worker deleted' END || ' · ' ||
                    COALESCE(NULLIF(a.error_text, ''), NULLIF(a.note, ''), ''), ' ·') AS detail,
               '/tasks/' || t.id AS link,
               CASE WHEN a.result = 'ok' THEN 'ok' WHEN a.result IN ('failed', 'lease_expired') THEN 'bad'
                    WHEN a.result IS NULL THEN '' ELSE 'warn' END AS level
        FROM attempts a JOIN tasks t ON t.id = a.task_id""",
    "escalation": """
        SELECT e.at, 'escalation', t.podcast_id, COALESCE(NULLIF(t.title, ''), t.video_id, 'scan'),
               'task ' || e.kind, e.detail, '/tasks/' || t.id, CASE WHEN e.kind = 'paused' THEN 'bad' WHEN e.kind = 'reopened' THEN 'ok' ELSE 'warn' END
        FROM task_events e JOIN tasks t ON t.id = e.task_id""",
    "retention": """
        SELECT l.at, 'retention', l.podcast_id, COALESCE(NULLIF(l.title, ''), l.video_id),
               'files deleted (' || l.reason || ')', l.files || ' file(s), ' || l.bytes || ' bytes',
               '/episodes/' || l.podcast_id || '/' || l.video_id, 'warn'
        FROM retention_log l""",
    "message": """
        SELECT COALESCE(n.sent_at, n.created_at), 'message', NULL, n.kind,
               'Telegram ' || n.status, substr(replace(n.text, char(10), ' '), 1, 200) || CASE WHEN n.last_error != '' THEN ' · ' || n.last_error ELSE '' END,
               NULL, CASE n.status WHEN 'sent' THEN 'ok' WHEN 'failed' THEN 'bad' ELSE 'warn' END
        FROM notifications n""",
}


def _with(kind: str | None) -> str:
    """A CTE named ``ev``; its column list names the columns whichever parts are included."""
    union = " UNION ALL ".join(sql for k, sql in _PARTS.items() if kind in (None, k))
    return f"WITH ev(at, kind, podcast_id, subject, what, detail, link, level) AS ({union})"


def count(conn: sqlite3.Connection, kind: str | None = None, podcast_id: str | None = None) -> int:
    return conn.execute(f"{_with(kind)} SELECT COUNT(*) FROM ev WHERE (? IS NULL OR podcast_id = ?)", (podcast_id, podcast_id)).fetchone()[0]


def list_events(conn: sqlite3.Connection, kind: str | None = None, podcast_id: str | None = None,
                limit: int = 50, offset: int = 0) -> list[sqlite3.Row]:
    return conn.execute(
        f"{_with(kind)} SELECT e.*, p.name AS podcast_name FROM ev e LEFT JOIN podcasts p ON p.id = e.podcast_id "
        "WHERE (? IS NULL OR e.podcast_id = ?) ORDER BY e.at DESC, e.subject LIMIT ? OFFSET ?",
        (podcast_id, podcast_id, limit, offset)).fetchall()
