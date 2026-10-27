from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "video-downloader-mcp"
APP_TITLE = "Video Downloader MCP"
VERSION = "1.0.0"


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


DOWNLOADS_DIR = Path(os.getenv("DOWNLOADS_DIR", "/tmp/video-mcp"))
DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)

# Render injects RENDER_EXTERNAL_URL automatically. PUBLIC_BASE_URL wins if set.
PUBLIC_BASE_URL = (
    os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or "http://127.0.0.1:8000"
).rstrip("/")

MAX_FILE_MB = _env_int("MAX_FILE_MB", 150)
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024

MAX_STORAGE_MB = _env_int("MAX_STORAGE_MB", 400)
MAX_STORAGE_BYTES = MAX_STORAGE_MB * 1024 * 1024

FILE_TTL_SECONDS = _env_int("FILE_TTL_MINUTES", 60) * 60

MAX_CONCURRENT_DOWNLOADS = max(1, _env_int("MAX_CONCURRENT_DOWNLOADS", 2))
MAX_DOWNLOADS_PER_HOUR = max(1, _env_int("MAX_DOWNLOADS_PER_HOUR", 40))

DOWNLOAD_TIMEOUT_SECONDS = _env_int("DOWNLOAD_TIMEOUT_SECONDS", 180)
INFO_TIMEOUT_SECONDS = _env_int("INFO_TIMEOUT_SECONDS", 45)

# Only enable for local testing. Never enable on a public deployment.
ALLOW_PRIVATE_URLS = _env_bool("ALLOW_PRIVATE_URLS", False)

# Optional cookies for sites that need a logged-in session (Instagram, YouTube, ...).
# COOKIES_FILE points at a Netscape-format cookies.txt; YTDLP_COOKIES can contain
# the file contents directly (useful as a Render secret env var).
COOKIES_FILE = os.getenv("COOKIES_FILE", "").strip()
COOKIES_PATH: Path | None = None
_cookies_content = os.getenv("YTDLP_COOKIES", "").strip()
if _cookies_content:
    COOKIES_PATH = DOWNLOADS_DIR / "cookies.txt"
    COOKIES_PATH.write_text(_cookies_content + "\n", encoding="utf-8")
    COOKIES_PATH.chmod(0o600)
elif COOKIES_FILE and Path(COOKIES_FILE).is_file():
    COOKIES_PATH = Path(COOKIES_FILE)

# Browser TLS fingerprint impersonation. Helps with TikTok and other bot-protected
# sites. Set to an empty string to disable.
YTDLP_IMPERSONATE = os.getenv("YTDLP_IMPERSONATE", "chrome").strip()

# Optional outbound proxy, e.g. http://user:pass@host:port or socks5://host:port.
# Useful when YouTube blocks the server's datacenter IP.
YTDLP_PROXY = os.getenv("YTDLP_PROXY", "").strip()

# Escape hatch for power users, e.g. "--extractor-args youtube:player_client=android"
YTDLP_EXTRA_ARGS = os.getenv("YTDLP_EXTRA_ARGS", "").strip()
