from __future__ import annotations

import asyncio
import contextlib
import html
import json
import mimetypes
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
import yt_dlp
from mcp.server.mcpserver import Image, MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import TextContent
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import config
from .analysis import AnalysisError, VideoAnalyzer
from .downloader import BusyError, DownloadError, RateLimitError, VideoDownloader
from .player import render_player, watch_page_data
from .security import UnsafeURLError
from .storage import FileStore

START_TIME = time.time()

store = FileStore(
    config.DOWNLOADS_DIR,
    ttl_seconds=config.FILE_TTL_SECONDS,
    max_total_bytes=config.MAX_STORAGE_BYTES,
)
downloader = VideoDownloader(store)
analyzer = VideoAnalyzer(store, downloader)

_PLAYER_URI = "ui://video-analysis/player.html"

_STRIP_KEYS = {"path", "sheet", "contact_sheet_path", "image_paths"}


def _clean(value: Any) -> Any:
    """Remove server-local paths before text reaches the model."""
    if isinstance(value, dict):
        return {key: _clean(item) for key, item in value.items() if key not in _STRIP_KEYS}
    if isinstance(value, list):
        return [_clean(item) for item in value]
    return value


def _text(payload: Any) -> TextContent:
    if isinstance(payload, str):
        body = payload
    else:
        body = json.dumps(_clean(payload), ensure_ascii=False, indent=2)
    return TextContent(type="text", text=body)


