import sys
from pathlib import Path

SERVER = Path(__file__).resolve().parent.parent
ROOT = SERVER.parent
# the app, plus the packages shared with / used by the worker
for path in (SERVER, ROOT / "shared", ROOT / "worker"):
    if path.is_dir():
        sys.path.insert(0, str(path))
