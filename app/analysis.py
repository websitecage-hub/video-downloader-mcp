from __future__ import annotations

import asyncio
import contextlib
import math
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import config, media, textindex
from .downloader import DownloadBundle, VideoDownloader
from .storage import FileStore


class AnalysisError(RuntimeError):
    pass


@dataclass
class Session:
    id: str
    dir: Path
    bundle: DownloadBundle
    media_info: media.MediaInfo
    created_at: float
    expires_at: float
    duration: float
    shots: list[dict[str, Any]] = field(default_factory=list)
    chapters: list[dict[str, Any]] = field(default_factory=list)
    transcript: list[dict[str, Any]] = field(default_factory=list)
    transcript_source: str = "none"
    transcript_language: str = ""
    audio: dict[str, Any] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    contact_sheet: str | None = None
    index_path: Path | None = None
    detail_cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    frame_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    motion_cache: dict[str, dict[str, Any]] = field(default_factory=dict)
    audio_images: dict[str, str] | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    @property
    def video(self) -> Path:
        return self.bundle.stored.path

    @property
    def title(self) -> str:
        return self.bundle.stored.title

    @property
    def platform(self) -> str:
        return self.bundle.stored.platform

    @property
    def playback_url(self) -> str:
        from urllib.parse import quote

        return (
            f"{config.PUBLIC_BASE_URL}/files/{self.bundle.stored.token}/"
            f"{quote(self.bundle.stored.filename)}"
        )

    @property
    def watch_url(self) -> str:
        return f"{config.PUBLIC_BASE_URL}/watch/{self.id}"

    def asset_url(self, name: str) -> str:
        return f"{config.PUBLIC_BASE_URL}/analysis/{self.id}/{name}"

    def asset_path(self, name: str) -> Path:
        return self.dir / name


