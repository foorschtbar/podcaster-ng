"""The production image only has server/requirements.txt, not the dev packages the tests run with."""

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# import name -> pinned distribution in server/requirements.txt
DIRECT = {"fastapi": "fastapi", "uvicorn": "uvicorn", "jinja2": "jinja2", "multipart": "python-multipart", "yaml": "pyyaml",
          "httpx": "httpx"}
# installed together with the packages above (fastapi brings starlette and pydantic, jinja2 brings markupsafe)
TRANSITIVE = {"starlette", "pydantic", "markupsafe", "anyio", "typing_extensions"}


def imported_modules(directory: Path) -> set[str]:
    found = set()
    for path in directory.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                found |= {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                found.add(node.module.split(".")[0])
    return found


def requirement_names(path: Path) -> set[str]:
    return {line.split("==")[0].strip().lower() for line in path.read_text().splitlines()
            if line.strip() and not line.startswith(("#", "-"))}


def test_the_server_only_imports_what_its_image_installs():
    pinned = requirement_names(ROOT / "server" / "requirements.txt")
    local = {"app", "pn_shared"}
    allowed = set(sys.stdlib_module_names) | local | TRANSITIVE | {m for m, dist in DIRECT.items() if dist in pinned}
    for directory in (ROOT / "server" / "app", ROOT / "shared" / "pn_shared"):
        unexpected = imported_modules(directory) - allowed
        assert not unexpected, f"{directory.name} imports {sorted(unexpected)}, which the server image does not install"


def test_every_direct_requirement_is_pinned():
    for line in (ROOT / "server" / "requirements.txt").read_text().splitlines():
        assert "==" in line, line


def test_the_worker_only_imports_what_its_image_installs():
    pinned = requirement_names(ROOT / "worker" / "requirements.txt")
    direct = {"httpx": "httpx", "pydantic": "pydantic", "PIL": "pillow"}
    allowed = (set(sys.stdlib_module_names) | {"pn_worker", "pn_shared", "yt_dlp"} |
               {m for m, dist in direct.items() if dist in pinned} | {"annotated_types", "typing_extensions"})
    unexpected = imported_modules(ROOT / "worker" / "pn_worker") - allowed
    assert not unexpected, f"the worker imports {sorted(unexpected)}, which its image does not install"