def _error(message: str, hint: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error": message}
    if hint:
        payload["hint"] = hint
    return payload


async def _cleanup_loop() -> None:
    while True:
        try:
            store.cleanup()
            analyzer.cleanup()
        except Exception:
            pass
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(_server: MCPServer):
    task = asyncio.create_task(_cleanup_loop())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


_INSTRUCTIONS = (
    "This server downloads and deeply analyzes public videos from YouTube, TikTok, Instagram, "
    "Facebook, X/Twitter, Reddit, Vimeo, Twitch clips, Dailymotion, and many other sites. "
    "For downloading, use download_video or download_audio and show the returned link. "
    "For understanding a video, call open_video once with the URL: it downloads the video, "
    "builds a timestamped textual map (chapters, captions, OCR, audio structure) and returns a "
    "session_id plus a visual contact sheet. Then work from that session_id: get_timeline for the "
    "full map, get_chapter(session_id, n) for real frames and exact text per chapter, "
    "search_video to find a phrase or on-screen text, get_frames for specific timestamps, "
    "get_motion to see change over time, get_audio for waveform/spectrogram, and get_transcript "
    "for the full caption track. Always reuse an existing session_id instead of calling open_video "
    "again. Cite timestamps when you quote or describe something. open_video can take up to a "
    "couple of minutes on this free server; call it once and be patient."
)


mcp = MCPServer(
    name=config.APP_NAME,
    title=config.APP_TITLE,
    version=config.VERSION,
    instructions=_INSTRUCTIONS,
    lifespan=lifespan,
)


# ---------------------------------------------------------------- download tools


@mcp.tool()
async def download_video(url: str, quality: str = "720p") -> dict:
    """Download a public video from almost any website and return a temporary direct link.

    Supports YouTube, TikTok, Instagram, Facebook, X/Twitter, Reddit, Vimeo, Twitch clips,
    Dailymotion, Streamable, and many other platforms. Use this whenever the user asks to
    download, save, fetch, or get a video from a URL.

    Args:
        url: The public video page URL, e.g. https://www.youtube.com/watch?v=... or a TikTok link.
        quality: Preferred quality: "360p", "480p", "720p" (default), "1080p", or "best".

    Returns:
        A JSON object with status, title, platform, file_size_mb, and download_url.
    """
    try:
        return await downloader.download_video(url, quality)
    except (DownloadError, UnsafeURLError) as exc:
        return _error(str(exc), "Check that the URL is public and points to a video post.")
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")


@mcp.tool()
async def download_audio(url: str) -> dict:
    """Extract audio (MP3, 192 kbps) from a public video and return a temporary direct link.

    Use this when the user asks for the audio, MP3, song, sound, or voice track from a video.

    Args:
        url: The public video page URL.

    Returns:
        A JSON object with status, title, file_size_mb, and download_url.
    """
    try:
        return await downloader.download_audio(url)
    except (DownloadError, UnsafeURLError) as exc:
        return _error(str(exc), "Check that the URL is public and points to a video post.")
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")


@mcp.tool()
async def get_video_info(url: str) -> dict:
    """Get metadata for a public video URL without downloading it.

    Returns title, platform, uploader, duration, thumbnail, view count, and available
    qualities. Use before downloading when the user wants details or quality options.

    Args:
        url: The public video page URL.
    """
    try:
        return await downloader.get_info(url)
    except (DownloadError, UnsafeURLError) as exc:
        return _error(str(exc), "Check that the URL is public and points to a video post.")
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")


# ---------------------------------------------------------------- analysis tools


@mcp.tool(
    title="Open and analyze a video",
    meta={"ui": {"resourceUri": _PLAYER_URI}},
    structured_output=False,
)
async def open_video(url: str, quality: str = "720p") -> Any:
    """Download a public video and build a complete timestamped analysis session.

    This is the entry point for understanding a video. It downloads the video, extracts the
    caption/transcript track, indexes keyframes into chapters, reads on-screen text when
    available, analyzes the audio timeline, and returns a contact sheet plus a full textual
    map with a session_id.

    Use this once, then reuse the session_id with get_timeline, get_chapter, search_video,
    get_frames, get_motion, get_audio, and get_transcript. It can take up to a couple of
    minutes on the free server.

    Args:
        url: Public video page URL.
        quality: "360p", "480p", "720p" (default), or "1080p". Lower is faster.

    Returns:
        A JSON payload (session_id, metadata, chapter timeline, playback/watch URLs) plus a
        visual contact sheet image.
    """
    try:
        payload = await analyzer.open(url, quality)
    except (DownloadError, UnsafeURLError, AnalysisError) as exc:
        return _error(str(exc), "Check that the URL is public and points to a video post.")
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")

    content: list[Any] = [_text(payload)]
    sheet = payload.get("contact_sheet_path")
    if sheet and Path(sheet).is_file():
        content.append(
            _text(
                "Visual contact sheet: keyframes in time order, each labelled with its shot "
                "number and timestamp."
            )
        )
        content.append(Image(path=sheet))
    return content


@mcp.tool()
async def get_timeline(session_id: str) -> dict:
    """Return the full textual map of an analyzed video.

    Gives every chapter with start/end timestamps, a summary built from captions, OCR, and
    visual stats, plus playback URLs. Use this right after open_video and to plan which
    chapters deserve a closer look.

    Args:
        session_id: The session_id returned by open_video.
    """
    try:
        return _clean(analyzer.timeline(session_id))
    except AnalysisError as exc:
        return _error(str(exc))


@mcp.tool(structured_output=False)
async def get_chapter(session_id: str, chapter: int) -> Any:
    """Get detailed evidence for one chapter: real frames plus exact text.

    Returns the chapter transcript, on-screen OCR text, per-shot visual statistics and motion
    scores, audio events, and several real frames (a small text crop is added when on-screen
    text was detected). Call this for the chapters that matter after reading get_timeline.

    Args:
        session_id: Session id from open_video.
        chapter: 1-based chapter number from the timeline.
    """
    try:
        payload = await analyzer.chapter(session_id, chapter)
    except AnalysisError as exc:
        return _error(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")

    content: list[Any] = [_text(payload)]
    for frame in payload.get("images") or []:
        path = frame.get("path")
        if path and Path(path).is_file():
            label = f"Frame at {frame.get('time')}"
            if frame.get("ocr"):
                label += " — on-screen text: " + str(frame["ocr"])[:200]
            content.append(_text(label))
            content.append(Image(path=path))
    crop = payload.get("text_crop")
    if crop and crop.get("path") and Path(crop["path"]).is_file():
        content.append(_text("Zoomed crop of the on-screen text at " + str(crop.get("time"))))
        content.append(Image(path=crop["path"]))
    return content


@mcp.tool()
async def search_video(session_id: str, query: str, kind: str = "all", limit: int = 8) -> dict:
    """Search everything said or shown in an analyzed video.

    Searches the caption transcript, on-screen OCR text, and chapter summaries, and returns
    matching moments with timestamps. Use this for questions like "when do they mention X" or
    "where is Y on screen".

    Args:
        session_id: Session id from open_video.
        query: Words or a phrase to find.
        kind: "all" (default), "transcript", "ocr", or "chapter".
        limit: Maximum results (default 8).
    """
    try:
        return _clean(analyzer.search(session_id, query, kind=kind, limit=limit))
    except AnalysisError as exc:
        return _error(str(exc))


@mcp.tool()
async def get_transcript(
    session_id: str,
    start: float | None = None,
    end: float | None = None,
    limit: int = 400,
    offset: int = 0,
) -> dict:
    """Return the full caption/transcript track with timestamps.

    Use for quoting exact wording, summarising long stretches, or translating. Filter with
    start/end seconds and page with limit/offset for very long videos.

    Args:
        session_id: Session id from open_video.
        start: Optional start time in seconds.
        end: Optional end time in seconds.
        limit: Max segments to return (default 400).
        offset: Skip this many segments (for paging).
    """
    try:
        return _clean(analyzer.transcript(session_id, start=start, end=end, limit=limit, offset=offset))
    except AnalysisError as exc:
        return _error(str(exc))


@mcp.tool(structured_output=False)
async def get_frames(
    session_id: str,
    timestamps: list[float] | None = None,
    chapter: int | None = None,
    count: int = 4,
    start: float | None = None,
    end: float | None = None,
    ocr: bool = True,
    max_side: int = 1280,
) -> Any:
    """Extract real frames at specific moments and read any on-screen text.

    Use for "show me the frame at 1:30", reading a sign, chart, product, or caption, or
    verifying what is visible. Provide timestamps directly, or a chapter number, or a
    start/end range with count.

    Args:
        session_id: Session id from open_video.
        timestamps: Explicit timestamps in seconds (max 6).
        chapter: Instead of timestamps, sample evenly across this 1-based chapter.
        count: Number of frames when using chapter/range (default 4, max 6).
        start: Optional range start in seconds.
        end: Optional range end in seconds.
        ocr: Run text recognition on each frame (default True).
        max_side: Frame resolution cap in pixels (default 1280).
    """
    try:
        payload = await analyzer.frames(
            session_id,
            timestamps=timestamps,
            chapter=chapter,
            count=count,
            start=start,
            end=end,
            ocr=ocr,
            max_side=max_side,
        )
    except AnalysisError as exc:
        return _error(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")

    content: list[Any] = [_text(payload)]
    for frame in payload.get("frames") or []:
        path = frame.get("path")
        if path and Path(path).is_file():
            label = f"Frame at {frame.get('time')}"
            if frame.get("ocr"):
                label += " — on-screen text: " + str(frame["ocr"])[:200]
            content.append(_text(label))
            content.append(Image(path=path))
    return content


@mcp.tool(structured_output=False)
async def get_motion(
    session_id: str,
    start: float | None = None,
    end: float | None = None,
    count: int = 8,
) -> Any:
    """Measure and show visual change over a time range.

    Returns per-frame motion scores (pixel change versus the previous frame), the peak moment,
    and a labelled filmstrip. Use to find cuts, action, or static talking-head stretches.

    Args:
        session_id: Session id from open_video.
        start: Optional range start in seconds (default 0).
        end: Optional range end in seconds (default whole video).
        count: Sampled frames, 4–12 (default 8).
    """
    try:
        payload = await analyzer.motion(session_id, start=start, end=end, count=count)
    except AnalysisError as exc:
        return _error(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")

    content: list[Any] = [_text(payload)]
    sheet = payload.get("sheet")
    if sheet and Path(sheet).is_file():
        content.append(_text("Motion filmstrip in time order. Δ is change from the previous frame."))
        content.append(Image(path=sheet))
    return content


@mcp.tool(structured_output=False)
async def get_audio(
    session_id: str,
    start: float | None = None,
    end: float | None = None,
) -> Any:
    """Return the audio structure: speech-vs-silence timeline, waveform, and spectrogram.

    Use to find pauses, music, shouting, or section boundaries, and to reason about delivery
    and pacing alongside the transcript.

    Args:
        session_id: Session id from open_video.
        start: Optional range start in seconds.
        end: Optional range end in seconds.
    """
    try:
        payload = await analyzer.audio(session_id, start=start, end=end)
    except AnalysisError as exc:
        return _error(str(exc))
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")

    content: list[Any] = [_text(payload)]
    paths = payload.get("image_paths") or {}
    for name, label in (("waveform", "Audio waveform"), ("spectrogram", "Audio spectrogram")):
        path = paths.get(name)
        if path and Path(path).is_file():
            content.append(_text(label))
            content.append(Image(path=path))
    return content


@mcp.tool()
async def list_sessions() -> dict:
    """List active analysis sessions with titles, durations, and remaining lifetime.

    Use when a session_id was lost or to pick between several analyzed videos. Sessions expire
    with the temporary file link (60 minutes by default).
    """
    return analyzer.list_sessions()


@mcp.tool()
async def close_video(session_id: str) -> dict:
    """Delete an analysis session and its cached frames to free server space.

    Call when finished with a video. The temporary download link keeps working until it
    expires on its own.

    Args:
        session_id: Session id from open_video.
    """
    return analyzer.close(session_id)


# ------------------------------------------------------------------- resources


@mcp.resource(
    _PLAYER_URI,
    name="Video analysis player",
    title="Video analysis player",
    description="Inline player with chapters, transcript, and search for an analysis session.",
    mime_type="text/html;profile=mcp-app",
    meta={
        "ui": {
            "prefersBorder": True,
            "csp": {
                "connectDomains": [config.PUBLIC_BASE_URL],
                "resourceDomains": [config.PUBLIC_BASE_URL],
            },
        }
    },
)
def video_player_resource() -> str:
    return render_player(None)


# ---------------------------------------------------------------------- routes


async def _serve_file(request: Request) -> Response:
    token = request.path_params.get("token", "")
    item = store.get(token)
    if item is None:
        return JSONResponse(
            {"error": "File not found or the link has expired. Download it again."},
            status_code=404,
        )
    return FileResponse(
        item.path,
        media_type=item.content_type,
        filename=item.filename,
        headers={
            "Cache-Control": "private, max-age=300",
            "X-Content-Type-Options": "nosniff",
        },
    )


@mcp.custom_route("/files/{token}/{filename:path}", methods=["GET", "HEAD"])
async def serve_file(request: Request) -> Response:
    return await _serve_file(request)


@mcp.custom_route("/files/{token}", methods=["GET", "HEAD"])
async def serve_file_short(request: Request) -> Response:
    return await _serve_file(request)


@mcp.custom_route("/analysis/{session_id}/{filename:path}", methods=["GET", "HEAD"])
async def serve_analysis_asset(request: Request) -> Response:
    session_id = request.path_params.get("session_id", "")
    filename = request.path_params.get("filename", "")
    try:
        session = analyzer.get_session(session_id)
    except AnalysisError:
        return JSONResponse({"error": "Session not found or expired."}, status_code=404)
    candidate = (session.dir / filename).resolve()
    try:
        candidate.relative_to(session.dir.resolve())
    except ValueError:
        return JSONResponse({"error": "Invalid asset path."}, status_code=400)
    if not candidate.is_file():
        return JSONResponse({"error": "Asset not found."}, status_code=404)
    media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
    return FileResponse(
        candidate,
        media_type=media_type,
        headers={"Cache-Control": "private, max-age=300", "X-Content-Type-Options": "nosniff"},
    )


@mcp.custom_route("/watch/{session_id}", methods=["GET"])
async def watch(request: Request) -> Response:
    session_id = request.path_params.get("session_id", "")
    try:
        session = analyzer.get_session(session_id)
    except AnalysisError:
        return HTMLResponse(
            "<!doctype html><meta charset='utf-8'><body style='background:#0b0f17;color:#e8eef8;"
            "font-family:system-ui;padding:40px'><h2>Session expired</h2>"
            "<p>Ask the assistant to open the video again.</p></body>",
            status_code=404,
        )
    return HTMLResponse(render_player(watch_page_data(session)))


@mcp.custom_route("/health", methods=["GET", "HEAD"])
async def health(request: Request) -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "service": config.APP_NAME,
            "version": config.VERSION,
            "yt_dlp": yt_dlp.version.__version__,
            "uptime_seconds": int(time.time() - START_TIME),
            "active_downloads": downloader.active,
            "analysis": analyzer.stats(),
            "files": store.stats(),
            "capabilities": {
                "ffmpeg": Path(config.FFMPEG_BIN).name,
                "ffprobe": Path(config.FFPROBE_BIN).name,
                "ocr": config.OCR_ENABLED,
                "ocr_lang": config.OCR_LANG,
                "captions": True,
                "search": True,
            },
            "limits": {
                "max_file_mb": config.MAX_FILE_MB,
                "max_concurrent_downloads": config.MAX_CONCURRENT_DOWNLOADS,
                "downloads_per_hour": config.MAX_DOWNLOADS_PER_HOUR,
                "analysis_max_seconds": config.ANALYSIS_MAX_SECONDS,
                "max_chapters": config.MAX_CHAPTERS,
            },
        },
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


@mcp.custom_route("/ping", methods=["GET", "HEAD"])
async def ping(request: Request) -> Response:
    """Minimal keep-alive target for uptime monitors."""
    return Response(
        "pong",
        media_type="text/plain",
        headers={"Cache-Control": "no-store", "Access-Control-Allow-Origin": "*"},
    )


@mcp.custom_route("/", methods=["GET"])
async def index(request: Request) -> Response:
    base = html.escape(config.PUBLIC_BASE_URL)
    endpoint = html.escape(f"{config.PUBLIC_BASE_URL}/mcp")
    page = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(config.APP_TITLE)}</title>
<style>
  :root {{ color-scheme: dark; --bg:#0b0f17; --card:#141b27; --line:#243044; --text:#e8eef8; --muted:#93a4bd; --accent:#5eead4; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; background:radial-gradient(1200px 600px at 50% -10%, #16233a 0%, var(--bg) 60%); color:var(--text); font:16px/1.6 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }}
  main {{ max-width:900px; margin:0 auto; padding:56px 22px 80px; }}
  .badge {{ display:inline-flex; align-items:center; gap:8px; border:1px solid var(--line); background:#0f1722; color:var(--muted); border-radius:999px; padding:6px 12px; font-size:13px; }}
  .dot {{ width:8px; height:8px; border-radius:50%; background:var(--accent); box-shadow:0 0 12px var(--accent); }}
  h1 {{ font-size:clamp(30px,5vw,46px); line-height:1.1; margin:20px 0 12px; letter-spacing:-.02em; }}
  h2 {{ font-size:13px; letter-spacing:.08em; text-transform:uppercase; color:var(--muted); margin:0 0 12px; }}
  p.lead {{ color:var(--muted); font-size:18px; margin:0 0 30px; max-width:700px; }}
  .card {{ background:linear-gradient(180deg,#151d2a,#111823); border:1px solid var(--line); border-radius:16px; padding:22px; margin:16px 0; }}
  code, .url {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:14px; }}
  .url {{ display:block; background:#0a1018; border:1px solid var(--line); border-radius:10px; padding:14px 16px; color:var(--accent); overflow-wrap:anywhere; }}
  ol {{ margin:10px 0 0; padding-left:22px; }}
  li {{ margin:8px 0; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(250px,1fr)); gap:14px; }}
  .tool {{ background:#0d141e; border:1px solid var(--line); border-radius:12px; padding:16px; }}
  .tool b {{ color:var(--accent); font-family:ui-monospace,monospace; font-size:14px; }}
  .tool span {{ display:block; color:var(--muted); font-size:14px; margin-top:6px; }}
  .muted {{ color:var(--muted); font-size:14px; }}
  a {{ color:var(--accent); }}
  footer {{ margin-top:36px; color:var(--muted); font-size:13px; }}
</style>
</head>
<body>
<main>
  <div class="badge"><span class="dot"></span> Server online</div>
  <h1>Video Downloader &amp; Analysis MCP</h1>
  <p class="lead">Give ChatGPT (or any MCP client) the ability to download public videos — and to actually understand them. It builds a timestamped map of chapters, captions, on-screen text, audio structure, motion, and real frames, then answers questions with exact timestamps.</p>

  <div class="card">
    <h2>MCP endpoint</h2>
    <code class="url">{endpoint}</code>
    <p class="muted">ChatGPT → Settings → Connectors → Advanced → Developer mode → Add custom connector. Choose “No authentication”.</p>
  </div>

  <div class="card">
    <h2>Analyze a video</h2>
    <div class="grid">
      <div class="tool"><b>open_video</b><span>Download and build a full analysis session with a contact sheet and chapter map.</span></div>
      <div class="tool"><b>get_timeline</b><span>The complete timestamped textual map of every chapter.</span></div>
      <div class="tool"><b>get_chapter</b><span>Real frames, transcript, OCR, motion and audio for one chapter.</span></div>
      <div class="tool"><b>search_video</b><span>Find any phrase said or any text shown, with timestamps.</span></div>
      <div class="tool"><b>get_frames</b><span>Pull exact frames at chosen moments and read on-screen text.</span></div>
      <div class="tool"><b>get_motion</b><span>Measure and visualise change, cuts and action over time.</span></div>
      <div class="tool"><b>get_audio</b><span>Waveform, spectrogram and speech-vs-silence timeline.</span></div>
      <div class="tool"><b>get_transcript</b><span>The full caption track, ready to quote or translate.</span></div>
    </div>
  </div>

  <div class="card">
    <h2>Download a video</h2>
    <div class="grid">
      <div class="tool"><b>download_video</b><span>Download a video and return a temporary direct MP4 link.</span></div>
      <div class="tool"><b>download_audio</b><span>Extract MP3 audio and return a temporary direct link.</span></div>
      <div class="tool"><b>get_video_info</b><span>Read title, duration, uploader, and available qualities.</span></div>
    </div>
  </div>

  <div class="card">
    <h2>How to use</h2>
    <ol>
      <li>Open ChatGPT → Settings → Connectors → Advanced → Developer mode.</li>
      <li>Add a custom connector with the MCP URL above and no authentication.</li>
      <li>Ask: <em>“Analyze this video and tell me what happens: &lt;link&gt;”</em> or <em>“Download this video: &lt;link&gt;”</em>.</li>
      <li>For a video you analyzed, ask follow-ups like <em>“What did they say about price?”</em> or <em>“Show the frame at 1:30.”</em></li>
    </ol>
  </div>

  <div class="card">
    <h2>Limits</h2>
    <p class="muted">Max {config.MAX_FILE_MB} MB per file · {config.MAX_DOWNLOADS_PER_HOUR} downloads/hour · analysis covers up to {config.ANALYSIS_MAX_SECONDS // 60} minutes and up to {config.MAX_CHAPTERS} chapters · links and sessions expire after {max(1, config.FILE_TTL_SECONDS // 60)} minutes · 1080p max · live streams are not supported. Private, paid, or login-only content may fail. On the free tier the server sleeps when idle, so sessions can end sooner.</p>
  </div>

  <footer>Base URL: {base} · <a href="/health">health</a> · Powered by yt-dlp, ffmpeg{', and Tesseract OCR' if config.OCR_ENABLED else ''}.</footer>
</main>
</body>
</html>"""
    return HTMLResponse(page)


app = mcp.streamable_http_app(
    streamable_http_path="/mcp",
    json_response=True,
    stateless_http=True,
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    host="0.0.0.0",
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Mcp-Session-Id", "Content-Disposition"],
)


def main() -> None:
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level=os.getenv("LOG_LEVEL", "info").lower(),
        timeout_keep_alive=75,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
