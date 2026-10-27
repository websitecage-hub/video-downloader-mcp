from __future__ import annotations

import asyncio
import contextlib
import json
import math
import os
import re
import signal
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, ImageStat

from . import config


class MediaError(RuntimeError):
    pass


@dataclass
class MediaInfo:
    duration: float
    width: int
    height: int
    fps: float
    has_audio: bool
    has_video: bool
    format_name: str
    size: int
    rotation: int = 0


_FONT_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
)


def _font(size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    for candidate in _FONT_CANDIDATES:
        if Path(candidate).is_file():
            try:
                return ImageFont.truetype(candidate, size)
            except OSError:
                pass
    return ImageFont.load_default()


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "replace")


async def run(cmd: list[str], timeout: float) -> tuple[int, str, str]:
    """Run a subprocess with a hard timeout and a clean process-group kill."""
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await proc.communicate()
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        with contextlib.suppress(Exception):
            await proc.wait()
        raise MediaError(f"Media operation timed out after {int(timeout)} seconds.") from None
    return proc.returncode or 0, _decode(stdout), _decode(stderr)


def _rate(value: Any) -> float:
    if not value:
        return 0.0
    text = str(value)
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            n, d = float(num), float(den)
            return n / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def _rotation(stream: dict[str, Any]) -> int:
    tags = stream.get("tags") or {}
    for key in ("rotate", "Rotate"):
        try:
            return int(float(tags.get(key)))
        except (TypeError, ValueError):
            pass
    for side in stream.get("side_data_list") or []:
        try:
            return int(float(side.get("rotation")))
        except (TypeError, ValueError):
            pass
    return 0


