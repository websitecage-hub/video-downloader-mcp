from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from app import config, media
from app.analysis import VideoAnalyzer
from app.downloader import DownloadBundle, VideoDownloader
from app.storage import FileStore


def _ffmpeg_ok() -> bool:
    try:
        result = subprocess.run(
            [config.FFMPEG_BIN, "-version"], capture_output=True, timeout=20
        )
        return result.returncode == 0
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ffmpeg_ok(), reason="ffmpeg is not available")


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("sample") / "sample.mp4"
    cmd = [
        config.FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        "testsrc=size=320x180:rate=10:duration=6",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=6",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        "-g",
        "10",
        "-c:a",
        "aac",
        "-shortest",
        str(path),
    ]
    result = subprocess.run(cmd, capture_output=True, timeout=180)
    if result.returncode != 0 or not path.is_file():
        pytest.skip("could not build sample video: " + result.stderr.decode("utf-8", "replace")[:200])
    return path


@pytest.fixture
def session(tmp_path, sample_video):
    store = FileStore(tmp_path / "store", ttl_seconds=3600, max_total_bytes=1_000_000_000)
    copy = tmp_path / "copy.mp4"
    shutil.copy(sample_video, copy)
    stored = store.add(
        copy,
        title="Sample Clip",
        source_url="https://example.com/v",
        platform="local",
        duration=6.0,
    )
    downloader = VideoDownloader(store)
    bundle = DownloadBundle(
        stored=stored,
        info={"title": "Sample Clip", "uploader": "tester", "extractor_key": "local"},
        quality="720p",
        audio_only=False,
        caption_paths=[],
    )
    return VideoAnalyzer(store, downloader), bundle


async def test_full_analysis_pipeline(session):
    analyzer, bundle = session
    payload = await analyzer._analyze(bundle)
    session_id = payload["session_id"]

    assert payload["status"] == "success"
    assert payload["shots"] >= 2
    assert payload["chapters_count"] >= 1
    assert payload["has_audio"] is True
    assert payload["resolution"] == "320x180"
    assert payload["contact_sheet_path"]
    assert Path(payload["contact_sheet_path"]).is_file()
    assert payload["timeline"]
    assert all("time" in chapter for chapter in payload["timeline"])

    timeline = analyzer.timeline(session_id)
    assert timeline["session_id"] == session_id
    assert timeline["chapters_count"] == payload["chapters_count"]

    chapter = await analyzer.chapter(session_id, 1)
    assert chapter["chapter"]["number"] == 1
    assert chapter["images"]
    assert all(Path(frame["path"]).is_file() for frame in chapter["images"])
    assert isinstance(chapter["shots"], list)

    frames = await analyzer.frames(session_id, timestamps=[0.5, 3.0], max_side=640)
    assert frames["count"] == 2
    assert all(Path(frame["path"]).is_file() for frame in frames["frames"])

    motion = await analyzer.motion(session_id, start=0, end=6, count=6)
    assert motion["count"] == 6
    assert motion["sheet"] and Path(motion["sheet"]).is_file()

    audio = await analyzer.audio(session_id)
    assert audio["has_audio"] is True
    assert audio["image_paths"]["waveform"] and Path(audio["image_paths"]["waveform"]).is_file()

    transcript = analyzer.transcript(session_id)
    assert transcript["status"] == "success"

    search = analyzer.search(session_id, "product")
    assert search["status"] == "success"

    listing = analyzer.list_sessions()
    assert listing["count"] == 1
    closed = analyzer.close(session_id)
    assert closed["closed"] is True
    assert analyzer.list_sessions()["count"] == 0


async def test_chapter_out_of_range(session):
    from app.analysis import AnalysisError

    analyzer, bundle = session
    payload = await analyzer._analyze(bundle)
    with pytest.raises(AnalysisError):
        await analyzer.chapter(payload["session_id"], 99)
    with pytest.raises(AnalysisError):
        analyzer.timeline("does-not-exist")


def test_ocr_degrades_without_binary(tmp_path, monkeypatch):
    image = tmp_path / "frame.png"
    from PIL import Image

    Image.new("RGB", (64, 32), (255, 255, 255)).save(image)
    monkeypatch.setattr(config, "TESSERACT_BIN", "")
    result = media.ocr_image(image)
    assert result["available"] is False
    assert result["text"] == ""
