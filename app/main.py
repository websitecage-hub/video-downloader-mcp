from __future__ import annotations

import asyncio
import contextlib
import html
import os
import time
from contextlib import asynccontextmanager
from typing import Any

import uvicorn
import yt_dlp
from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.middleware.cors import CORSMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, HTMLResponse, JSONResponse, Response

from . import config
from .downloader import BusyError, DownloadError, RateLimitError, VideoDownloader
from .security import UnsafeURLError
from .storage import FileStore

START_TIME = time.time()

store = FileStore(
    config.DOWNLOADS_DIR,
    ttl_seconds=config.FILE_TTL_SECONDS,
    max_total_bytes=config.MAX_STORAGE_BYTES,
)
downloader = VideoDownloader(store)


async def _cleanup_loop() -> None:
    while True:
        try:
            store.cleanup()
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


mcp = MCPServer(
    name=config.APP_NAME,
    title=config.APP_TITLE,
    version=config.VERSION,
    instructions=(
        "This server downloads public videos and audio from YouTube, TikTok, Instagram, "
        "Facebook, X/Twitter, Reddit, Vimeo, Twitch clips, Dailymotion, and many other sites. "
        "When the user shares a video URL and asks to download, save, or fetch it, call "
        "download_video. If they ask for audio or MP3, call download_audio. Use get_video_info "
        "first only when you need details or available qualities. "
        "The download tools return a temporary public download_url. Always show that URL to the "
        "user as a clickable link and mention that it expires. You cannot attach the binary file "
        "to the chat; the link is the deliverable."
    ),
    lifespan=lifespan,
)


def _error(message: str, hint: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": "error", "error": message}
    if hint:
        payload["hint"] = hint
    return payload


@mcp.tool()
async def download_video(url: str, quality: str = "720p") -> dict:
    """Download a public video from almost any website and return a temporary direct link.

    Supports YouTube, TikTok, Instagram, Facebook, X/Twitter, Reddit, Vimeo, Twitch clips,
    Dailymotion, Streamable, and many other platforms. Use this whenever the user asks to
    download, save, fetch, or get a video from a URL.

    Args:
        url: The public video page URL, e.g. https://www.youtube.com/watch?v=... or a TikTok link.
        quality: Preferred quality: "360p", "480p", "720p" (default), "1080p", or "best".
            Higher quality takes longer and may exceed the server's size limit.

    Returns:
        A JSON object with status, title, platform, file_size_mb, and download_url.
        Present download_url to the user as a clickable link. The link is temporary.
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
    Supports the same sites as download_video.

    Args:
        url: The public video page URL.

    Returns:
        A JSON object with status, title, file_size_mb, and download_url. The link is temporary.
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

    Returns the title, platform, uploader, duration, thumbnail, view count, and the
    available download qualities. Use this when the user wants details about a video or
    asks what qualities are available before downloading.

    Args:
        url: The public video page URL.
    """
    try:
        return await downloader.get_info(url)
    except (DownloadError, UnsafeURLError) as exc:
        return _error(str(exc), "Check that the URL is public and points to a video post.")
    except Exception as exc:  # pragma: no cover - defensive
        return _error(f"Unexpected server error: {exc}")


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


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> Response:
    return JSONResponse(
        {
            "status": "ok",
            "service": config.APP_NAME,
            "version": config.VERSION,
            "yt_dlp": yt_dlp.version.__version__,
            "uptime_seconds": int(time.time() - START_TIME),
            "active_downloads": downloader.active,
            "files": store.stats(),
            "limits": {
                "max_file_mb": config.MAX_FILE_MB,
                "max_concurrent_downloads": config.MAX_CONCURRENT_DOWNLOADS,
                "downloads_per_hour": config.MAX_DOWNLOADS_PER_HOUR,
            },
        }
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
  main {{ max-width:860px; margin:0 auto; padding:56px 22px 80px; }}
  .badge {{ display:inline-flex; align-items:center; gap:8px; border:1px solid var(--line); background:#0f1722; color:var(--muted); border-radius:999px; padding:6px 12px; font-size:13px; }}
  .dot {{ width:8px; height:8px; border-radius:50%; background:var(--accent); box-shadow:0 0 12px var(--accent); }}
  h1 {{ font-size:clamp(30px,5vw,46px); line-height:1.1; margin:20px 0 12px; letter-spacing:-.02em; }}
  p.lead {{ color:var(--muted); font-size:18px; margin:0 0 30px; max-width:680px; }}
  .card {{ background:linear-gradient(180deg,#151d2a,#111823); border:1px solid var(--line); border-radius:16px; padding:22px; margin:16px 0; }}
  .label {{ color:var(--muted); font-size:12px; letter-spacing:.08em; text-transform:uppercase; margin-bottom:8px; }}
  code, .url {{ font-family:ui-monospace,SFMono-Regular,Menlo,monospace; font-size:14px; }}
  .url {{ display:block; background:#0a1018; border:1px solid var(--line); border-radius:10px; padding:14px 16px; color:var(--accent); overflow-wrap:anywhere; }}
  ol {{ margin:10px 0 0; padding-left:22px; }}
  li {{ margin:8px 0; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:14px; }}
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
  <h1>Video Downloader MCP</h1>
  <p class="lead">Give ChatGPT (or any MCP client) the ability to download public videos and audio from YouTube, TikTok, Instagram, Facebook, X, Reddit, Vimeo, Twitch clips, and many more sites.</p>

  <div class="card">
    <div class="label">MCP endpoint</div>
    <code class="url">{endpoint}</code>
    <p class="muted">Use this URL in ChatGPT → Settings → Connectors → Advanced → Developer mode → Add custom connector. Choose “No authentication”.</p>
  </div>

  <div class="card">
    <div class="label">Tools</div>
    <div class="grid">
      <div class="tool"><b>download_video</b><span>Download a video and return a temporary direct MP4 link.</span></div>
      <div class="tool"><b>download_audio</b><span>Extract MP3 audio and return a temporary direct link.</span></div>
      <div class="tool"><b>get_video_info</b><span>Read title, duration, uploader, and available qualities.</span></div>
    </div>
  </div>

  <div class="card">
    <div class="label">How to use</div>
    <ol>
      <li>Open ChatGPT → Settings → Connectors → Advanced → Developer mode.</li>
      <li>Add a custom connector with the MCP URL above and no authentication.</li>
      <li>Ask: <em>“Download this video: &lt;link&gt;”</em> and open the download link ChatGPT returns.</li>
    </ol>
  </div>

  <div class="card">
    <div class="label">Limits</div>
    <p class="muted">Max {config.MAX_FILE_MB} MB per file · {config.MAX_DOWNLOADS_PER_HOUR} downloads/hour · links expire after {max(1, config.FILE_TTL_SECONDS // 60)} minutes · 1080p max · live streams are not supported. Private, paid, or login-only content may fail.</p>
  </div>

  <footer>Base URL: {base} · <a href="/health">health</a> · Powered by yt-dlp and ffmpeg.</footer>
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
