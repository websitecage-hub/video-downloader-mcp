from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_for(url: str, timeout: float = 45) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2) as response:
                if response.status == 200:
                    return
        except Exception as exc:  # noqa: BLE001
            last_error = exc
        time.sleep(0.3)
    raise RuntimeError(f"Timed out waiting for {url}: {last_error}")


def make_sample_video(path: Path) -> bool:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        try:
            import imageio_ffmpeg

            ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            return False
    result = subprocess.run(
        [
            ffmpeg,
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x180:rate=15",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440",
            "-t",
            "2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ],
        capture_output=True,
        timeout=60,
    )
    return result.returncode == 0 and path.exists() and path.stat().st_size > 1000


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    media_dir = tmp_path_factory.mktemp("media")
    sample = media_dir / "sample.mp4"
    if not make_sample_video(sample):
        pytest.skip("ffmpeg is not available to generate the test video")

    media_port = free_port()
    media = subprocess.Popen(
        [PY, "-m", "http.server", str(media_port), "--bind", "127.0.0.1"],
        cwd=media_dir,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    app_port = free_port()
    downloads = tmp_path_factory.mktemp("downloads")
    env = os.environ.copy()
    env.update(
        {
            "PORT": str(app_port),
            "PUBLIC_BASE_URL": f"http://127.0.0.1:{app_port}",
            "DOWNLOADS_DIR": str(downloads),
            "ALLOW_PRIVATE_URLS": "1",
            "MAX_DOWNLOADS_PER_HOUR": "100",
            "MAX_CONCURRENT_DOWNLOADS": "2",
        }
    )
    if not shutil.which("ffmpeg"):
        try:
            import imageio_ffmpeg

            ffmpeg_dir = Path(imageio_ffmpeg.get_ffmpeg_exe()).parent
            env["PATH"] = f"{ffmpeg_dir}:{env.get('PATH', '')}"
        except Exception:
            pass

    app = subprocess.Popen(
        [PY, "-m", "app.main"],
        cwd=ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        wait_for(f"http://127.0.0.1:{app_port}/health")
        yield {
            "app_port": app_port,
            "media_port": media_port,
            "media_url": f"http://127.0.0.1:{media_port}/sample.mp4",
            "app": app,
        }
    finally:
        for process in (app, media):
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()


@pytest.mark.asyncio
async def test_mcp_end_to_end(servers):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    app_port = servers["app_port"]
    media_url = servers["media_url"]

    async with streamable_http_client(f"http://127.0.0.1:{app_port}/mcp") as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {"download_video", "download_audio", "get_video_info"} <= names

            info_result = await session.call_tool("get_video_info", {"url": media_url})
            assert not info_result.is_error
            info = json.loads(info_result.content[0].text)
            assert info["status"] == "success"
            assert info["title"] == "sample"

            result = await session.call_tool(
                "download_video", {"url": media_url, "quality": "360p"}
            )
            assert not result.is_error
            payload = json.loads(result.content[0].text)
            assert payload["status"] == "success", payload
            assert payload["download_url"].startswith(f"http://127.0.0.1:{app_port}/files/")

            with urllib.request.urlopen(payload["download_url"], timeout=20) as response:
                body = response.read()
                assert response.headers["Content-Type"] == "video/mp4"
            assert len(body) > 1000

            audio_result = await session.call_tool("download_audio", {"url": media_url})
            assert not audio_result.is_error
            audio = json.loads(audio_result.content[0].text)
            assert audio["status"] == "success", audio
            with urllib.request.urlopen(audio["download_url"], timeout=20) as response:
                audio_body = response.read()
            assert len(audio_body) > 500


def test_health_and_pages(servers):
    app_port = servers["app_port"]
    with urllib.request.urlopen(f"http://127.0.0.1:{app_port}/health", timeout=10) as response:
        health = json.loads(response.read())
    assert health["status"] == "ok"
    assert health["yt_dlp"]

    with urllib.request.urlopen(f"http://127.0.0.1:{app_port}/", timeout=10) as response:
        page = response.read().decode()
    assert "Video Downloader MCP" in page
    assert f"/mcp" in page

    with pytest.raises(urllib.error.HTTPError) as exc_info:
        urllib.request.urlopen(f"http://127.0.0.1:{app_port}/files/nope/x.mp4", timeout=10)
    assert exc_info.value.code == 404
