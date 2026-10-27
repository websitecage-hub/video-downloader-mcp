# Video Downloader & Analysis MCP

A public, no-auth MCP server that lets ChatGPT (or any MCP client) **download** public
videos and audio, and **understand** them. It builds a timestamped, text-first map of a
video — chapters, captions, on-screen OCR text, audio structure, motion — and returns real
frames when the model needs to *see* something. Powered by yt-dlp, ffmpeg, Tesseract, and
SQLite FTS5. No external paid APIs.

## Live deployment

- **MCP endpoint:** https://video-downloader-mcp.onrender.com/mcp
- **Health:** https://video-downloader-mcp.onrender.com/health
- **Technical brief:** https://video-mcp-brief.bramble-cannon.workers.dev

> ChatGPT cannot receive a binary file through an MCP tool. Download tools return a
> **temporary direct link** the user clicks. Analysis tools return a session id plus
> evidence; the inline player card (and the standalone `/watch/{session_id}` page) let a
> human watch the video with clickable chapters and transcript.

## Tools

### Analysis (the reason this exists)

| Tool | What it does |
| --- | --- |
| `open_video(url, quality="720p")` | Downloads the video, fetches captions, indexes keyframes into chapters, reads on-screen text, analyzes audio, and returns a **contact sheet** + timestamped chapter map + `session_id`. |
| `get_timeline(session_id)` | The complete textual map: every chapter with start/end, title, and summary. |
| `get_chapter(session_id, chapter)` | Deep evidence for one chapter: real frames, transcript text, OCR, per-shot visual stats and motion, audio events. |
| `search_video(session_id, query, kind, limit)` | Search captions, OCR, and chapter text; returns moments with timestamps. |
| `get_frames(session_id, timestamps?, chapter?, start?, end?, count?, ocr?)` | Extract exact frames and read their on-screen text. |
| `get_motion(session_id, start?, end?, count?)` | Per-frame pixel-change scores, peak moment, and a labelled filmstrip. |
| `get_audio(session_id, start?, end?)` | Speech-vs-silence timeline, waveform, and spectrogram. |
| `get_transcript(session_id, start?, end?, limit?, offset?)` | The full caption track with timestamps. |
| `list_sessions()` / `close_video(session_id)` | Manage active sessions. |

### Downloads

| Tool | What it does |
| --- | --- |
| `download_video(url, quality="720p")` | Downloads a video and returns a temporary MP4 link. |
| `download_audio(url)` | Extracts MP3 (192 kbps) and returns a temporary link. |
| `get_video_info(url)` | Metadata and available qualities without downloading. |

## How ChatGPT should use it

1. `open_video(url)` **once**. Read the returned chapter timeline and contact sheet.
2. `get_timeline(session_id)` for the full map (also included in the `open_video` result).
3. `get_chapter(session_id, n)` for the chapters that matter — this returns real frames plus
   the exact transcript and OCR for that window.
4. `search_video(...)`, `get_frames(...)`, `get_motion(...)`, `get_audio(...)` as needed.
5. Synthesise the answer and **cite timestamps**.

Reuse the `session_id`. Calling `open_video` again re-downloads and re-analyzes.

## Why it is fast on a tiny server

Render's free tier gives 0.1 CPU. The design works within that:

- **Keyframe-only decode.** A single `ffmpeg -skip_frame nokey` pass extracts up to 48
  thumbnails and their exact timestamps from the `showinfo` log, instead of decoding every
  frame.
- **Selective visual evidence.** Only 4 frames per chapter are decoded, on demand, with
  ffmpeg fast-seek.
- **Text-first.** Captions, OCR, and visual statistics carry most of the understanding, so
  images are used only where they add information.
- **Bounded work.** One analysis job and one media operation at a time, hard timeouts on
  every subprocess, and a 30-minute analysis cap by default.
- **Lazy OCR.** A few high-text frames are read during `open_video`; the rest on `get_chapter`.
- **Caching.** Thumbnails, detail frames, motion strips, waveform, and spectrogram are cached
  per session and expire with the video.

## Evidence layers

- Shot/keyframe index with timestamps and visual stats (brightness, contrast, edge/text score,
  palette, histogram).
- Captions/transcript (manual or auto, best language chosen from yt-dlp metadata).
- OCR text (Tesseract) with line boxes and a zoomed text crop.
- Audio timeline (speech/silence), waveform, spectrogram.
- Contact sheet, chapter storyboards, motion strips.

## Limits and privacy

- Max 150 MB per file; 1080p max; live streams unsupported.
- Analysis covers up to 30 minutes and up to 24 chapters (configurable).
- Files, sessions, and links expire after 60 minutes by default.
- Everything runs on the server; nothing is sent to a paid third party.
- Private, paid, or login-only content may fail. The server owner can supply cookies via the
  `YTDLP_COOKIES` environment variable.
- On Render's free tier the instance sleeps when idle, which also ends sessions early.

## Configuration

Environment variables (all optional):

| Variable | Default | Purpose |
| --- | --- | --- |
| `MAX_FILE_MB` | `150` | Download size cap. |
| `FILE_TTL_MINUTES` | `60` | Lifetime of files and sessions. |
| `MAX_DOWNLOADS_PER_HOUR` | `40` | Rate limit. |
| `ANALYSIS_MAX_SECONDS` | `1800` | Seconds of video analyzed. |
| `MAX_SHOTS` / `MAX_CHAPTERS` | `48` / `24` | Granularity caps. |
| `OCR_LANG` | `eng` | Tesseract language. |
| `FFMPEG_BIN` / `FFPROBE_BIN` / `TESSERACT_BIN` | auto | Binary paths. |
| `YTDLP_COOKIES` / `YTDLP_PROXY` / `YTDLP_IMPERSONATE` | – | Access to bot-protected sites. |
| `ALLOW_PRIVATE_URLS` | `false` | Local testing only. Never enable publicly. |

## Run locally

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt -r requirements-dev.txt
FFMPEG_BIN=/path/to/ffmpeg FFPROBE_BIN=/path/to/ffprobe .venv/bin/python -m app.main
# then open http://127.0.0.1:8000
```

Tesseract is optional; OCR degrades gracefully when the binary is missing.

## Tests

```bash
FFMPEG_BIN=/path/to/ffmpeg FFPROBE_BIN=/path/to/ffprobe .venv/bin/python -m pytest -q
```

The suite covers the downloader, storage, security, caption parsers, the FTS index, and a
full analysis pipeline on a generated sample video (skipped automatically if ffmpeg is
absent). The end-to-end test boots the real server and exercises the MCP tools.

## Deploy

The repo includes `render.yaml` for a free Render Docker web service. Push to `main` and
Render auto-deploys. The Docker image installs ffmpeg, Tesseract, and DejaVu fonts.
