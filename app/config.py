from __future__ import annotations

import os
import shutil
from pathlib import Path

APP_NAME = "video-downloader-mcp"
APP_TITLE = "Video Downloader & Analysis MCP"
VERSION = "2.0.0"


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

# YouTube bot-wall bypass: when the datacenter IP gets "Sign in to confirm you're not
# a bot", retry the same request with each of these player clients until one works.
# Order matters — cheapest/most reliable first. Override with a comma-separated list.
YTDLP_PLAYER_CLIENTS = [
    client.strip()
    for client in os.getenv(
        "YTDLP_PLAYER_CLIENTS",
        "default,tv,tv_simply,android_vr,ios,web_safari,web_embedded,mweb",
    ).split(",")
    if client.strip()
]

# ---------------------------------------------------------------------------
# Analysis / evidence engine
# ---------------------------------------------------------------------------

# Binary paths. Docker installs ffmpeg and tesseract into PATH. For local
# development you can point these at static builds.
FFMPEG_BIN = os.getenv("FFMPEG_BIN", "").strip() or shutil.which("ffmpeg") or "ffmpeg"
FFPROBE_BIN = os.getenv("FFPROBE_BIN", "").strip() or shutil.which("ffprobe") or "ffprobe"
TESSERACT_BIN = os.getenv("TESSERACT_BIN", "").strip() or shutil.which("tesseract") or ""
OCR_LANG = os.getenv("OCR_LANG", "eng").strip() or "eng"
OCR_ENABLED = bool(TESSERACT_BIN)

# Analysis is intentionally bounded so a 0.1-CPU free instance stays responsive.
ANALYSIS_MAX_SECONDS = max(60, _env_int("ANALYSIS_MAX_SECONDS", 1800))
MAX_SHOTS = max(8, _env_int("MAX_SHOTS", 48))
MAX_CHAPTERS = max(3, _env_int("MAX_CHAPTERS", 24))
MAX_FRAMES_PER_CALL = max(1, _env_int("MAX_FRAMES_PER_CALL", 6))
MAX_CHAPTER_IMAGES = max(1, _env_int("MAX_CHAPTER_IMAGES", 4))
MAX_ANALYSIS_FILE_MB = max(1, _env_int("MAX_ANALYSIS_FILE_MB", 150))
MAX_ANALYSIS_FILE_BYTES = MAX_ANALYSIS_FILE_MB * 1024 * 1024

# Timeouts (seconds) for individual ffmpeg/tesseract operations.
PROBE_TIMEOUT_SECONDS = max(5, _env_int("PROBE_TIMEOUT_SECONDS", 30))
FRAME_TIMEOUT_SECONDS = max(10, _env_int("FRAME_TIMEOUT_SECONDS", 45))
AUDIO_TIMEOUT_SECONDS = max(10, _env_int("AUDIO_TIMEOUT_SECONDS", 150))
OCR_TIMEOUT_SECONDS = max(5, _env_int("OCR_TIMEOUT_SECONDS", 25))
KEYFRAME_TIMEOUT_SECONDS = max(20, _env_int("KEYFRAME_TIMEOUT_SECONDS", 180))
ANALYSIS_TIMEOUT_SECONDS = max(60, _env_int("ANALYSIS_TIMEOUT_SECONDS", 240))

# OCR is the most CPU-expensive optional layer. Keep a per-call budget.
OCR_BUDGET_SECONDS = max(5, _env_int("OCR_BUDGET_SECONDS", 30))
OCR_OPEN_FRAMES = max(0, _env_int("OCR_OPEN_FRAMES", 4))

# Audio is kept as a small 8 kHz mono WAV for waveform/spectrogram evidence.
AUDIO_SAMPLE_RATE = max(4000, _env_int("AUDIO_SAMPLE_RATE", 8000))
