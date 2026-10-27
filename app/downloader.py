from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import shlex
import shutil
import signal
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from urllib.parse import quote

from . import config
from .security import validate_url
from .storage import FileStore, StorageFullError

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "webm", "mkv", "avi", "flv", "wmv"}
_AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "opus", "ogg", "wav"}

_FORMATS = {
    "360p": (
        "bv*[height<=360][ext=mp4]+ba[ext=m4a]/b[height<=360][ext=mp4]/"
        "bv*[height<=360]+ba/b[height<=360]/b"
    ),
    "480p": (
        "bv*[height<=480][ext=mp4]+ba[ext=m4a]/b[height<=480][ext=mp4]/"
        "bv*[height<=480]+ba/b[height<=480]/b"
    ),
    "720p": (
        "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/"
        "bv*[height<=720]+ba/b[height<=720]/b"
    ),
    "1080p": (
        "bv*[height<=1080][ext=mp4]+ba[ext=m4a]/b[height<=1080][ext=mp4]/"
        "bv*[height<=1080]+ba/b[height<=1080]/b"
    ),
}


class DownloadError(RuntimeError):
    pass


class RateLimitError(DownloadError):
    pass


class BusyError(DownloadError):
    pass


def normalize_quality(quality: str | None) -> str:
    raw = (quality or "720p").strip().lower().replace(" ", "")
    if raw in {"best", "max", "highest", "source", "4k", "8k", "2k", "uhd"}:
        return "1080p"
    match = re.search(r"(\d{3,4})", raw)
    if not match:
        return "720p"
    height = int(match.group(1))
    if height >= 1080:
        return "1080p"
    if height >= 720:
        return "720p"
    if height >= 480:
        return "480p"
    return "360p"


