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
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote

from . import config
from .security import validate_url
from .storage import FileStore, StorageFullError, StoredFile

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "webm", "mkv", "avi", "flv", "wmv"}
_AUDIO_EXTENSIONS = {"mp3", "m4a", "aac", "opus", "ogg", "wav"}
_CAPTION_EXTENSIONS = {"vtt", "srt", "json3", "ass", "ssa", "ttml", "xml", "srv1", "srv2", "srv3"}


def _quality_format(short_side: int) -> str:
    """Prefer landscape by height and portrait by width (short side <= limit)."""
    h = str(short_side)
    return (
        f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/b[height<={h}][ext=mp4]/"
        f"bv*[height<={h}]+ba/b[height<={h}]/"
        f"bv*[width<={h}][ext=mp4]+ba[ext=m4a]/b[width<={h}][ext=mp4]/"
        f"bv*[width<={h}]+ba/b[width<={h}]/b"
    )


_FORMATS = {f"{height}p": _quality_format(height) for height in (360, 480, 720, 1080)}


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


@dataclass
class DownloadBundle:
    """Internal result shared by download tools and the analysis engine."""

    stored: StoredFile
    info: dict
    quality: str
    audio_only: bool
    caption_paths: list[Path] = field(default_factory=list)


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
        bundle = await self._download_bundle(
            url,
            quality=quality,
            audio_only=False,
            captions=False,
            max_bytes=config.MAX_FILE_BYTES,
        )
        return self._public_result(bundle)

    async def download_audio(self, url: str) -> dict:
        bundle = await self._download_bundle(
            url,
            quality="best",
            audio_only=True,
            captions=False,
            max_bytes=config.MAX_FILE_BYTES,
        )
        return self._public_result(bundle)

    async def download_for_analysis(self, url: str, quality: str = "720p") -> DownloadBundle:
        quality = normalize_quality(quality)
        return await self._download_bundle(
            url,
            quality=quality,
            audio_only=False,
            captions=True,
            max_bytes=config.MAX_ANALYSIS_FILE_BYTES,
        )

    async def _download_bundle(
        self,
        url: str,
        *,
        quality: str,
        audio_only: bool,
        captions: bool,
        max_bytes: int,
    ) -> DownloadBundle:
        url = await asyncio.to_thread(validate_url, url)
        await self._check_rate_limit()

        try:
            await asyncio.wait_for(self._semaphore.acquire(), timeout=45)
        except TimeoutError:
            raise BusyError("The server is busy with other downloads. Try again in a minute.") from None

        self._active += 1
        job_dir = Path(tempfile.mkdtemp(prefix="job-", dir=config.DOWNLOADS_DIR))
        try:
            info: dict | None = None
            bot_error: DownloadError | None = None
            for client in self._client_attempts(url):
                cmd = self._build_download_cmd(url, quality, audio_only, job_dir, captions=captions)
                if client and client != "default":
                    cmd += ["--extractor-args", f"youtube:player_client={client}"]
                try:
                    info = await self._run(cmd, config.DOWNLOAD_TIMEOUT_SECONDS)
                    break
                except DownloadError as exc:
                    if self._is_bot_error(str(exc)):
                        bot_error = exc
                        continue
                    raise
            if info is None:
                raise bot_error or DownloadError("The download failed.")

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
            if size > max_bytes:
                limit_mb = max(1, max_bytes // (1024 * 1024))
                raise DownloadError(
                    f"The file is larger than the {limit_mb} MB limit. "
                    "Try a lower quality or a shorter video."
                )

            title = (video.get("title") or media.stem or "video").strip()
            platform = (
                video.get("extractor_key")
                or video.get("extractor")
                or video.get("webpage_url_domain")
                or "generic"
            )
            duration = video.get("duration")
            try:
                duration_value = float(duration) if duration is not None else None
            except (TypeError, ValueError):
                duration_value = None

            caption_files = self._caption_files(job_dir) if captions else []
            stored = self.store.add(
                media,
                title=title,
                source_url=video.get("webpage_url") or url,
                platform=str(platform),
                duration=duration_value,
                extra={"captions": bool(caption_files)},
            )

            moved_captions: list[Path] = []
            if caption_files:
                cap_dir = stored.path.parent / "captions"
                cap_dir.mkdir(parents=True, exist_ok=True)
                for caption in caption_files:
                    try:
                        target = cap_dir / caption.name
                        if target.exists():
                            target = cap_dir / f"{caption.stem}-{len(moved_captions)}{caption.suffix}"
                        shutil.move(str(caption), str(target))
                        moved_captions.append(target)
                    except OSError:
                        continue

            return DownloadBundle(
                stored=stored,
                info=video,
                quality=quality,
                audio_only=audio_only,
                caption_paths=moved_captions,
            )

        except StorageFullError as exc:
            raise DownloadError(str(exc)) from exc
        finally:
            self._active -= 1
            self._semaphore.release()
            shutil.rmtree(job_dir, ignore_errors=True)

    def _public_result(self, bundle: DownloadBundle) -> dict:
        stored = bundle.stored
        video = bundle.info
        download_url = f"{config.PUBLIC_BASE_URL}/files/{stored.token}/{quote(stored.filename)}"

        actual_width = video.get("width")
        actual_height = video.get("height")
        if bundle.audio_only:
            actual_quality = "mp3 192kbps"
        elif (
            isinstance(actual_width, int)
            and isinstance(actual_height, int)
            and actual_width > 0
            and actual_height > 0
        ):
            actual_quality = f"{min(actual_width, actual_height)}p"
        elif isinstance(actual_height, int) and actual_height > 0:
            actual_quality = f"{actual_height}p"
        else:
            actual_quality = bundle.quality

        return {
            "status": "success",
            "title": stored.title,
            "platform": stored.platform,
            "uploader": video.get("uploader") or video.get("channel") or video.get("creator"),
            "duration_seconds": int(stored.duration) if stored.duration else None,
            "kind": "audio" if bundle.audio_only else "video",
            "quality": actual_quality,
            "file_size_mb": stored.size_mb,
            "filename": stored.filename,
            "download_url": download_url,
            "playback_url": download_url,
            "file_token": stored.token,
            "expires_in_minutes": max(1, config.FILE_TTL_SECONDS // 60),
            "note": (
                "Give the user this download_url as a clickable link. "
                "The link is temporary and expires automatically."
            ),
        }

    async def get_info(self, url: str) -> dict:
        url = await asyncio.to_thread(validate_url, url)
        info: dict | None = None
        bot_error: DownloadError | None = None
        for client in self._client_attempts(url):
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
            if client and client != "default":
                cmd += ["--extractor-args", f"youtube:player_client={client}"]
            cmd.append(url)
            try:
                info = await self._run(cmd, config.INFO_TIMEOUT_SECONDS)
                break
            except DownloadError as exc:
                if self._is_bot_error(str(exc)):
                    bot_error = exc
                    continue
                raise
        if info is None:
            raise bot_error or DownloadError("Could not read the video metadata.")
        video = self._first_video(info)

        heights = set()
        for fmt in video.get("formats") or []:
            if fmt.get("vcodec") in (None, "none"):
                continue
            width, height = fmt.get("width"), fmt.get("height")
            short_side = None
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                short_side = min(width, height)
            elif isinstance(height, int) and height > 0:
                short_side = height
            if short_side:
                heights.add(short_side)
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

    async def fetch_captions(self, url: str, language: str, dest_dir: Path) -> list[Path]:
        """Download one caption track for analysis. Best-effort, never fatal."""
        if not language:
            return []
        try:
            url = await asyncio.to_thread(validate_url, url)
        except Exception:
            return []
        dest_dir.mkdir(parents=True, exist_ok=True)
        for client in self._client_attempts(url):
            cmd = [
                sys.executable,
                "-m",
                "yt_dlp",
                "--no-playlist",
                "--skip-download",
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                language,
                "--sub-format",
                "json3/vtt/srt/best",
                "--no-warnings",
                "--no-progress",
                "--no-color",
                "--restrict-filenames",
                "-J",
                "--socket-timeout",
                "15",
                "--retries",
                "1",
                "--output",
                str(dest_dir / "%(id).100B.%(ext)s"),
            ]
            self._add_network_args(cmd)
            if client and client != "default":
                cmd += ["--extractor-args", f"youtube:player_client={client}"]
            cmd.append(url)
            try:
                await self._run(cmd, 60)
            except Exception:
                continue
            files = self._caption_files(dest_dir)
            if files:
                return files
        return []

    @staticmethod
    def choose_caption_language(info: dict) -> str | None:
        """Pick the most useful caption language from yt-dlp metadata."""
        manual = info.get("subtitles") or {}
        auto = info.get("automatic_captions") or {}
        original = str(info.get("language") or "").split("-")[0].lower()

        def score(lang: str, is_manual: bool) -> int:
            base = lang.split("-")[0].lower()
            value = 0
            if is_manual:
                value += 20
            if base == "en":
                value += 25
            if original and base == original:
                value += 35
            if lang.lower() in {"en", "en-us", "en-gb", "en-orig"}:
                value += 5
            return value

        candidates: list[tuple[int, str]] = []
        for lang in manual:
            candidates.append((score(lang, True), lang))
        for lang in auto:
            candidates.append((score(lang, False), lang))
        if not candidates:
            return None
        candidates.sort(key=lambda item: (-item[0], item[1]))
        return candidates[0][1]

    def _build_download_cmd(
        self,
        url: str,
        quality: str,
        audio_only: bool,
        job_dir: Path,
        *,
        captions: bool = False,
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
        if captions:
            cmd += [
                "--write-subs",
                "--write-auto-subs",
                "--sub-langs",
                "en.*,en",
                "--sub-format",
                "json3/vtt/srt/best",
            ]
        self._add_network_args(cmd)
        cmd.append(url)
        return cmd

    @staticmethod
    def _is_bot_error(message: str) -> bool:
        low = (message or "").lower()
        return "sign in to confirm" in low or "not a bot" in low

    @staticmethod
    def _is_youtube(url: str) -> bool:
        low = (url or "").lower()
        return "youtube.com" in low or "youtu.be" in low

    def _client_attempts(self, url: str):
        """Player clients to try for this URL, in order (bot-wall bypass)."""
        if not self._is_youtube(url):
            yield None
            return
        for client in config.YTDLP_PLAYER_CLIENTS or [None]:
            yield client or None

    @staticmethod
    def _add_network_args(cmd: list[str]) -> None:
        if config.COOKIES_PATH and config.COOKIES_PATH.is_file():
            cmd += ["--cookies", str(config.COOKIES_PATH)]
        if config.YTDLP_IMPERSONATE:
            cmd += ["--impersonate", config.YTDLP_IMPERSONATE]
        if config.YTDLP_PROXY:
            cmd += ["--proxy", config.YTDLP_PROXY]
        ffmpeg_path = config.FFMPEG_BIN
        if os.path.isabs(ffmpeg_path) and Path(ffmpeg_path).is_file():
            cmd += ["--ffmpeg-location", str(Path(ffmpeg_path).parent)]
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
    def _caption_files(root: Path) -> list[Path]:
        files = [
            path
            for path in root.rglob("*")
            if path.is_file() and path.suffix.lower().lstrip(".") in _CAPTION_EXTENSIONS
        ]
        return sorted(files, key=lambda path: (len(path.name), path.name))

    @staticmethod
    def _pick_file(job_dir: Path, *, audio_only: bool) -> Path | None:
        candidates: list[Path] = []
        for path in job_dir.rglob("*"):
            if not path.is_file():
                continue
            suffix = path.suffix.lower().lstrip(".")
            if suffix in {"part", "ytdl", "temp", "json", "jpg", "jpeg", "png", "webp"}:
                continue
            if suffix in _CAPTION_EXTENSIONS:
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