def _even_indices(count: int, target: int) -> list[int]:
    if count <= 0 or target <= 0:
        return []
    if count <= target:
        return list(range(count))
    if target == 1:
        return [count // 2]
    step = (count - 1) / (target - 1)
    return sorted({int(round(index * step)) for index in range(target)})


def _mean(values: Iterable[float]) -> float:
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else 0.0


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


class VideoAnalyzer:
    """Text-first video database plus selective visual evidence."""

    def __init__(self, store: FileStore, downloader: VideoDownloader):
        self.store = store
        self.downloader = downloader
        self.sessions: dict[str, Session] = {}
        self._job_lock = asyncio.Lock()
        self._media_lock = asyncio.Lock()
        self._active = 0

    # ------------------------------------------------------------------ setup

    @asynccontextmanager
    async def _media_guard(self):
        try:
            await asyncio.wait_for(self._media_lock.acquire(), timeout=180)
        except TimeoutError:
            raise AnalysisError(
                "The analysis engine is busy with another request. Try again in a moment."
            ) from None
        try:
            yield
        finally:
            self._media_lock.release()

    @property
    def active(self) -> int:
        return self._active

    def stats(self) -> dict[str, Any]:
        return {"active_jobs": self._active, "sessions": len(self.sessions)}

    # --------------------------------------------------------------- opening

    async def open(self, url: str, quality: str = "720p") -> dict[str, Any]:
        async with self._job_lock:
            self._active += 1
            try:
                bundle = await self.downloader.download_for_analysis(url, quality)
                return await self._analyze(bundle)
            finally:
                self._active -= 1

    async def _analyze(self, bundle: DownloadBundle) -> dict[str, Any]:
        video = bundle.stored.path
        info = await asyncio.to_thread(media.probe, video)
        if not info.has_video:
            raise AnalysisError(
                "This URL has no video stream, so there is nothing visual to analyze. "
                "Use download_audio for audio-only content."
            )

        duration = max(0.0, info.duration or bundle.stored.duration or 0.0)
        analyzed_duration = min(duration, float(config.ANALYSIS_MAX_SECONDS)) if duration else 0.0

        session = Session(
            id=secrets.token_urlsafe(12),
            dir=bundle.stored.path.parent / "analysis",
            bundle=bundle,
            media_info=info,
            created_at=time.time(),
            expires_at=bundle.stored.expires_at,
            duration=analyzed_duration,
        )
        session.dir.mkdir(parents=True, exist_ok=True)
        if duration > analyzed_duration + 1:
            session.warnings.append(
                f"Only the first {textindex.format_time(analyzed_duration)} of "
                f"{textindex.format_time(duration)} was analyzed on this free instance."
            )

        session.shots = await self._build_shots(session)
        session.transcript, session.transcript_source, session.transcript_language = (
            await self._build_transcript(session)
        )
        session.chapters = self._build_chapters(session)
        session.audio = await self._build_audio(session)
        await self._ocr_open_frames(session)
        self._enrich_chapters(session)
        session.index_path = session.dir / "index.sqlite"
        await self._build_index(session)
        await self._write_transcript_file(session)
        session.contact_sheet = await self._compose_contact_sheet(session)

        self.sessions[session.id] = session
        return self._open_payload(session)

    # ---------------------------------------------------------------- shots

    async def _build_shots(self, session: Session) -> list[dict[str, Any]]:
        video = session.video
        duration = session.duration or session.media_info.duration
        thumbs_dir = session.dir / "thumbs"
        thumbs_dir.mkdir(parents=True, exist_ok=True)

        keyframes = await asyncio.to_thread(media.keyframe_times, video, duration or 1.0)
        frames: list[tuple[float, Path]] = []
        if len(keyframes) >= 4:
            step = max(1, math.ceil(len(keyframes) / config.MAX_SHOTS))
            async with self._media_guard():
                frames = await media.extract_keyframe_thumbs(
                    video,
                    thumbs_dir,
                    step=step,
                    max_side=480,
                    max_seconds=int(duration or config.ANALYSIS_MAX_SECONDS) + 1,
                )

        if len(frames) < 3:
            frames = await self._uniform_frames(session, thumbs_dir)
        if not frames:
            raise AnalysisError(
                "Could not decode any frames from this video. It may be corrupt or unsupported."
            )

        # Normalize ordering and merge near-duplicate timestamps.
        frames.sort(key=lambda item: item[0])
        merged: list[tuple[float, Path]] = []
        for timestamp, path in frames:
            if merged and timestamp - merged[-1][0] < 0.2:
                continue
            merged.append((max(0.0, timestamp), path))
        frames = merged

        # Make sure the timeline starts at 0 so early content is never skipped.
        if frames[0][0] > 1.0:
            first_path = thumbs_dir / "kf-00000.jpg"
            try:
                async with self._media_guard():
                    await media.extract_frame(video, 0.0, first_path, max_side=480)
                frames.insert(0, (0.0, first_path))
            except media.MediaError:
                pass

        shots: list[dict[str, Any]] = []
        for index, (timestamp, path) in enumerate(frames):
            start = 0.0 if index == 0 else timestamp
            end = frames[index + 1][0] if index + 1 < len(frames) else max(
                duration, timestamp + 1.0
            )
            features = await asyncio.to_thread(media.image_features, path)
            shots.append(
                {
                    "index": index,
                    "start": round(start, 2),
                    "end": round(max(start + 0.05, end), 2),
                    "keyframe": round(timestamp, 2),
                    "thumb": path.name,
                    "features": features,
                    "motion": 0.0,
                    "ocr": None,
                    "chapter": 0,
                }
            )

        for index in range(1, len(shots)):
            previous = thumbs_dir / shots[index - 1]["thumb"]
            current = thumbs_dir / shots[index]["thumb"]
            score = await asyncio.to_thread(media.motion_score, previous, current)
            histogram_distance = media.histogram_distance(
                shots[index - 1]["features"].get("histogram") or [],
                shots[index]["features"].get("histogram") or [],
            )
            shots[index]["motion"] = round(max(score, histogram_distance), 3)

        return shots

    async def _uniform_frames(self, session: Session, thumbs_dir: Path) -> list[tuple[float, Path]]:
        duration = session.duration or session.media_info.duration
        if duration <= 0:
            duration = 1.0
        count = int(_clamp(round(duration / 5) + 1, 6, 24))
        frames: list[tuple[float, Path]] = []
        for index in range(count):
            timestamp = (duration - 0.05) * index / max(1, count - 1)
            path = thumbs_dir / f"u-{index:05d}.jpg"
            try:
                async with self._media_guard():
                    await media.extract_frame(session.video, timestamp, path, max_side=480)
                frames.append((timestamp, path))
            except media.MediaError:
                continue
        return frames

    # ------------------------------------------------------------ transcript

    async def _build_transcript(
        self, session: Session
    ) -> tuple[list[dict[str, Any]], str, str]:
        preferred = self.downloader.choose_caption_language(session.bundle.info)
        files = [path for path in session.bundle.caption_paths if path.is_file()]
        has_caption_metadata = bool(
            session.bundle.info.get("subtitles")
            or session.bundle.info.get("automatic_captions")
        )
        if not files and has_caption_metadata:
            source_url = (
                session.bundle.stored.source_url
                or session.bundle.info.get("webpage_url")
                or ""
            )
            language = preferred or "en"
            fetched = await self.downloader.fetch_captions(
                source_url, language, session.dir / "captions"
            )
            files = [path for path in fetched if path.is_file()]

        segments, caption_format, path = textindex.choose_best_caption(files, preferred)
        if not segments:
            return [], "none", ""

        language = ""
        if path is not None:
            parts = path.name.split(".")
            if len(parts) >= 3:
                language = parts[-2]
        source = caption_format if not language else f"{caption_format}:{language}"
        return segments, source, language

    async def _write_transcript_file(self, session: Session) -> None:
        if not session.transcript:
            return
        lines = [
            f"[{textindex.format_time(segment['start'])}] {segment['text']}"
            for segment in session.transcript
        ]
        header = f"# {session.title}\n# {session.playback_url}\n\n"
        path = session.dir / "transcript.txt"
        await asyncio.to_thread(
            path.write_text, header + "\n".join(lines) + "\n", "utf-8"
        )

    # -------------------------------------------------------------- chapters

    def _build_chapters(self, session: Session) -> list[dict[str, Any]]:
        shots = session.shots
        if not shots:
            return []
        duration = session.duration or shots[-1]["end"]
        target = int(_clamp(round(duration / 30) if duration else 1, 1, config.MAX_CHAPTERS))
        if len(shots) <= target:
            target = max(1, len(shots))

        breakpoints: list[float] = [0.0]
        for index in range(1, target):
            desired = duration * index / target
            nearest = min(shots, key=lambda shot: abs(shot["start"] - desired))
            if nearest["start"] > breakpoints[-1] + 1.0:
                breakpoints.append(nearest["start"])
        breakpoints.append(max(duration, shots[-1]["end"] + 0.01))

        chapters: list[dict[str, Any]] = []
        for index in range(len(breakpoints) - 1):
            start = breakpoints[index]
            end = breakpoints[index + 1]
            chapter_shots = [
                shot for shot in shots if start <= shot["start"] < end
            ] or [shots[min(index, len(shots) - 1)]]
            chapter = self._make_chapter(len(chapters), start, end, chapter_shots, session)
            chapters.append(chapter)
            for shot in chapter_shots:
                shot["chapter"] = chapter["index"]

        return chapters

    def _make_chapter(
        self,
        index: int,
        start: float,
        end: float,
        chapter_shots: list[dict[str, Any]],
        session: Session,
    ) -> dict[str, Any]:
        text = textindex.segments_in_range(session.transcript, start, end, max_chars=900)
        features = [shot["features"] for shot in chapter_shots]
        brightness = _mean([feature.get("brightness", 0.0) for feature in features])
        contrast = _mean([feature.get("contrast", 0.0) for feature in features])
        text_score = max((feature.get("text_score", 0.0) for feature in features), default=0.0)
        motion = _mean([shot["motion"] for shot in chapter_shots])

        visual_parts = [
            "dark" if brightness < 0.25 else "bright" if brightness > 0.72 else "well-lit",
            "low detail" if contrast < 0.08 else "detailed",
            "high motion"
            if motion > 0.18
            else "moderate motion"
            if motion > 0.07
            else "mostly static",
        ]
        if text_score > 0.1:
            visual_parts.append("on-screen text")
        colors: list[str] = []
        for feature in features:
            for color in feature.get("colors") or []:
                if color not in colors:
                    colors.append(color)

        title = self._chapter_title(text, index, start, end)
        summary_parts = []
        if text:
            summary_parts.append(text)
        summary_parts.append("Visual: " + ", ".join(visual_parts))
        if colors:
            summary_parts.append("Palette: " + " ".join(colors[:4]))
        if session.transcript and not text:
            summary_parts.append("No speech detected in this chapter.")

        return {
            "index": index,
            "start": round(start, 2),
            "end": round(end, 2),
            "title": title,
            "summary": " · ".join(summary_parts)[:1200],
            "text": text,
            "ocr": "",
            "visual": ", ".join(visual_parts),
            "colors": colors[:4],
            "motion": round(motion, 3),
            "brightness": round(brightness, 3),
            "shots": [shot["index"] for shot in chapter_shots],
        }

    @staticmethod
    def _chapter_title(text: str, index: int, start: float, end: float) -> str:
        if text:
            sentence = text.split(".")[0].split("?")[0].split("!")[0].strip()
            if 8 <= len(sentence) <= 90:
                return sentence
            return text[:70].rsplit(" ", 1)[0]
        return f"Scene {index + 1} · {textindex.format_time(start)}–{textindex.format_time(end)}"

    def _enrich_chapters(self, session: Session) -> None:
        for chapter in session.chapters:
            chapter_shots = [
                shot for shot in session.shots if shot["index"] in chapter["shots"]
            ]
            ocr_lines: list[str] = []
            for shot in chapter_shots:
                ocr = shot.get("ocr") or {}
                for line in str(ocr.get("text") or "").splitlines():
                    line = line.strip()
                    if line and line not in ocr_lines:
                        ocr_lines.append(line)
            if ocr_lines:
                chapter["ocr"] = " | ".join(ocr_lines)[:400]
                chapter["summary"] = (chapter["summary"] + " · OCR: " + chapter["ocr"])[:1200]
                if chapter["title"].startswith("Scene ") and ocr_lines:
                    chapter["title"] = ocr_lines[0][:80]

    # ------------------------------------------------------------------ OCR

    async def _ocr_open_frames(self, session: Session) -> None:
        if not config.OCR_ENABLED or config.OCR_OPEN_FRAMES <= 0:
            return
        candidates = [
            shot
            for shot in session.shots
            if (shot["features"].get("text_score") or 0.0) > 0.03
        ]
        candidates.sort(key=lambda shot: shot["features"].get("text_score") or 0.0, reverse=True)
        candidates = candidates[: config.OCR_OPEN_FRAMES]
        deadline = time.monotonic() + config.OCR_BUDGET_SECONDS
        for shot in candidates:
            if time.monotonic() > deadline:
                break
            path = session.dir / "thumbs" / shot["thumb"]
            result = await self._ocr(path, deadline)
            shot["ocr"] = result

    async def _ocr(self, path: Path, deadline: float) -> dict[str, Any]:
        if not config.OCR_ENABLED:
            return {"text": "", "boxes": [], "available": False}
        async with self._media_guard():
            result = await asyncio.to_thread(media.ocr_image, path, psm=6)
            if not result.get("text") and time.monotonic() + 5 < deadline:
                sparse = await asyncio.to_thread(media.ocr_image, path, psm=11)
                if len(sparse.get("text") or "") > len(result.get("text") or ""):
                    result = sparse
        return result

    # ---------------------------------------------------------------- audio

    async def _build_audio(self, session: Session) -> dict[str, Any]:
        if not session.media_info.has_audio:
            return {"has_audio": False}
        duration = session.duration or session.media_info.duration or config.ANALYSIS_MAX_SECONDS
        audio_path = session.dir / "audio.wav"
        try:
            async with self._media_guard():
                ok = await media.extract_audio(
                    session.video,
                    audio_path,
                    sample_rate=config.AUDIO_SAMPLE_RATE,
                    max_seconds=int(min(duration, config.ANALYSIS_MAX_SECONDS)),
                )
        except media.MediaError:
            ok = False
        if not ok:
            return {"has_audio": False, "warning": "Audio could not be extracted."}
        try:
            async with self._media_guard():
                timeline = await media.audio_timeline(audio_path, duration)
        except media.MediaError:
            return {"has_audio": False, "warning": "Audio timeline analysis failed."}
        return {
            "has_audio": True,
            "path": "audio.wav",
            "sample_rate": config.AUDIO_SAMPLE_RATE,
            "duration": round(duration, 2),
            "mean_volume_db": timeline.get("mean_volume_db"),
            "max_volume_db": timeline.get("max_volume_db"),
            "silence_ratio": timeline.get("silence_ratio"),
            "events": timeline.get("events") or [],
        }

    # --------------------------------------------------------------- search

    async def _build_index(self, session: Session) -> None:
        docs: list[dict[str, Any]] = []
        for segment in session.transcript:
            docs.append(
                {
                    "kind": "transcript",
                    "start": segment["start"],
                    "end": segment["end"],
                    "text": segment["text"],
                    "meta": {"source": session.transcript_source},
                }
            )
        docs.extend(self._ocr_docs(session, session.shots))
        for chapter in session.chapters:
            docs.append(
                {
                    "kind": "chapter",
                    "start": chapter["start"],
                    "end": chapter["end"],
                    "text": chapter["summary"],
                    "meta": {"chapter": chapter["index"] + 1, "title": chapter["title"]},
                }
            )
        if session.index_path is not None:
            await asyncio.to_thread(textindex.build_index, session.index_path, docs)

    @staticmethod
    def _ocr_docs(session: Session, shots: list[dict[str, Any]]) -> list[dict[str, Any]]:
        docs: list[dict[str, Any]] = []
        for shot in shots:
            ocr = shot.get("ocr") or {}
            text = str(ocr.get("text") or "").strip()
            if not text:
                continue
            docs.append(
                {
                    "kind": "ocr",
                    "start": shot["start"],
                    "end": shot["end"],
                    "text": text,
                    "meta": {"shot": shot["index"], "time": shot["keyframe"]},
                }
            )
        return docs

    # ------------------------------------------------------------ contact sheet

    async def _compose_contact_sheet(self, session: Session) -> str | None:
        shots = session.shots
        if not shots:
            return None
        if 1 < len(session.chapters) <= 12:
            picks = [session.chapters[index]["shots"][0] for index in range(len(session.chapters))]
            selected = [next(shot for shot in shots if shot["index"] == pick) for pick in picks]
        else:
            selected = [shots[index] for index in _even_indices(len(shots), 12)]
        frames = [
            {
                "path": session.dir / "thumbs" / shot["thumb"],
                "label": f"#{shot['index'] + 1}  {textindex.format_time(shot['keyframe'])}",
                "timestamp": shot["keyframe"],
            }
            for shot in selected
        ]
        out = session.dir / "contact.jpg"
        title = f"{session.title[:70]} · {textindex.format_time(session.media_info.duration)}"
        try:
            async with self._media_guard():
                await asyncio.to_thread(
                    media.compose_sheet,
                    frames,
                    out,
                    duration=session.media_info.duration or session.duration,
                    cols=4,
                    rows=3,
                    width=1600,
                    height=900,
                    header=title,
                )
        except Exception:
            return None
        return out.name if out.is_file() else None

    # ------------------------------------------------------------------- API

    def _open_payload(self, session: Session) -> dict[str, Any]:
        info = session.media_info
        payload = {
            "status": "success",
            "session_id": session.id,
            "title": session.title,
            "platform": session.platform,
            "uploader": session.bundle.info.get("uploader")
            or session.bundle.info.get("channel")
            or session.bundle.info.get("creator"),
            "duration_seconds": round(info.duration or session.duration, 2),
            "duration_string": textindex.format_time(info.duration or session.duration),
            "resolution": f"{info.width}x{info.height}",
            "fps": round(info.fps, 2),
            "file_size_mb": session.bundle.stored.size_mb,
            "analyzed_seconds": round(session.duration, 2),
            "has_audio": bool(session.audio.get("has_audio")),
            "has_transcript": bool(session.transcript),
            "transcript_source": session.transcript_source,
            "transcript_language": session.transcript_language,
            "ocr_available": config.OCR_ENABLED,
            "shots": len(session.shots),
            "chapters_count": len(session.chapters),
            "playback_url": session.playback_url,
            "watch_url": session.watch_url,
            "contact_sheet_url": session.asset_url(session.contact_sheet)
            if session.contact_sheet
            else None,
            "contact_sheet_path": str(session.dir / session.contact_sheet)
            if session.contact_sheet
            else None,
            "timeline": [
                {
                    "chapter": chapter["index"] + 1,
                    "start": chapter["start"],
                    "end": chapter["end"],
                    "time": f"{textindex.format_time(chapter['start'])}–{textindex.format_time(chapter['end'])}",
                    "title": chapter["title"],
                    "summary": chapter["summary"],
                    "motion": chapter["motion"],
                    "images": len(chapter["shots"]),
                }
                for chapter in session.chapters
            ],
            "warnings": session.warnings,
            "how_to_go_deeper": (
                "The timeline above is the complete textual map. For visual evidence call "
                "get_chapter(session_id, chapter) to get real frames plus transcript and OCR "
                "for one chapter. Use search_video(session_id, query) to find anything said or "
                "shown, get_frames(session_id, timestamps=[...]) for specific moments, "
                "get_motion(session_id, start, end) for change over time, and "
                "get_audio(session_id) for waveform/spectrogram and speech-vs-silence structure. "
                "When you cite a moment, include its timestamp. Reuse this session_id instead "
                "of calling open_video again."
            ),
        }
        return payload

    def get_session(self, session_id: str) -> Session:
        session = self.sessions.get(session_id)
        if session is None:
            raise AnalysisError(
                "Session not found or expired. Call open_video again with the video URL."
            )
        if session.expires_at <= time.time():
            self._drop_session(session_id)
            raise AnalysisError(
                "That analysis session expired. Call open_video again with the video URL."
            )
        return session

    def timeline(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        return self._open_payload(session) | {
            "next_steps": "Call get_chapter for a chapter number, or search_video for a phrase.",
        }

    def search(
        self, session_id: str, query: str, *, kind: str = "all", limit: int = 8
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        results = textindex.search_index(
            session.index_path or session.dir / "index.sqlite",
            query,
            limit=max(1, min(20, limit)),
            kind=kind if kind in {"transcript", "ocr", "chapter"} else None,
        )
        if not results:
            results = self._substring_search(session, query, limit=limit)
        for result in results:
            result["chapter"] = self._chapter_for_time(session, result["start"])
        return {
            "status": "success",
            "session_id": session.id,
            "query": query,
            "kind": kind,
            "count": len(results),
            "results": results,
            "note": (
                "Results are ordered by relevance. Each has a 'time' you can cite and a "
                "'start' timestamp for get_frames or get_motion. If nothing matches, try "
                "fewer or different words."
            ),
        }

    def _substring_search(self, session: Session, query: str, *, limit: int) -> list[dict[str, Any]]:
        needle = (query or "").strip().lower()
        if len(needle) < 2:
            return []
        results: list[dict[str, Any]] = []
        for segment in session.transcript:
            if needle in segment["text"].lower():
                results.append(
                    {
                        "kind": "transcript",
                        "start": segment["start"],
                        "end": segment["end"],
                        "time": textindex.format_time(segment["start"]),
                        "text": segment["text"],
                        "snippet": textindex._snippet(segment["text"], query),
                        "score": 0.0,
                        "meta": {},
                    }
                )
        for shot in session.shots:
            text = str((shot.get("ocr") or {}).get("text") or "")
            if text and needle in text.lower():
                results.append(
                    {
                        "kind": "ocr",
                        "start": shot["start"],
                        "end": shot["end"],
                        "time": textindex.format_time(shot["keyframe"]),
                        "text": text,
                        "snippet": textindex._snippet(text, query),
                        "score": 0.0,
                        "meta": {"shot": shot["index"]},
                    }
                )
        return results[:limit]

    @staticmethod
    def _chapter_for_time(session: Session, timestamp: float) -> int | None:
        for chapter in session.chapters:
            if chapter["start"] <= timestamp <= chapter["end"]:
                return chapter["index"] + 1
        return None

    def transcript(
        self,
        session_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        limit: int = 400,
        offset: int = 0,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        if not session.transcript:
            return {
                "status": "success",
                "session_id": session.id,
                "source": "none",
                "count": 0,
                "has_more": False,
                "segments": [],
                "text": "",
                "note": (
                    "No captions were available for this video. Use get_audio for the audio "
                    "structure, search_video for on-screen text (OCR), and get_frames to read "
                    "the visuals directly."
                ),
            }
        low = 0.0 if start is None else max(0.0, float(start))
        high = session.duration if end is None else max(low, float(end))
        selected = [
            segment
            for segment in session.transcript
            if segment["end"] >= low and segment["start"] <= high
        ]
        total = len(selected)
        page = selected[max(0, offset) : max(0, offset) + max(1, min(1000, limit))]
        text = textindex.transcript_text(page, limit=len(page))
        if len(text) > 30000:
            text = text[:30000] + "\n…"
        return {
            "status": "success",
            "session_id": session.id,
            "source": session.transcript_source,
            "language": session.transcript_language,
            "total_segments": total,
            "count": len(page),
            "offset": max(0, offset),
            "has_more": max(0, offset) + len(page) < total,
            "segments": page,
            "text": text,
            "transcript_url": session.asset_url("transcript.txt"),
            "note": "Cite timestamps from 'start' or 'time' when quoting.",
        }

    async def frames(
        self,
        session_id: str,
        *,
        timestamps: list[float] | None = None,
        chapter: int | None = None,
        count: int = 4,
        start: float | None = None,
        end: float | None = None,
        ocr: bool = True,
        max_side: int = 1280,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        resolved = self._resolve_frame_times(
            session, timestamps=timestamps, chapter=chapter, count=count, start=start, end=end
        )
        if not resolved:
            raise AnalysisError("No valid timestamps to extract. Provide timestamps or a chapter.")

        deadline = time.monotonic() + config.OCR_BUDGET_SECONDS
        frames: list[dict[str, Any]] = []
        new_docs: list[dict[str, Any]] = []
        for timestamp in resolved:
            key = f"{round(timestamp, 2)}:{max_side}"
            cached = session.frame_cache.get(key)
            if cached is None:
                path = session.dir / "frames" / f"f-{int(round(timestamp * 10)):07d}-{max_side}.jpg"
                if not path.is_file():
                    async with self._media_guard():
                        await media.extract_frame(
                            session.video, timestamp, path, max_side=max_side, quality=3
                        )
                cached = {"timestamp": round(timestamp, 2), "path": str(path), "ocr": None}
                session.frame_cache[key] = cached
            entry = dict(cached)
            if ocr and config.OCR_ENABLED and entry.get("ocr") is None:
                entry["ocr"] = await self._ocr(Path(entry["path"]), deadline)
                if entry["ocr"].get("text"):
                    new_docs.append(
                        {
                            "kind": "ocr",
                            "start": entry["timestamp"],
                            "end": entry["timestamp"] + 1.0,
                            "text": entry["ocr"]["text"],
                            "meta": {"time": entry["timestamp"], "source": "frame"},
                        }
                    )
                session.frame_cache[key]["ocr"] = entry["ocr"]
            frames.append(entry)

        if new_docs and session.index_path is not None:
            await asyncio.to_thread(textindex.add_docs, session.index_path, new_docs)

        return {
            "status": "success",
            "session_id": session.id,
            "count": len(frames),
            "frames": [
                {
                    "timestamp": frame["timestamp"],
                    "time": textindex.format_time(frame["timestamp"]),
                    "ocr": (frame.get("ocr") or {}).get("text") or "",
                    "path": frame["path"],
                }
                for frame in frames
            ],
            "note": (
                "Each image below matches its frame entry by position and timestamp. "
                "Use these frames to describe exactly what is visible at each moment."
            ),
        }

    def _resolve_frame_times(
        self,
        session: Session,
        *,
        timestamps: list[float] | None,
        chapter: int | None,
        count: int,
        start: float | None,
        end: float | None,
    ) -> list[float]:
        duration = session.duration or session.media_info.duration or 1.0
        if timestamps:
            resolved = []
            for value in timestamps[: config.MAX_FRAMES_PER_CALL]:
                with contextlib.suppress(TypeError, ValueError):
                    resolved.append(round(_clamp(float(value), 0.0, max(0.0, duration - 0.15)), 2))
            return sorted(set(resolved))
        if chapter is not None:
            index = int(chapter) - 1
            if index < 0 or index >= len(session.chapters):
                raise AnalysisError(f"Chapter out of range. Choose 1–{len(session.chapters)}.")
            selected = session.chapters[index]
            return self._times_in_range(selected["start"], selected["end"], min(count, 6))
        low = 0.0 if start is None else _clamp(float(start), 0.0, duration)
        high = duration if end is None else _clamp(float(end), low, duration)
        return self._times_in_range(low, high, min(count, config.MAX_FRAMES_PER_CALL))

    @staticmethod
    def _times_in_range(start: float, end: float, count: int) -> list[float]:
        count = max(1, min(6, count))
        start = max(0.0, start)
        end = max(start, end)
        if end - start < 0.3:
            return [round(max(0.0, start), 2)]
        high = max(start + 0.05, end - 0.15)
        step = (high - start) / (count + 1)
        return [round(start + step * (index + 1), 2) for index in range(count)]

    async def motion(
        self,
        session_id: str,
        *,
        start: float | None = None,
        end: float | None = None,
        count: int = 8,
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        duration = session.duration or session.media_info.duration or 1.0
        low = 0.0 if start is None else _clamp(float(start), 0.0, duration)
        high = duration if end is None else _clamp(float(end), low, duration)
        count = int(_clamp(count, 4, 12))
        key = f"{round(low, 1)}:{round(high, 1)}:{count}"
        cached = session.motion_cache.get(key)
        if cached is not None:
            return cached

        safe_high = max(low + 0.05, high - 0.15)
        times = [
            round(low + (safe_high - low) * index / max(1, count - 1), 2)
            for index in range(count)
        ]
        frames: list[dict[str, Any]] = []
        for index, timestamp in enumerate(times):
            path = session.dir / "motion" / f"m-{int(round(timestamp * 10)):07d}.jpg"
            if not path.is_file():
                async with self._media_guard():
                    await media.extract_frame(session.video, timestamp, path, max_side=640, quality=4)
            motion_score = 0.0
            if frames:
                motion_score = await asyncio.to_thread(
                    media.motion_score, Path(frames[-1]["path"]), path
                )
            frames.append(
                {
                    "timestamp": timestamp,
                    "path": str(path),
                    "motion": round(motion_score, 3),
                    "label": f"{textindex.format_time(timestamp)}  Δ{motion_score:.2f}",
                }
            )

        sheet = session.dir / "motion" / f"strip-{abs(hash(key)) % 100000}.jpg"
        rows = max(1, math.ceil(len(frames) / 4))
        try:
            async with self._media_guard():
                await asyncio.to_thread(
                    media.compose_sheet,
                    [{"path": frame["path"], "label": frame["label"], "timestamp": frame["timestamp"]} for frame in frames],
                    sheet,
                    duration=max(0.01, high - low),
                    cols=4,
                    rows=rows,
                    width=1600,
                    height=max(400, 260 * rows),
                    header=f"Motion {textindex.format_time(low)}–{textindex.format_time(high)}",
                )
        except Exception:
            sheet = None

        scores = [frame["motion"] for frame in frames[1:]]
        peak = max(range(len(scores)), key=lambda index: scores[index]) if scores else 0
        peak_time = frames[peak + 1]["timestamp"] if scores else low
        payload = {
            "status": "success",
            "session_id": session.id,
            "start": low,
            "end": high,
            "start_time": textindex.format_time(low),
            "end_time": textindex.format_time(high),
            "count": len(frames),
            "average_motion": round(_mean(scores), 3),
            "peak_motion": round(max(scores), 3) if scores else 0.0,
            "peak_time": textindex.format_time(peak_time),
            "frames": [
                {"timestamp": frame["timestamp"], "time": textindex.format_time(frame["timestamp"]), "motion": frame["motion"], "path": frame["path"]}
                for frame in frames
            ],
            "sheet": str(sheet) if sheet else None,
            "note": (
                "Δ is the pixel change from the previous frame (0 = identical, 1 = completely "
                "different). The filmstrip shows the sampled frames in time order."
            ),
        }
        session.motion_cache[key] = payload
        return payload

    async def audio_images(self, session_id: str) -> dict[str, Any]:
        session = self.get_session(session_id)
        return await self._ensure_audio_images(session)

    async def _ensure_audio_images(self, session: Session) -> dict[str, Any]:
        if not session.audio.get("has_audio"):
            return {"has_audio": False, "images": []}
        if session.audio_images is not None:
            return session.audio_images
        audio_path = session.dir / str(session.audio.get("path") or "audio.wav")
        images: dict[str, str] = {}
        if audio_path.is_file():
            wave = session.dir / "waveform.png"
            spectro = session.dir / "spectrogram.png"
            try:
                async with self._media_guard():
                    if await media.waveform(audio_path, wave):
                        images["waveform"] = wave.name
            except media.MediaError:
                pass
            try:
                async with self._media_guard():
                    if await media.spectrogram(audio_path, spectro):
                        images["spectrogram"] = spectro.name
            except media.MediaError:
                pass
        session.audio_images = {"has_audio": True, "images": images}
        return session.audio_images

    async def audio(
        self, session_id: str, *, start: float | None = None, end: float | None = None
    ) -> dict[str, Any]:
        session = self.get_session(session_id)
        if not session.audio.get("has_audio"):
            return {
                "status": "success",
                "session_id": session.id,
                "has_audio": False,
                "note": "This video has no audio track.",
            }
        images = await self._ensure_audio_images(session)
        low = 0.0 if start is None else max(0.0, float(start))
        high = session.duration if end is None else max(low, float(end))
        events = [
            event
            for event in session.audio.get("events") or []
            if event.get("end", 0) >= low and event.get("start", 0) <= high
        ]
        return {
            "status": "success",
            "session_id": session.id,
            "has_audio": True,
            "mean_volume_db": session.audio.get("mean_volume_db"),
            "max_volume_db": session.audio.get("max_volume_db"),
            "silence_ratio": session.audio.get("silence_ratio"),
            "start": low,
            "end": high,
            "start_time": textindex.format_time(low),
            "end_time": textindex.format_time(high),
            "events": events[:200],
            "images": {
                "waveform": session.asset_url(images["images"]["waveform"])
                if images["images"].get("waveform")
                else None,
                "spectrogram": session.asset_url(images["images"]["spectrogram"])
                if images["images"].get("spectrogram")
                else None,
            },
            "image_paths": {
                "waveform": str(session.dir / images["images"]["waveform"])
                if images["images"].get("waveform")
                else None,
                "spectrogram": str(session.dir / images["images"]["spectrogram"])
                if images["images"].get("spectrogram")
                else None,
            },
            "note": (
                "'silence' events are >0.6s gaps below -35 dB — use them to find pauses "
                "and scene transitions in narration."
            ),
        }

    async def chapter(self, session_id: str, chapter: int, *, with_ocr: bool = True) -> dict[str, Any]:
        session = self.get_session(session_id)
        if not session.chapters:
            raise AnalysisError("This session has no chapters.")
        index = int(chapter) - 1
        if index < 0 or index >= len(session.chapters):
            raise AnalysisError(f"Chapter out of range. Choose 1–{len(session.chapters)}.")
        cached = session.detail_cache.get(index)
        if cached is None or (with_ocr and not cached.get("ocr_done")):
            async with session.lock:
                cached = session.detail_cache.get(index)
                if cached is None or (with_ocr and not cached.get("ocr_done")):
                    cached = await self._chapter_detail(session, index, with_ocr=with_ocr)
                    session.detail_cache[index] = cached

        detail = session.chapters[index]
        low, high = detail["start"], detail["end"]
        audio_events = [
            event
            for event in session.audio.get("events") or []
            if event.get("end", 0) >= low and event.get("start", 0) <= high
        ]
        transcript_text = textindex.segments_in_range(session.transcript, low, high, max_chars=3000)
        shots = [shot for shot in session.shots if shot["index"] in detail["shots"]]
        return {
            "status": "success",
            "session_id": session.id,
            "chapter": {
                "number": index + 1,
                "start": low,
                "end": high,
                "start_time": textindex.format_time(low),
                "end_time": textindex.format_time(high),
                "title": detail["title"],
                "summary": detail["summary"],
                "visual": detail["visual"],
                "colors": detail["colors"],
                "motion": detail["motion"],
            },
            "transcript": transcript_text,
            "ocr": [line for line in str(detail.get("ocr") or "").split(" | ") if line],
            "shots": [
                {
                    "index": shot["index"] + 1,
                    "time": textindex.format_time(shot["keyframe"]),
                    "start": shot["start"],
                    "end": shot["end"],
                    "motion": shot["motion"],
                    "visual": shot["features"],
                    "ocr": (shot.get("ocr") or {}).get("text") or "",
                }
                for shot in shots
            ],
            "audio_events": audio_events,
            "images": [
                {
                    "timestamp": frame["timestamp"],
                    "time": textindex.format_time(frame["timestamp"]),
                    "path": frame["path"],
                    "ocr": (frame.get("ocr") or {}).get("text") or "",
                }
                for frame in cached["frames"]
            ],
            "text_crop": cached.get("text_crop"),
            "note": (
                "Images are in timestamp order. The first text block above is the transcript "
                "for this chapter; OCR lines describe on-screen text. Cite the timestamps."
            ),
        }

    async def _chapter_detail(self, session: Session, index: int, *, with_ocr: bool) -> dict[str, Any]:
        detail = session.chapters[index]
        count = config.MAX_CHAPTER_IMAGES
        times = self._times_in_range(detail["start"], detail["end"], count)
        frames: list[dict[str, Any]] = []
        deadline = time.monotonic() + config.OCR_BUDGET_SECONDS
        best: dict[str, Any] | None = None
        for position, timestamp in enumerate(times):
            path = session.dir / "detail" / f"ch{index + 1:03d}-{position}.jpg"
            if not path.is_file():
                async with self._media_guard():
                    await media.extract_frame(session.video, timestamp, path, max_side=960, quality=3)
            entry: dict[str, Any] = {
                "timestamp": round(timestamp, 2),
                "path": str(path),
                "ocr": None,
            }
            if with_ocr and config.OCR_ENABLED:
                entry["ocr"] = await self._ocr(path, deadline)
                if best is None or len((entry["ocr"] or {}).get("text") or "") > len(
                    (best.get("ocr") or {}).get("text") or ""
                ):
                    best = entry
            frames.append(entry)

        text_crop: dict[str, Any] | None = None
        if best is not None:
            ocr = best.get("ocr") or {}
            if len(str(ocr.get("text") or "").strip()) >= 12 and ocr.get("boxes"):
                crop_path = session.dir / "detail" / f"ch{index + 1:03d}-text.jpg"
                async with self._media_guard():
                    result = await asyncio.to_thread(
                        media.make_text_crop, Path(best["path"]), ocr["boxes"], crop_path
                    )
                if result is not None:
                    text_crop = {
                        "timestamp": best["timestamp"],
                        "time": textindex.format_time(best["timestamp"]),
                        "path": str(result),
                        "text": ocr.get("text"),
                    }
        return {"frames": frames, "text_crop": text_crop, "ocr_done": with_ocr}

    # ---------------------------------------------------------------- session

    def list_sessions(self) -> dict[str, Any]:
        now = time.time()
        sessions = []
        for session in sorted(self.sessions.values(), key=lambda item: item.created_at, reverse=True):
            sessions.append(
                {
                    "session_id": session.id,
                    "title": session.title,
                    "platform": session.platform,
                    "duration_seconds": round(session.media_info.duration or session.duration, 1),
                    "chapters": len(session.chapters),
                    "has_transcript": bool(session.transcript),
                    "expires_in_minutes": max(0, int((session.expires_at - now) // 60)),
                }
            )
        return {
            "status": "success",
            "count": len(sessions),
            "sessions": sessions,
            "note": "Reuse a session_id within its lifetime instead of re-downloading.",
        }

    def close(self, session_id: str) -> dict[str, Any]:
        session = self.sessions.get(session_id)
        if session is None:
            return {"status": "success", "closed": False, "note": "No such session."}
        self._drop_session(session_id)
        return {
            "status": "success",
            "closed": True,
            "session_id": session_id,
            "note": "Analysis files were removed. The temporary video link may still work until it expires.",
        }

    def _drop_session(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            shutil.rmtree(session.dir, ignore_errors=True)

    def cleanup(self) -> int:
        now = time.time()
        removed = 0
        for session_id, session in list(self.sessions.items()):
            if session.expires_at <= now or self.store.get(session.bundle.stored.token) is None:
                self._drop_session(session_id)
                removed += 1
        return removed