class VideoDownloader:
    def __init__(self, store: FileStore):
        self.store = store
        self._semaphore = asyncio.Semaphore(config.MAX_CONCURRENT_DOWNLOADS)
        self._rate_lock = asyncio.Lock()
        self._requests: deque[float] = deque()
        self._active = 0

    @property
    def active(self) -> int:
        return self._active

    async def _check_rate_limit(self) -> None:
        async with self._rate_lock:
            now = time.monotonic()
            while self._requests and now - self._requests[0] > 3600:
                self._requests.popleft()
            if len(self._requests) >= config.MAX_DOWNLOADS_PER_HOUR:
                retry_in = max(1, int(3600 - (now - self._requests[0])))
                raise RateLimitError(
                    f"Download limit reached ({config.MAX_DOWNLOADS_PER_HOUR} per hour). "
                    f"Try again in about {max(1, retry_in // 60)} minute(s)."
                )
            self._requests.append(now)

    async def download_video(self, url: str, quality: str = "720p") -> dict:
        quality = normalize_quality(quality)
        return await self._download(url, quality=quality, audio_only=False)

    async def download_audio(self, url: str) -> dict:
        return await self._download(url, quality="best", audio_only=True)

    async def _download(self, url: str, *, quality: str, audio_only: bool) -> dict:
        url = await asyncio.to_thread(validate_url, url)
        await self._check_rate_limit()

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=45)
        except TimeoutError:
            raise BusyError("The server is busy with other downloads. Try again in a minute.") from None

        self._active += 1
        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=config.DOWNLOADS_DIR))
        try:
            cmd = self._build_download_cmd(url, quality, audio_only, job_dir)
            info = await self._run(cmd, config.DOWNLOAD_TIMEOUT_SECONDS)

            video = self._first_video(info)
            if video.get("is_live"):
                raise DownloadError("Live streams are not supported. Wait until the stream ends.")

            media = self._pick_file(job_dir, audio_only=audio_only)
            if media is None:
                raise DownloadError(
                    "The download finished but no media file was produced. "
                    "The source may be a live stream, a playlist, or an unsupported format."
                )

            size = media.stat().st_size
            if size > config.MAX_FILE_BYTES:
                raise DownloadError(
                    f"The file is larger than the {config.MAX_FILE_MB} MB limit. "
                    "Try a lower quality or a shorter video."
                )

            title = (video.get("title") or media.stem or "video").strip()
            platform = (
                video.get("extractor_key") or video.get("extractor") or video.get("webpage_url_domain") or "generic"
            )
            duration = video.get("duration")
            try:
                duration_value = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration_value = None

            stored = self.store.add(
                media,
                title=title,
                source_url=video.get("webpage_url") or url,
                platform=str(platform),
                duration=duration_value,
            )

            actual_height = video.get("height")
            if audio_only:
                actual_quality = "mp3 192kbps"
            elif isinstance(actual_height, int) and actual_height > 0:
                actual_quality = f"{actual_height}p"
            else:
                actual_quality = quality

            download_url = (
                f"{config.PUBLIC_BASE_URL}/files/{stored.token}/{quote(stored.filename)}"
            )
            result = {
                "status": "success",
                "title": stored.title,
                "platform": stored.platform,
                "uploader": video.get("uploader") or video.get("channel") or video.get("creator"),
                "duration_seconds": int(duration_value) if duration_value else None,
                "kind": "audio" if audio_only else "video",
                "quality": actual_quality,
                "file_size_mb": stored.size_mb,
                "filename": stored.filename,
                "download_url": download_url,
                "expires_in_minutes": max(1, config.FILE_TTL_SECONDS // 60),
                "note": (
                    "Give the user this download_url as a clickable link. "
                    "The link is temporary and expires automatically."
                ),
            }
            return result

        except StorageFullError as exc:
            raise DownloadError(str(exc)) from exc
        finally:
            self._active -= 1
            self._semaphore.release()
            shutil.rmtree(job_dir, ignore_errors=True)

    async def get_info(self, url: str) -> dict:
        url = await asyncio.to_thread(validate_url, url)
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--skip-download",
            "--no-warnings",
            "--no-progress",
            "--no-color",
            "-J",
            "--socket-timeout",
            "15",
            "--retries",
            "1",
            "--extractor-retries",
            "1",
        ]
        self._add_network_args(cmd)
        cmd.append(url)

        info = await self._run(cmd, config.INFO_TIMEOUT_SECONDS)
        video = self._first_video(info)

        heights = set()
        for fmt in video.get("formats") or []:
            height = fmt.get("height")
            if isinstance(height, int) and height > 0 and fmt.get("vcodec") not in (None, "none"):
                heights.add(height)
        standard = [h for h in (144, 240, 360, 480, 720, 1080, 1440, 2160) if h in heights]
        available = [f"{h}p" for h in standard]
        if not available and video.get("height"):
            available = [f"{video['height']}p"]

        return {
            "status": "success",
            "title": video.get("title"),
            "platform": video.get("extractor_key") or video.get("extractor"),
            "uploader": video.get("uploader") or video.get("channel") or video.get("creator"),
            "duration_seconds": video.get("duration"),
            "duration_string": video.get("duration_string"),
            "is_live": bool(video.get("is_live")),
            "webpage_url": video.get("webpage_url") or url,
            "thumbnail": video.get("thumbnail"),
            "view_count": video.get("view_count"),
            "upload_date": video.get("upload_date"),
            "available_qualities": available,
            "note": "Use download_video with this URL to get a temporary download link.",
        }

    def _build_download_cmd(
        self, url: str, quality: str, audio_only: bool, job_dir: Path
    ) -> list[str]:
        cmd = [
            sys.executable,
            "-m",
            "yt_dlp",
            "--no-playlist",
            "--playlist-items",
            "1",
            "--no-warnings",
            "--no-progress",
            "--no-color",
            "--no-simulate",
            "-J",
            "--socket-timeout",
            "20",
            "--retries",
            "2",
            "--extractor-retries",
            "1",
            "--max-filesize",
            f"{config.MAX_FILE_MB}M",
            "--match-filter",
            "!is_live",
            "--restrict-filenames",
            "--output",
            str(job_dir / "%(id).100B.%(ext)s"),
        ]
        if audio_only:
            cmd += [
                "--format",
                "ba/b",
                "--extract-audio",
                "--audio-format",
                "mp3",
                "--audio-quality",
                "192K",
            ]
        else:
            cmd += [
                "--format",
                _FORMATS.get(quality, _FORMATS["720p"]),
                "--merge-output-format",
                "mp4",
            ]
        self._add_network_args(cmd)
        cmd.append(url)
        return cmd

    @staticmethod
    def _add_network_args(cmd: list[str]) -> None:
        if config.COOKIES_PATH and config.COOKIES_PATH.is_file():
            cmd += ["--cookies", str(config.COOKIES_PATH)]
        if config.YTDLP_IMPERSONATE:
            cmd += ["--impersonate", config.YTDLP_IMPERSONATE]
        if config.YTDLP_PROXY:
            cmd += ["--proxy", config.YTDLP_PROXY]
        VideoDownloader._add_extra_args(cmd)

    @staticmethod
    def _add_extra_args(cmd: list[str]) -> None:
        if config.YTDLP_EXTRA_ARGS:
            try:
                cmd.extend(shlex.split(config.YTDLP_EXTRA_ARGS))
            except ValueError:
                pass

    async def _run(self, cmd: list[str], timeout: int) -> dict:
        env = os.environ.copy()
        env.setdefault("PYTHONUNBUFFERED", "1")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            start_new_session=True,
        )
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            # Kill the whole process group so an ffmpeg child does not survive.
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await proc.wait()
            raise DownloadError(
                f"The download timed out after {timeout} seconds. "
                "Try a shorter video or a lower quality."
            ) from None

        if proc.returncode != 0:
            raise DownloadError(self._friendly_error(stderr.decode("utf-8", "replace")))

        text = stdout.decode("utf-8", "replace").strip()
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1:
            raise DownloadError(
                "The source returned no usable metadata. It may not be a supported video URL."
            )
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError:
            raise DownloadError("Could not read the source's metadata response.") from None
        return data

    @staticmethod
    def _first_video(info: dict) -> dict:
        if not isinstance(info, dict):
            return {}
        if info.get("_type") in {"playlist", "multi_video"}:
            entries = [e for e in (info.get("entries") or []) if isinstance(e, dict)]
            if entries:
                return entries[0]
        return info

    @staticmethod
    def _pick_file(job_dir: Path, *, audio_only: bool) -> Path | None:
        candidates: list[Path] = []
        for path in job_dir.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix.lower() in {".part", ".ytdl", ".temp", ".json", ".jpg", ".jpeg", ".png", ".webp"}:
                continue
            candidates.append(path)
        if not candidates:
            return None

        preferred = _AUDIO_EXTENSIONS if audio_only else _VIDEO_EXTENSIONS
        matching = [p for p in candidates if p.suffix.lower().lstrip(".") in preferred]
        pool = matching or candidates
        return max(pool, key=lambda p: p.stat().st_size)

    @staticmethod
    def _friendly_error(stderr: str) -> str:
        text = _ANSI_RE.sub("", stderr)
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        errors = [line for line in lines if line.startswith("ERROR:")]
        message = errors[-1] if errors else (lines[-1] if lines else "Unknown download error.")
        message = message.removeprefix("ERROR:").strip()
        message = re.sub(r"\s+", " ", message)
        low = message.lower()

        if "sign in to confirm" in low or "not a bot" in low:
            return (
                "YouTube is blocking downloads from this server's IP address. "
                "Try another platform, or configure YTDLP_COOKIES on the server. "
                f"Original error: {message[:300]}"
            )
        if "unsupported url" in low:
            return (
                "This URL is not supported. Provide a direct link to a public video post "
                f"or page. Original error: {message[:300]}"
            )
        if "private video" in low or "video is private" in low:
            return "This video is private, so it cannot be downloaded."
        if "login" in low or "cookies" in low or "registered users" in low:
            return (
                "This content requires a logged-in session. "
                "The server owner can add cookies via the YTDLP_COOKIES environment variable. "
                f"Original error: {message[:300]}"
            )
        if "max-filesize" in low or "larger than" in low:
            return (
                f"The file exceeds the server's {config.MAX_FILE_MB} MB limit. "
                "Try a lower quality or a shorter video."
            )
        if "does not pass filter" in low or "live" in low and "stream" in low:
            return "Live streams are not supported. Wait until the stream ends and try again."
        if "http error 403" in low:
            return (
                "The source blocked the request (HTTP 403). "
                "It may require login or may be region-restricted. "
                f"Original error: {message[:300]}"
            )
        return message[:500]
