# `shoosty-video-render` — task.md

You're building a RunPod-serverless Docker container that takes a song's audio + slides + (optional) lyric timing and renders an MP4 video. Same architecture as `shoosty1/ace-step:v57` (the existing ACE-Step container), minus the model weights — so the image is ~500MB instead of ~10GB.

This container will eventually be wired into `shoosty-studio` via a "Download video" button on each song page. For this task, you are ONLY building and deploying the container. The shoosty-studio integration is a separate task.

---

## Why this exists

Stephen (the user) has a 4-month silk-art show opening week of 2026-06-21 at the Jacksonville (JAX) airport gallery. Each silk piece will have a QR code next to it that scans to a public song page on `dadbotstudio.com`. He also wants to download MP4 videos of the songs (slides + audio synced) to post on Instagram, play on screens at the gallery, and email to people.

The browser-side video recording (Path A) is shipping first as a quick win. This RunPod container is Path B — a real, broadcast-clean MP4 renderer for when Path A's WebM output isn't good enough.

Background context:
- The "self-hosted compute fleet" pattern: every paid-vendor backend gets replaced with a 3-endpoint RunPod fleet over time. This container slots into that.
- ACE-Step pattern is mature; reuse the same handler shape, env var conventions, and `lib/runpod.ts` dispatcher integration.

---

## Contract: input → output

### Input (RunPod job payload, JSON)

```json
{
  "input": {
    "audio_url": "https://sgqlpcvbwgsjkllppfna.supabase.co/.../song.mp3",
    "slides": [
      {
        "image_url": "https://.../slide1.jpg",
        "start_sec": 0.0,
        "end_sec": 12.5
      },
      {
        "image_url": "https://.../slide2.jpg",
        "start_sec": 12.5,
        "end_sec": 28.0
      }
    ],
    "title": "The Silk Path",
    "artist": "Shoosty",
    "qr_code_url": "https://.../qr.png",
    "lyric_timing": null,
    "song_id": "uuid-of-the-song-row",
    "generation_id": "uuid-of-the-generation-row"
  }
}
```

Required fields: `audio_url`, `slides[]`. Everything else is optional. `lyric_timing` is reserved for later (karaoke captions burned into the video) — accept the field but you can no-op on it in v1.

### Output (handler return value, JSON)

```json
{
  "video_url": "https://sgqlpcvbwgsjkllppfna.supabase.co/.../song_id/song.mp4",
  "duration_sec": 187.5,
  "size_bytes": 9876543,
  "render_time_sec": 42.3
}
```

On failure, raise an exception with a clear message — RunPod surfaces it to the caller.

---

## Video spec