def probe(path: Path) -> MediaInfo:
    cmd = [
        config.FFPROBE_BIN,
        "-v",
        "error",
        "-show_entries",
        "format=duration,size,format_name",
        "-show_entries",
        "stream=index,codec_type,codec_name,width,height,avg_frame_rate,r_frame_rate,duration,channels,sample_rate:stream_tags=rotate:stream_side_data=rotation",
        "-of",
        "json",
        str(path),
    ]
    returncode, stdout, stderr = asyncio.run(run(cmd, config.PROBE_TIMEOUT_SECONDS))
    if returncode != 0:
        raise MediaError(f"ffprobe could not read the video: {stderr.strip()[:300]}")
    try:
        data = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise MediaError("ffprobe returned invalid metadata.") from exc

    streams = data.get("streams") or []
    video = next(
        (
            stream
            for stream in streams
            if stream.get("codec_type") == "video"
            and not (stream.get("disposition") or {}).get("attached_pic")
        ),
        None,
    )
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    fmt = data.get("format") or {}

    duration = 0.0
    for candidate in (fmt.get("duration"), (video or {}).get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    width = int((video or {}).get("width") or 0)
    height = int((video or {}).get("height") or 0)
    rotation = _rotation(video or {})
    if rotation % 180 == 90:
        width, height = height, width

    fps = _rate((video or {}).get("avg_frame_rate")) or _rate((video or {}).get("r_frame_rate"))
    size = int(fmt.get("size") or 0)
    if not size:
        with contextlib.suppress(OSError):
            size = path.stat().st_size

    return MediaInfo(
        duration=max(0.0, duration),
        width=max(0, width),
        height=max(0, height),
        fps=max(0.0, fps),
        has_audio=audio is not None,
        has_video=video is not None,
        format_name=str(fmt.get("format_name") or ""),
        size=size,
        rotation=rotation,
    )


def keyframe_times(path: Path, duration: float) -> list[float]:
    cmd = [
        config.FFPROBE_BIN,
        "-v",
        "error",
        "-select_streams",
        "v:0",
        "-show_packets",
        "-show_entries",
        "packet=pts_time,dts_time,flags",
        "-of",
        "json",
        str(path),
    ]
    returncode, stdout, _stderr = asyncio.run(run(cmd, config.PROBE_TIMEOUT_SECONDS))
    if returncode != 0:
        return []
    try:
        packets = (json.loads(stdout) or {}).get("packets") or []
    except json.JSONDecodeError:
        return []

    times: list[float] = []
    for packet in packets:
        if "K" not in str(packet.get("flags") or ""):
            continue
        raw = packet.get("pts_time") or packet.get("dts_time")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if 0 <= value < max(duration, 0.01):
            times.append(value)

    times.sort()
    deduped: list[float] = []
    for value in times:
        if not deduped or value - deduped[-1] >= 0.25:
            deduped.append(value)
    if deduped and deduped[0] > 0.5:
        deduped.insert(0, 0.0)
    return deduped


async def extract_frame(
    video: Path,
    timestamp: float,
    out: Path,
    *,
    max_side: int = 1280,
    accurate: bool = True,
    quality: int = 3,
) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [config.FFMPEG_BIN, "-hide_banner", "-loglevel", "error", "-y", "-threads", "1"]
    cmd += ["-ss", f"{max(0.0, timestamp):.3f}"]
    if not accurate:
        cmd += ["-noaccurate_seek"]
    cmd += [
        "-i",
        str(video),
        "-frames:v",
        "1",
        "-an",
        "-sn",
        "-vf",
        f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease",
        "-q:v",
        str(quality),
        str(out),
    ]
    returncode, _stdout, stderr = await run(cmd, config.FRAME_TIMEOUT_SECONDS)
    if returncode != 0 or not out.is_file() or out.stat().st_size == 0:
        # The requested time can land just past the final frame. Step back once.
        retry_at = max(0.0, timestamp - 0.35)
        if retry_at < timestamp:
            cmd[cmd.index("-ss") + 1] = f"{retry_at:.3f}"
            returncode, _stdout, retry_err = await run(cmd, config.FRAME_TIMEOUT_SECONDS)
            if returncode == 0 and out.is_file() and out.stat().st_size > 0:
                return out
            stderr = retry_err or stderr
        raise MediaError(f"Could not extract a frame at {timestamp:.1f}s: {stderr.strip()[:200]}")
    return out


async def extract_keyframe_thumbs(
    video: Path,
    out_dir: Path,
    *,
    step: int = 1,
    max_side: int = 480,
    max_seconds: int = 1800,
) -> list[tuple[float, Path]]:
    """Decode only keyframes and write every Nth one in a single ffmpeg pass.

    This is dramatically cheaper than decoding every frame on a 0.1-CPU box.
    Timestamps come from ffmpeg's ``showinfo`` log so the caller gets an exact
    shot timeline without a second probe.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    for stale in out_dir.glob("kf-*.jpg"):
        with contextlib.suppress(OSError):
            stale.unlink()

    step = max(1, int(step))
    pattern = out_dir / "kf-%05d.jpg"
    filter_graph = (
        f"select=not(mod(n\\,{step})),"
        f"scale={max_side}:{max_side}:force_original_aspect_ratio=decrease,"
        "showinfo"
    )
    cmd = [
        config.FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "info",
        "-threads",
        "1",
        "-skip_frame",
        "nokey",
        "-i",
        str(video),
        "-t",
        str(max(1, int(max_seconds))),
        "-an",
        "-sn",
        "-vsync",
        "0",
        "-q:v",
        "4",
        "-vf",
        filter_graph,
        str(pattern),
    ]
    returncode, _stdout, stderr = await run(cmd, config.KEYFRAME_TIMEOUT_SECONDS)
    if returncode != 0:
        return []

    files = sorted(out_dir.glob("kf-*.jpg"))
    times: list[float] = []
    for line in stderr.splitlines():
        if "Parsed_showinfo" not in line:
            continue
        match = re.search(r"pts_time:([0-9.]+)", line)
        if match:
            with contextlib.suppress(ValueError):
                times.append(float(match.group(1)))

    count = min(len(files), len(times))
    frames: list[tuple[float, Path]] = []
    for index in range(count):
        path = files[index]
        if path.is_file() and path.stat().st_size > 0:
            frames.append((max(0.0, times[index]), path))
    # Remove files we did not pair with a timestamp so a failed run leaves no junk.
    for path in files[count:]:
        with contextlib.suppress(OSError):
            path.unlink()
    return frames


async def extract_audio(
    video: Path,
    out: Path,
    *,
    sample_rate: int = 8000,
    max_seconds: int | None = None,
) -> bool:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-threads",
        "1",
        "-i",
        str(video),
        "-vn",
        "-sn",
        "-ac",
        "1",
        "-ar",
        str(sample_rate),
        "-c:a",
        "pcm_s16le",
    ]
    if max_seconds:
        cmd += ["-t", str(max(1, int(max_seconds)))]
    cmd.append(str(out))
    returncode, _stdout, _stderr = await run(cmd, config.AUDIO_TIMEOUT_SECONDS)
    return returncode == 0 and out.is_file() and out.stat().st_size > 100


def _parse_silences(stderr: str) -> list[tuple[float, float]]:
    starts: list[float] = []
    silences: list[tuple[float, float]] = []
    for line in stderr.splitlines():
        match = re.search(r"silence_start:\s*([0-9.]+)", line)
        if match:
            starts.append(float(match.group(1)))
            continue
        match = re.search(r"silence_end:\s*([0-9.]+)", line)
        if match and starts:
            start = starts.pop(0)
            end = float(match.group(1))
            if end > start:
                silences.append((start, end))
    return silences


async def audio_timeline(audio: Path, duration: float) -> dict[str, Any]:
    silence_cmd = [
        config.FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "info",
        "-threads",
        "1",
        "-i",
        str(audio),
        "-af",
        "silencedetect=noise=-35dB:d=0.6",
        "-f",
        "null",
        "-",
    ]
    _rc, _stdout, stderr = await run(silence_cmd, config.AUDIO_TIMEOUT_SECONDS)
    silences = _parse_silences(stderr)

    volume_cmd = [
        config.FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "info",
        "-threads",
        "1",
        "-i",
        str(audio),
        "-af",
        "volumedetect",
        "-f",
        "null",
        "-",
    ]
    _rc2, _stdout2, volume_stderr = await run(volume_cmd, config.AUDIO_TIMEOUT_SECONDS)
    mean_db = None
    max_db = None
    for line in volume_stderr.splitlines():
        match = re.search(r"mean_volume:\s*(-?[0-9.]+)\s*dB", line)
        if match:
            mean_db = float(match.group(1))
        match = re.search(r"max_volume:\s*(-?[0-9.]+)\s*dB", line)
        if match:
            max_db = float(match.group(1))

    events: list[dict[str, Any]] = []
    cursor = 0.0
    for start, end in silences:
        if start > cursor + 0.2:
            events.append({"start": round(cursor, 2), "end": round(start, 2), "kind": "audio"})
        events.append({"start": round(start, 2), "end": round(end, 2), "kind": "silence"})
        cursor = max(cursor, end)
    if cursor < duration - 0.2:
        events.append({"start": round(cursor, 2), "end": round(duration, 2), "kind": "audio"})
    if not events and duration > 0:
        events.append({"start": 0.0, "end": round(duration, 2), "kind": "audio"})

    silence_seconds = sum(max(0.0, end - start) for start, end in silences)
    return {
        "has_audio": True,
        "mean_volume_db": mean_db,
        "max_volume_db": max_db,
        "silence_ratio": round(min(1.0, silence_seconds / duration), 3) if duration else 0.0,
        "events": events[:200],
    }


def image_features(path: Path) -> dict[str, Any]:
    try:
        with Image.open(path) as source:
            image = source.convert("RGB")
            image = ImageOps.exif_transpose(image)
            small = image.resize((64, 64), Image.Resampling.BILINEAR)
            gray = small.convert("L")
            stat = ImageStat.Stat(gray)
            brightness = (stat.mean[0] if stat.mean else 0.0) / 255.0
            contrast = (stat.stddev[0] if stat.stddev else 0.0) / 128.0
            edges = gray.filter(ImageFilter.FIND_EDGES)
            edge_stat = ImageStat.Stat(edges)
            edge_mean = (edge_stat.mean[0] if edge_stat.mean else 0.0) / 255.0
            # 4x4x4 RGB histogram, normalized.
            bins = [0] * 64
            try:
                pixels = list(small.get_flattened_data())
            except AttributeError:
                pixels = list(small.getdata())
            for r, g, b in pixels:
                bins[(r // 64) * 16 + (g // 64) * 4 + (b // 64)] += 1
            total = max(1, len(pixels))
            bins = [round(value / total, 4) for value in bins]
            palette = small.quantize(colors=5).getpalette() or []
            colors = []
            for index in range(0, min(len(palette), 15), 3):
                colors.append(f"#{palette[index]:02x}{palette[index + 1]:02x}{palette[index + 2]:02x}")
            return {
                "brightness": round(brightness, 3),
                "contrast": round(contrast, 3),
                "text_score": round(edge_mean, 3),
                "histogram": bins,
                "colors": colors[:4],
            }
    except Exception:
        return {"brightness": 0.5, "contrast": 0.0, "text_score": 0.0, "histogram": [0.0] * 64, "colors": []}


def motion_score(path_a: Path, path_b: Path) -> float:
    try:
        with Image.open(path_a) as first, Image.open(path_b) as second:
            a = ImageOps.exif_transpose(first).convert("L").resize((64, 64))
            b = ImageOps.exif_transpose(second).convert("L").resize((64, 64))
            diff = ImageChops.difference(a, b)
            mean = ImageStat.Stat(diff).mean[0] if ImageStat.Stat(diff).mean else 0.0
            return round(min(1.0, mean / 255.0), 3)
    except Exception:
        return 0.0


def histogram_distance(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    return round(min(1.0, sum(abs(x - y) for x, y in zip(a, b)) / 2.0), 3)


def _fit(image: Image.Image, width: int, height: int) -> Image.Image:
    return ImageOps.contain(image.convert("RGB"), (max(1, width), max(1, height)), Image.Resampling.LANCZOS)


def compose_sheet(
    frames: list[dict[str, Any]],
    out: Path,
    *,
    duration: float,
    cols: int = 2,
    rows: int = 2,
    width: int = 1600,
    height: int = 900,
    header: str | None = None,
) -> Path:
    """Compose timestamped frames into a labelled contact sheet."""
    out.parent.mkdir(parents=True, exist_ok=True)
    canvas = Image.new("RGB", (width, height), (13, 18, 26))
    draw = ImageDraw.Draw(canvas)

    margin = 16
    gap = 12
    label_h = 34
    bar_h = 22
    header_h = 34 if header else 0
    usable_w = width - 2 * margin - (cols - 1) * gap
    usable_h = height - 2 * margin - header_h - (rows - 1) * gap - bar_h
    tile_w = max(1, usable_w // cols)
    tile_h = max(1, usable_h // rows)

    if header:
        draw.text((margin, 8), header[:120], font=_font(22), fill=(232, 238, 248))

    for slot in range(cols * rows):
        col = slot % cols
        row = slot // cols
        x = margin + col * (tile_w + gap)
        y = margin + header_h + row * (tile_h + gap)
        draw.rounded_rectangle(
            (x, y, x + tile_w, y + tile_h),
            radius=8,
            fill=(20, 27, 39),
            outline=(36, 48, 68),
            width=1,
        )
        if slot >= len(frames):
            continue
        frame = frames[slot]
        path = Path(frame["path"])
        try:
            with Image.open(path) as source:
                image = _fit(source, tile_w - 8, tile_h - label_h - 8)
        except Exception:
            continue
        image_w, image_h = image.size
        paste_x = x + (tile_w - image_w) // 2
        paste_y = y + 4 + (tile_h - label_h - 8 - image_h) // 2
        canvas.paste(image, (paste_x, paste_y))

        label_y = y + tile_h - label_h
        draw.rectangle((x, label_y, x + tile_w, y + tile_h), fill=(10, 15, 23))
        label = str(frame.get("label") or "")
        draw.text((x + 10, label_y + 6), label[:48], font=_font(20), fill=(94, 234, 212))

    bar_y = height - margin - bar_h + 6
    draw.rectangle((margin, bar_y, width - margin, bar_y + 3), fill=(36, 48, 68))
    if duration > 0 and frames:
        for frame in frames:
            try:
                t = float(frame.get("timestamp") or 0.0)
            except (TypeError, ValueError):
                continue
            marker_x = margin + int((width - 2 * margin) * min(1.0, max(0.0, t / duration)))
            draw.rectangle((marker_x - 2, bar_y - 5, marker_x + 2, bar_y + 8), fill=(94, 234, 212))

    canvas.save(out, "JPEG", quality=84, optimize=True)
    return out


def ocr_image(path: Path, *, lang: str | None = None, psm: int = 6) -> dict[str, Any]:
    """Run Tesseract if available. Returns text plus line boxes in source pixels."""
    tesseract = config.TESSERACT_BIN
    if not tesseract:
        return {"text": "", "boxes": [], "available": False}
    lang = lang or config.OCR_LANG
    tmp_path: Path | None = None
    scale = 1.0
    original_size = (1, 1)
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            original_size = image.size
            gray = ImageOps.autocontrast(image.convert("L"))
            if gray.width < 900:
                scale = min(3.0, 900 / max(1, gray.width))
                gray = gray.resize(
                    (max(1, int(gray.width * scale)), max(1, int(gray.height * scale))),
                    Image.Resampling.LANCZOS,
                )
            with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
                tmp_path = Path(handle.name)
            gray.save(tmp_path, "PNG")
        cmd = [
            tesseract,
            str(tmp_path),
            "stdout",
            "--oem",
            "1",
            "--psm",
            str(max(0, min(13, psm))),
            "-l",
            lang,
            "tsv",
        ]
        returncode, stdout, _stderr = asyncio.run(run(cmd, config.OCR_TIMEOUT_SECONDS))
        if returncode != 0:
            return {"text": "", "boxes": [], "available": True}
    except FileNotFoundError:
        return {"text": "", "boxes": [], "available": False}
    except Exception:
        return {"text": "", "boxes": [], "available": False}
    finally:
        if tmp_path is not None:
            with contextlib.suppress(OSError):
                tmp_path.unlink()

    lines: dict[tuple[str, str, str], dict[str, Any]] = {}
    for row in stdout.splitlines()[1:]:
        parts = row.split("\t")
        if len(parts) < 12 or parts[0] != "5":
            continue
        try:
            conf = float(parts[10])
        except ValueError:
            continue
        text = parts[11].strip()
        if conf < 35 or not text:
            continue
        key = (parts[2], parts[3], parts[4])
        left, top, width, height = (int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]))
        entry = lines.setdefault(key, {"words": [], "left": left, "top": top, "right": left + width, "bottom": top + height})
        entry["words"].append(text)
        entry["left"] = min(entry["left"], left)
        entry["top"] = min(entry["top"], top)
        entry["right"] = max(entry["right"], left + width)
        entry["bottom"] = max(entry["bottom"], top + height)

    ordered = sorted(lines.values(), key=lambda item: (item["top"], item["left"]))
    texts = [" ".join(item["words"]) for item in ordered]
    max_width, max_height = original_size
    boxes: list[list[int]] = []
    for item in ordered:
        left = max(0, min(max_width - 1, int(round(item["left"] / scale))))
        top = max(0, min(max_height - 1, int(round(item["top"] / scale))))
        right = max(left + 1, min(max_width, int(round(item["right"] / scale))))
        bottom = max(top + 1, min(max_height, int(round(item["bottom"] / scale))))
        boxes.append([left, top, right - left, bottom - top])
    text = "\n".join(line for line in texts if line.strip())
    return {"text": text[:4000], "boxes": boxes[:60], "available": True}


def make_text_crop(path: Path, boxes: list[list[int]], out: Path) -> Path | None:
    if not boxes:
        return None
    try:
        with Image.open(path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            left = min(box[0] for box in boxes)
            top = min(box[1] for box in boxes)
            right = max(box[0] + box[2] for box in boxes)
            bottom = max(box[1] + box[3] for box in boxes)
            pad = max(8, int(min(image.width, image.height) * 0.02))
            left = max(0, left - pad)
            top = max(0, top - pad)
            right = min(image.width, right + pad)
            bottom = min(image.height, bottom + pad)
            if right - left < 20 or bottom - top < 12:
                return None
            crop = image.crop((left, top, right, bottom))
            if crop.width < 700:
                scale = min(3.0, 700 / max(1, crop.width))
                crop = crop.resize(
                    (int(crop.width * scale), int(crop.height * scale)),
                    Image.Resampling.LANCZOS,
                )
            out.parent.mkdir(parents=True, exist_ok=True)
            crop.save(out, "JPEG", quality=90, optimize=True)
            return out
    except Exception:
        return None


async def waveform(audio: Path, out: Path) -> Path | None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-threads",
        "1",
        "-i",
        str(audio),
        "-filter_complex",
        "showwavespic=s=1400x320:colors=0x5eead4",
        "-frames:v",
        "1",
        str(out),
    ]
    returncode, _stdout, _stderr = await run(cmd, config.AUDIO_TIMEOUT_SECONDS)
    return out if returncode == 0 and out.is_file() else None


async def spectrogram(audio: Path, out: Path) -> Path | None:
    out.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        config.FFMPEG_BIN,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-threads",
        "1",
        "-i",
        str(audio),
        "-lavfi",
        "showspectrumpic=s=1400x600:legend=0:color=viridis",
        "-frames:v",
        "1",
        str(out),
    ]
    returncode, _stdout, _stderr = await run(cmd, config.AUDIO_TIMEOUT_SECONDS)
    return out if returncode == 0 and out.is_file() else None
