# Docker_Video_Exports

RunPod-serverless Docker container that renders a song's audio + slide deck into an MP4 video, uploads to Supabase Storage, and returns the public URL.

Same architecture as `Docker_Ace_Step` (Shoosty's ACE-Step container) but with no model weights — image is ~500 MB instead of 10 GB and builds in a couple of minutes.

Built for `shoosty-studio` / `dadbotstudio.com` — the JAX airport silk-art show (June 2026 → Oct 2026+) is the first real use case. See [`task.md`](./task.md) for the full spec and the why-this-exists.

## Quick start

```bash
# Build locally
docker build -t shoosty-video-render:dev .

# Smoke test against fixtures (drop sample-audio.mp3, slide{1,2,3}.jpg,
# and qr.png into test/fixtures/ first)
python test/test_handler.py
```

## Deploy

```bash
# Tag for Docker Hub
docker tag shoosty-video-render:dev shoosty1/video-render:v1
docker push shoosty1/video-render:v1
```

Then on RunPod: create a Serverless endpoint pointing at `shoosty1/video-render:v1`. **CPU-only — no GPU needed.** Container disk 5 GB; idle timeout 30 s; execution timeout 600 s; min workers 0; max workers 1 to start.

## Required environment variables (set on the RunPod endpoint)

| Var | Value | Notes |
|-----|-------|-------|
| `SUPABASE_URL` | `https://sgqlpcvbwgsjkllppfna.supabase.co` | Shoosty's Supabase project |
| `SUPABASE_SERVICE_ROLE_KEY` | (paste in RunPod UI) | NEVER commit |
| `SUPABASE_VIDEO_BUCKET` | `song-videos` | Bucket must exist in Supabase before first call |

## Input / output contract

See [`task.md`](./task.md) — section "Contract: input → output".

## Architecture

- Python 3.12 + ffmpeg 6.x (from apt)
- `handler.py` runs as a `runpod.serverless.start(...)` worker
- ffmpeg filter graph: scale + letterbox each slide to 1920×1080, chain xfades for crossfades, mux audio, output H.264/AAC MP4
- Optional 4 s end card with QR code + title + "made with Shoosty Studio"
- Uploads to Supabase Storage at `{song_id}/{generation_id or 'song'}.mp4`

## What's NOT in v1

- Karaoke caption burn-in (reserved for v2 when `lyric_timing` is wired)
- Hardware NVENC encoding (CPU is plenty for the show)
- Multi-endpoint dispatcher in `shoosty-studio` (separate task)
- The "Download video" button in the app (separate task)