- **Format:** MP4 (H.264 video, AAC audio). Universal playback — iPhone Safari, airport AV systems, Instagram, everything.
- **Resolution:** 1920×1080 (16:9). Letterbox slides that don't match aspect ratio (black bars, not stretch).
- **Frame rate:** 30 fps.
- **Audio:** AAC 192 kbps, stereo, 44.1 kHz. Use the song's audio file directly — don't re-encode if you can avoid it (use `-c:a copy` if it's already AAC, otherwise re-encode).
- **Slide transitions:** 1-second crossfade between adjacent slides. The `start_sec` of slide N is when it's fully visible (mid-crossfade is centered on this time, so slide N-1 fades out from `start_sec - 0.5` to `start_sec + 0.5`).
- **End card:** if `qr_code_url` is present, append a 4-second end card with the QR code centered on a cream background (#F5F0E8), the song title above, and "made with Shoosty Studio" below. If `qr_code_url` is absent, no end card.
- **Total duration:** matches the audio duration. The last slide extends to the end of the audio. End card adds 4 seconds AFTER the audio.

---

## Container architecture

### File structure (your new repo)

```
shoosty-video-render/
├── Dockerfile
├── requirements.txt
├── handler.py
├── README.md
├── .dockerignore
├── .gitignore
└── test/
    ├── test_handler.py     # local-test harness
    └── fixtures/
        ├── sample-audio.mp3
        ├── slide1.jpg
        ├── slide2.jpg
        ├── slide3.jpg
        └── qr.png
```

### `Dockerfile`

```dockerfile
FROM python:3.12-slim

# ffmpeg is the workhorse. apt's version is recent enough.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY handler.py .

# RunPod's serverless runtime invokes handler.py's `handler(job)` function.
CMD ["python", "-u", "handler.py"]
```

### `requirements.txt`

```
runpod==1.6.2
requests==2.32.3
supabase==2.7.4
```

(Pin exact versions. Match the ACE handler's versions if the shoosty-studio repo's `Docker_Ace_Step/requirements.txt` has different pins — consistency wins.)

### `handler.py` skeleton

```python
import os
import json
import time
import tempfile
import subprocess
from pathlib import Path
import requests
import runpod
from supabase import create_client

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_SECRET_KEY"]
SUPABASE_BUCKET = os.environ.get("SUPABASE_VIDEO_BUCKET", "song-videos")

def download(url: str, dest: Path) -> None:
    """Download a URL to a local path. Raise on non-2xx."""
    r = requests.get(url, timeout=60, stream=True)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 14):
            f.write(chunk)

def build_filter_graph(slides: list, audio_duration: float, has_end_card: bool) -> str:
    """
    Build the ffmpeg filter graph for crossfaded slides synced to audio.
    Each slide gets a duration window; transitions are 1s crossfades.
    Returns the -filter_complex string.
    """
    # See the "ffmpeg recipe" section below for the exact filter pattern.
    # ... build dynamically based on len(slides)
    raise NotImplementedError

def render_video(input_data: dict, tmpdir: Path) -> Path:
    audio_path = tmpdir / "audio.mp3"
    download(input_data["audio_url"], audio_path)

    slide_paths = []
    for i, slide in enumerate(input_data["slides"]):
        p = tmpdir / f"slide_{i}.jpg"
        download(slide["image_url"], p)
        slide_paths.append(p)

    qr_path = None
    if input_data.get("qr_code_url"):
        qr_path = tmpdir / "qr.png"
        download(input_data["qr_code_url"], qr_path)

    # Probe audio duration
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(audio_path)],
        capture_output=True, text=True, check=True
    )
    audio_duration = float(probe.stdout.strip())

    # Build the ffmpeg command (see recipe below)
    output_path = tmpdir / "output.mp4"
    cmd = build_ffmpeg_command(
        slide_paths, input_data["slides"], audio_path, audio_duration,
        qr_path, input_data.get("title"), input_data.get("artist"),
        output_path
    )

    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg failed:\n{result.stderr[-2000:]}")
    render_time = time.time() - t0

    return output_path, audio_duration, render_time

def upload_to_supabase(local_path: Path, song_id: str) -> str:
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    storage_path = f"{song_id}/song.mp4"
    with open(local_path, "rb") as f:
        sb.storage.from_(SUPABASE_BUCKET).upload(
            storage_path, f, {"content-type": "video/mp4", "upsert": "true"}
        )
    public_url = sb.storage.from_(SUPABASE_BUCKET).get_public_url(storage_path)
    return public_url

def handler(job: dict) -> dict:
    input_data = job["input"]
    song_id = input_data.get("song_id", "untitled")

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        video_path, audio_duration, render_time = render_video(input_data, tmpdir)
        size_bytes = video_path.stat().st_size
        video_url = upload_to_supabase(video_path, song_id)

    return {
        "video_url": video_url,
        "duration_sec": audio_duration,
        "size_bytes": size_bytes,
        "render_time_sec": render_time,
    }

if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
```

---

## ffmpeg recipe — the actual filter graph

For N slides and audio of length T seconds, the command is roughly:

```bash
ffmpeg -y \
  -loop 1 -t <duration_1> -i slide_0.jpg \
  -loop 1 -t <duration_2> -i slide_1.jpg \
  -loop 1 -t <duration_3> -i slide_2.jpg \
  -i audio.mp3 \
  -filter_complex "
    [0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30[v0];
    [1:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30[v1];
    [2:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,setsar=1,fps=30[v2];
    [v0][v1]xfade=transition=fade:duration=1:offset=<offset_1>[vx1];
    [vx1][v2]xfade=transition=fade:duration=1:offset=<offset_2>[vout]
  " \
  -map "[vout]" -map 3:a \
  -c:v libx264 -preset medium -crf 22 -pix_fmt yuv420p \
  -c:a aac -b:a 192k \
  -shortest \
  output.mp4
```

Build this dynamically in `build_ffmpeg_command()`. The `<offset_N>` is cumulative slide durations minus 1s (the crossfade overlap).

For the end card, append it as a separate ffmpeg pass with `concat` after the main video is rendered — it's simpler than fitting it into the main filter graph.

**Test locally before pushing the container.** Set up a `test/test_handler.py` that builds a dummy job dict with fixture audio + slides + qr and calls `handler()` directly. It should produce a playable MP4 in the test directory.

---

## Environment variables (RunPod endpoint config)

Set these in the RunPod endpoint's "Environment Variables" section:

| Var | Value | Notes |
|-----|-------|-------|
| `SUPABASE_URL` | `https://sgqlpcvbwgsjkllppfna.supabase.co` | Same as ACE handler |
| `SUPABASE_SERVICE_ROLE_KEY` | (Stephen will paste) | NEVER commit. Service role key, not anon. |
| `SUPABASE_VIDEO_BUCKET` | `song-videos` | Bucket must exist in Supabase; create it as private bucket first |

**DO NOT** check in any `.env` file. The `.gitignore` should exclude `.env` and `.env.*`.

---

## Deploy steps

1. **Build the container locally** to validate it boots: `docker build -t shoosty-video-render:dev .`
2. **Run a smoke test** against the fixtures: `docker run --rm -v $(pwd)/test:/test shoosty-video-render:dev python test/test_handler.py`
3. **Push to Docker Hub:** tag as `shoosty1/video-render:v1` (matches the ACE convention `shoosty1/ace-step:v57`). `docker push shoosty1/video-render:v1`.
4. **Create a RunPod Serverless endpoint:**
   - Container image: `shoosty1/video-render:v1`
   - Container disk: 5 GB
   - GPU: **none — CPU only**. Choose a "CPU 2/4" or similar instance type.
   - Min Workers: 0 (idle = free)
   - Max Workers: 1 (scale up later)
   - Idle Timeout: 30s
   - Execution Timeout: 600s (10 min — generous for long songs)
5. **Test against the live endpoint** via the RunPod UI's "Send Request" form using the JSON contract above.

---

## Acceptance criteria

When this is done:
- Pushing the JSON payload above to the RunPod endpoint returns within ~2× audio duration with a `video_url`.
- That URL points to a playable MP4 at the specified resolution and bitrate.
- Audio is in sync with the slides (no drift over a 3-min song).
- The end card (when QR is provided) appears cleanly after the audio finishes.
- A song with 1 slide works (no crossfades needed).
- A song with 10+ slides works without ffmpeg complaining about filter graph complexity.
- Render time on a 3-min song with 8 slides is under 90 seconds on a CPU 2/4 instance.

When the container is live and reachable, Stephen will paste the endpoint URL into `shoosty-studio`'s env vars (`RUNPOD_VIDEO_ENDPOINT_URLS`) and integration in the app is a separate task.

---

## Reference: ACE container for patterns

The existing `Docker_Ace_Step/` folder in the `shoosty-studio` repo has the working pattern for:
- RunPod handler shape (`handler.py` structure)
- Supabase upload (`song-uploads` bucket; use `song-videos` for this project)
- Env var conventions
- Service role vs secret key handling

When in doubt, mirror that pattern. Don't reinvent shapes that already work.

---

## What this is NOT (out of scope for this task)

- Karaoke caption burn-in (slated for v2, when `lyric_timing` is provided)
- Hardware NVENC encoding (CPU is fine for the show)
- Multi-endpoint dispatcher integration (that's a shoosty-studio change, separate task)
- The "Download video" button in the UI (separate task in shoosty-studio)
- Background-job queueing (RunPod serverless handles this natively)

---

## Done = pushed image + live endpoint + sample MP4 URL Stephen can play

End deliverable to Stephen:
1. The Docker Hub image reference (e.g., `shoosty1/video-render:v1`)
2. The RunPod endpoint ID/URL
3. One sample MP4 URL produced by the endpoint, playing back cleanly on his phone

He'll handle the shoosty-studio integration after that.
