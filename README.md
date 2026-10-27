# Video Downloader MCP

A public, no-auth MCP server that lets ChatGPT download videos and audio from
YouTube, TikTok, Instagram, Facebook, X/Twitter, Reddit, Vimeo, Twitch clips,
Dailymotion, and many other sites. Powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp)
and ffmpeg, packaged for Render's free Docker tier.

> ChatGPT cannot receive a binary file through an MCP tool. The tools download the
> media to the server and return a **temporary direct link**. The user clicks the
> link to save the file. Links expire automatically (60 minutes by default).

## Tools

| Tool | What it does |
| --- | --- |
| `download_video(url, quality="720p")` | Downloads a video, returns a temporary MP4 link plus metadata. |
| `download_audio(url)` | Extracts MP3 audio at 192 kbps, returns a temporary link. |
| `get_video_info(url)` | Returns title, platform, uploader, duration, thumbnail, and available qualities without downloading. |

## Add it to ChatGPT

1. Open **ChatGPT → Settings → Connectors → Advanced → Developer mode**.
2. Click **Create** / **Add custom connector**.
3. Name it `Video Downloader`.
4. MCP server URL: `https://<your-service>.onrender.com/mcp`
5. Authentication: **No authentication**.
6. In a chat, ask: *"Download this video: https://..."* and open the link ChatGPT returns.

Custom connectors require a ChatGPT plan that supports them. The server must be
reachable over public HTTPS, which Render provides.

## Deploy on Render (free tier)

The repo includes a `Dockerfile` and `render.yaml`.

### Blueprint

1. Push this repo to GitHub.
2. In Render, choose **New → Blueprint** and select the repo.
3. Apply the blueprint. Render builds the Docker image and deploys it.
4. Copy the service URL and use `https://<service>.onrender.com/mcp` in ChatGPT.

### Manual Docker web service

- Environment: **Docker**
- Plan: **Free**
- Dockerfile path: `./Dockerfile`
- Health check path: `/health`
- No disk, no database, no auth needed.

Render sets `PORT` and `RENDER_EXTERNAL_URL` automatically. The app binds to
`0.0.0.0:$PORT`.

Free instances spin down after inactivity, so the first request after a pause can
take 30–60 seconds while the container starts. Temporary files live on the
instance's ephemeral disk, so download links stop working if the service restarts
or spins down before the link is used. Download and open links promptly.

## Configuration

All settings are optional environment variables.

| Variable | Default | Description |
| --- | --- | --- |
| `MAX_FILE_MB` | `150` | Maximum size of one downloaded file. |
| `MAX_STORAGE_MB` | `400` | Total temporary storage cap. |
| `FILE_TTL_MINUTES` | `60` | How long download links stay valid. |
| `MAX_CONCURRENT_DOWNLOADS` | `2` | Parallel download jobs. |
| `MAX_DOWNLOADS_PER_HOUR` | `40` | Global rate limit (protects the free instance). |
| `DOWNLOAD_TIMEOUT_SECONDS` | `180` | Hard timeout per download. |
| `PUBLIC_BASE_URL` | auto | Overrides the public URL used in download links. |
| `YTDLP_IMPERSONATE` | `chrome` | Browser TLS impersonation target. Set empty to disable. |
| `YTDLP_PROXY` | unset | Optional outbound proxy (`http://...` or `socks5://...`). |
| `YTDLP_COOKIES` | unset | Netscape-format cookies.txt contents, for login-only sites. |
| `COOKIES_FILE` | unset | Path to a cookies.txt file instead of `YTDLP_COOKIES`. |
| `YTDLP_EXTRA_ARGS` | unset | Extra yt-dlp CLI flags, e.g. `--extractor-args youtube:player_client=android`. |

### YouTube / Instagram limitations

YouTube aggressively blocks datacenter IPs. On a Render IP you may see
*"Sign in to confirm you're not a bot"*. Options:

- Supply cookies from an account via `YTDLP_COOKIES` (Netscape cookies.txt format).
- Route downloads through a residential/mobile proxy via `YTDLP_PROXY`.
- Try again later; YouTube's blocking is inconsistent.

Instagram and Vimeo sometimes require a logged-in session for public posts. Cookies
fix those cases too. TikTok works well with the default browser impersonation.

Live streams are rejected. Private, paid, DRM-protected, and login-only content
cannot be downloaded.

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
# ffmpeg is required for merging video+audio and MP3 extraction
.venv/bin/python -m app.main
```

Open `http://127.0.0.1:8000/` for the landing page and
`http://127.0.0.1:8000/health` for health. The MCP endpoint is
`http://127.0.0.1:8000/mcp`.

Run the tests (the end-to-end test generates a local video and downloads it through
the MCP protocol):

```bash
.venv/bin/python -m pytest
```

## Security notes

This server is intentionally **public and unauthenticated**, as requested. Anyone
with the URL can download media through it. It includes:

- SSRF protection: only `http/https`, hostnames are resolved, and private,
  loopback, link-local, and reserved addresses are rejected.
- A global hourly rate limit, a concurrency limit, file-size caps, and storage caps.
- Unguessable capability URLs for temporary files.
- No shell execution of user input; yt-dlp runs as an argument list.

For a private deployment, put the service behind a secret path or an access token
at the proxy/CDN layer. Never enable `ALLOW_PRIVATE_URLS` on a public server.

## Updating yt-dlp

Sites change constantly. Update the pinned `yt-dlp` version in `requirements.txt`
and redeploy regularly:

```bash
.venv/bin/pip install -U yt-dlp
.venv/bin/pip freeze | grep yt-dlp
```

## License

MIT. This tool is for downloading content you have the right to access and use.
Respect each platform's terms of service and applicable copyright law.
