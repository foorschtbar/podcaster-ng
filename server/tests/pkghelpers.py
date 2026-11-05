"""Builds valid (and broken) episode packages for tests."""

import hashlib
import json

M4A = b"\x00\x00\x00\x18ftypM4A \x00\x00\x00\x00M4A mp42isom" + bytes(range(256)) * 40
JPEG = b"\xff\xd8\xff\xe0" + b"jpegdata" * 50
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + b"webpdata" * 20
PNG = b"\x89PNG\r\n\x1a\n" + b"pngdata" * 20


def info(video_id="aaaaaaaaaaa", title="Group Therapy 701: Live & Direct", **over):
    data = {"id": video_id, "title": title, "description": "Tracklist <b>&</b>", "channel": "Above & Beyond", "duration": 3600,
            "release_date": "20260105", "chapters": [{"start_time": 0, "title": "Intro"}]}
    data.update(over)
    return json.dumps(data).encode()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def package(video_id="aaaaaaaaaaa", title="Group Therapy 701: Live & Direct", thumb=True, square=True, chapters=True, **over):
    """role -> (bytes, ext)"""
    files = {"audio": (M4A, ""), "info": (info(video_id, title, **over), "")}
    if thumb:
        files["thumb"] = (WEBP, "webp")
    if square:
        files["square"] = (JPEG, "")
    if chapters:
        files["chapters"] = (json.dumps({"version": "1.2.0", "chapters": [{"startTime": 0, "title": "Intro"}]}).encode(), "")
    return files


def manifest(files):
    return {"files": [{"role": r, "size": len(d), "sha256": sha(d)} for r, (d, _) in files.items()]}


def upload_all(client, headers, task_id, files, only=None):
    for role, (data, ext) in files.items():
        if only and role not in only:
            continue
        r = client.post(f"/api/worker/tasks/{task_id}/upload", params={"role": role, "size": len(data), "sha256": sha(data), "ext": ext},
                        content=data, headers={**headers, "Content-Type": "application/octet-stream"})
        assert r.status_code == 200, (role, r.status_code, r.text)
