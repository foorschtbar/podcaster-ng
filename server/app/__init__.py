"""PodcasterNG server."""

import os

# The build stamps the real version (git describe: a tag, or the commit) into the image as APP_VERSION;
# "dev" means an unstamped build (a checkout run directly).
__version__ = (os.environ.get("APP_VERSION") or "").strip()[:50] or "dev"
