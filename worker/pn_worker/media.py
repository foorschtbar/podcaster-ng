"""Media helpers: square artwork, chapters, hashing and ffprobe verification."""

import hashlib
import json
import logging
import tempfile
import threading
from pathlib import Path

from .process import Failure, run_process

log = logging.getLogger("pn_worker.media")
SQUARE_SIZE = 1400
DURATION_TOLERANCE_S = 10
DURATION_TOLERANCE_FRACTION = 0.03


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def square_thumbnail(source: Path, dest: Path, size: int = SQUARE_SIZE) -> bool:
    """Apple Podcasts wants square artwork. Build it as the old Podcaster did: a blurred, cropped-to-fill copy
    of the thumbnail as the background, with the original (aspect-correct) centred on top. Returns False on failure."""
    from PIL import Image, ImageFilter
    try:
        with Image.open(source) as src:
            src = src.convert("RGB")
            bg_scale = max(size / src.width, size / src.height)
            bg = src.resize((round(src.width * bg_scale), round(src.height * bg_scale)), Image.LANCZOS)
            bg = bg.filter(ImageFilter.GaussianBlur(size // 40))
            left, top = (bg.width - size) // 2, (bg.height - size) // 2
            bg = bg.crop((left, top, left + size, top + size))
            fg_scale = min(size / src.width, size / src.height)
            fg = src.resize((round(src.width * fg_scale), round(src.height * fg_scale)), Image.LANCZOS)
            bg.paste(fg, ((size - fg.width) // 2, (size - fg.height) // 2))
            fd, tmp = tempfile.mkstemp(suffix=".jpg", dir=dest.parent)
            Path(tmp).unlink()
            bg.save(tmp, "JPEG", quality=90)
            Path(tmp).replace(dest)
        return True
    except Exception:  # noqa: BLE001 - artwork is optional, the episode is still worth publishing
        log.warning("Could not build the square thumbnail", exc_info=True)
        return False


def probe(path: Path, timeout: float = 60, binary: str = "ffprobe") -> dict:
    cmd = [binary, "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)]
    code, out, err = run_process(cmd, timeout, threading.Event(), what="ffprobe")
    if code != 0:
        raise Failure("incomplete", f"ffprobe cannot read the audio file: {err.strip()[-300:] or code}")
    try:
        return json.loads(out)
    except ValueError:
        raise Failure("incomplete", "ffprobe returned no usable output") from None


def verify_audio(path: Path, expected_duration: float | None, probe_result: dict | None = None) -> float:
    """The file must be readable, contain an AAC audio stream and match the metadata duration. Returns the duration."""
    data = probe_result or probe(path)
    audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
    if not audio:
        raise Failure("incomplete", "the file has no audio stream")
    if not any(s.get("codec_name") == "aac" for s in audio):
        raise Failure("incomplete", f"the audio is {audio[0].get('codec_name')}, not AAC")
    try:
        duration = float(data["format"]["duration"])
    except (KeyError, TypeError, ValueError):
        raise Failure("incomplete", "the file has no readable duration") from None
    if expected_duration:
        allowed = max(DURATION_TOLERANCE_S, expected_duration * DURATION_TOLERANCE_FRACTION)
        if abs(duration - expected_duration) > allowed:
            raise Failure("incomplete", f"the audio is {duration:.0f} s long, the metadata says {expected_duration:.0f} s "
                                        f"(download cut off?)")
    return duration
