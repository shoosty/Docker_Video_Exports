import os
import sys
import time
import tempfile
import subprocess
from pathlib import Path

import requests
import runpod
from supabase import create_client

# ─────────────────────────────────────────────
# Logging
# ─────────────────────────────────────────────

def log(msg: str, level: str = "INFO") -> None:
    ts = time.strftime("%Y-%m-%dT%H:%M:%S")
    print(f"[{ts}] [{level}] {msg}", flush=True)

def log_env() -> None:
    log("── Environment ──────────────────────────────")
    for key, val in sorted(os.environ.items()):
        if any(s in key.upper() for s in ("KEY", "SECRET", "TOKEN", "PASSWORD")):
            display = f"{val[:6]}…(redacted)" if val else "(empty)"
        else:
            display = val
        log(f"  {key}={display}")
    log("─────────────────────────────────────────────")

def run_cmd(cmd: list, label: str) -> subprocess.CompletedProcess:
    """Run a subprocess, log full command + all output, raise on failure."""
    log(f"▶ {label}")
    log(f"  CMD: {' '.join(str(c) for c in cmd)}")
    t0 = time.time()
    result = subprocess.run(cmd, capture_output=True, text=True)
    elapsed = time.time() - t0
    log(f"  exit={result.returncode}  time={elapsed:.1f}s")
    if result.stdout.strip():
        log(f"  STDOUT:\n{result.stdout.strip()}")
    if result.stderr.strip():
        log(f"  STDERR (tail 4000):\n{result.stderr[-4000:]}")
    if result.returncode != 0:
        raise RuntimeError(
            f"[{label}] failed (exit {result.returncode}):\n{result.stderr[-4000:]}"
        )
    log(f"✓ {label} ({elapsed:.1f}s)")
    return result

# ─────────────────────────────────────────────
# Startup checks
# ─────────────────────────────────────────────

log("━━━ shoosty-video-render starting ━━━")
log_env()

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or os.environ["SUPABASE_SECRET_KEY"]
)
SUPABASE_BUCKET = os.environ.get("SUPABASE_VIDEO_BUCKET", "song-videos")

log(f"SUPABASE_URL    = {SUPABASE_URL}")
log(f"SUPABASE_BUCKET = {SUPABASE_BUCKET}")

# Verify ffmpeg/ffprobe present and log versions
try:
    r = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, check=True)
    log(f"ffmpeg: {r.stdout.splitlines()[0]}")
    r2 = subprocess.run(["ffprobe", "-version"], capture_output=True, text=True, check=True)
    log(f"ffprobe: {r2.stdout.splitlines()[0]}")
except Exception as e:
    log(f"FATAL — ffmpeg/ffprobe not found: {e}", "ERROR")
    sys.exit(1)

log("━━━ startup OK ━━━")

# ─────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────

def download(url: str, dest: Path) -> None:
    log(f"↓ {url} → {dest.name}")
    t0 = time.time()
    r = requests.get(url, timeout=120, stream=True)
    log(f"  HTTP {r.status_code}  content-type={r.headers.get('content-type')}  content-length={r.headers.get('content-length', '?')}")
    r.raise_for_status()
    total = 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 14):
            f.write(chunk)
            total += len(chunk)
    log(f"  ✓ {total:,} bytes in {time.time()-t0:.1f}s → {dest}")


def probe_duration(path: Path) -> float:
    log(f"ffprobe duration: {path.name}")
    result = run_cmd(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        "ffprobe duration",
    )
    duration = float(result.stdout.strip())
    log(f"  audio duration = {duration:.3f}s")
    return duration


# ─────────────────────────────────────────────
# ffmpeg filter graph builder
# ─────────────────────────────────────────────

SCALE_PAD = (
    "scale=1920:1080:force_original_aspect_ratio=decrease,"
    "pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
    "setsar=1,fps=30"
)


def build_ffmpeg_command(
    slide_paths: list,
    slides_meta: list,
    audio_path: Path,
    audio_duration: float,
    output_path: Path,
) -> list:
    n = len(slides_meta)
    log(f"building ffmpeg command: {n} slide(s), audio={audio_duration:.2f}s")

    windows = []
    for i, s in enumerate(slides_meta):
        start = s["start_sec"]
        end = s["end_sec"] if i < n - 1 else audio_duration
        w = end - start
        windows.append(w)
        log(f"  slide {i}: start={start}s  end={end:.3f}s  window={w:.3f}s")

    loop_durations = []
    for i, w in enumerate(windows):
        extra = 0.0
        if i > 0:
            extra += 0.5
        if i < n - 1:
            extra += 0.5
        loop_durations.append(w + extra)
        log(f"  slide {i}: loop_duration={w + extra:.3f}s (window={w:.3f} + overlap={extra:.1f})")

    cmd = ["ffmpeg", "-y", "-loglevel", "verbose"]
    for i, path in enumerate(slide_paths):
        cmd += ["-loop", "1", "-t", f"{loop_durations[i]:.3f}", "-i", str(path)]
    cmd += ["-i", str(audio_path)]

    audio_input_idx = n

    if n == 1:
        filter_complex = f"[0:v]{SCALE_PAD}[vout]"
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", f"{audio_input_idx}:a",
        ]
    else:
        filter_lines = []
        for i in range(n):
            filter_lines.append(f"[{i}:v]{SCALE_PAD}[v{i}]")

        prev_label = "v0"
        cumulative = 0.0
        for i in range(1, n):
            cumulative += windows[i - 1]
            offset = cumulative - 0.5
            next_label = f"vx{i}" if i < n - 1 else "vout"
            xfade = f"[{prev_label}][v{i}]xfade=transition=fade:duration=1:offset={offset:.3f}[{next_label}]"
            filter_lines.append(xfade)
            log(f"  xfade {i}: offset={offset:.3f}s → [{next_label}]")
            prev_label = next_label

        filter_complex = ";\n".join(filter_lines)
        cmd += [
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-map", f"{audio_input_idx}:a",
        ]

    cmd += [
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(output_path),
    ]

    log(f"filter_complex:\n{filter_complex}")
    return cmd


# ─────────────────────────────────────────────
# End card (second ffmpeg pass)
# ─────────────────────────────────────────────

def build_end_card(qr_path: Path, title: str | None, tmpdir: Path) -> Path:
    log(f"building end card: title={title!r}  qr={qr_path}")
    end_card_path = tmpdir / "end_card.mp4"
    title_text = title or ""

    drawtext_filters = []
    if title_text:
        safe_title = title_text.replace("'", "\\'")
        drawtext_filters.append(
            f"drawtext=text='{safe_title}':fontsize=60:fontcolor=0x3D3226:"
            "x=(w-text_w)/2:y=(h/2-300):"
            "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        )

    drawtext_filters.append(
        "drawtext=text='made with Shoosty Studio':fontsize=36:fontcolor=0x3D3226:"
        "x=(w-text_w)/2:y=(h/2+250):"
        "fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
    )

    filter_complex = (
        "color=c=#F5F0E8:size=1920x1080:rate=30[bg];"
        "[1:v]scale=400:400[qr];"
        "[bg][qr]overlay=(W-w)/2:(H-h)/2[base];"
        f"[base]{','.join(drawtext_filters)}[vout]"
    )

    log(f"end card filter_complex:\n{filter_complex}")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "verbose",
        "-f", "lavfi", "-i", "color=c=#F5F0E8:size=1920x1080:rate=30:duration=4",
        "-i", str(qr_path),
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-t", "4",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        str(end_card_path),
    ]
    run_cmd(cmd, "end card render")
    log(f"end card: {end_card_path.stat().st_size:,} bytes")
    return end_card_path


def concat_videos(main_path: Path, end_card_path: Path, output_path: Path) -> None:
    log("concat: adding silence to end card")
    silent_end_card = output_path.parent / "end_card_audio.mp4"

    run_cmd([
        "ffmpeg", "-y", "-loglevel", "verbose",
        "-i", str(end_card_path),
        "-f", "lavfi", "-i", "aevalsrc=0:c=stereo:s=44100:d=4",
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k",
        "-shortest",
        str(silent_end_card),
    ], "silence pad end card")

    concat_list = output_path.parent / "concat.txt"
    concat_list.write_text(f"file '{main_path}'\nfile '{silent_end_card}'\n")
    log(f"concat list:\n{concat_list.read_text()}")

    run_cmd([
        "ffmpeg", "-y", "-loglevel", "verbose",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(output_path),
    ], "concat main + end card")

    log(f"final output: {output_path.stat().st_size:,} bytes")


# ─────────────────────────────────────────────
# Core render pipeline
# ─────────────────────────────────────────────

def render_video(input_data: dict, tmpdir: Path) -> tuple:
    log(f"━━━ render_video: song_id={input_data.get('song_id')} ━━━")
    log(f"  slides: {len(input_data['slides'])}")
    log(f"  title:  {input_data.get('title')}")
    log(f"  qr:     {bool(input_data.get('qr_code_url'))}")
    log(f"  tmpdir: {tmpdir}")

    # 1. Download audio
    audio_path = tmpdir / "audio.mp3"
    download(input_data["audio_url"], audio_path)
    log(f"  audio on disk: {audio_path.stat().st_size:,} bytes")

    # 2. Download slides
    slide_paths = []
    for i, slide in enumerate(input_data["slides"]):
        p = tmpdir / f"slide_{i}.jpg"
        download(slide["image_url"], p)
        log(f"  slide {i} on disk: {p.stat().st_size:,} bytes")
        slide_paths.append(p)

    # 3. Download QR if present
    qr_path = None
    if input_data.get("qr_code_url"):
        qr_path = tmpdir / "qr.png"
        download(input_data["qr_code_url"], qr_path)
        log(f"  qr on disk: {qr_path.stat().st_size:,} bytes")

    # 4. Probe audio duration
    audio_duration = probe_duration(audio_path)

    # 5. Main render
    main_path = tmpdir / "main.mp4"
    cmd = build_ffmpeg_command(
        slide_paths, input_data["slides"], audio_path, audio_duration, main_path
    )
    t0 = time.time()
    run_cmd(cmd, "main render")
    log(f"  main.mp4: {main_path.stat().st_size:,} bytes")

    # 6. End card
    if qr_path:
        log("appending end card…")
        end_card_path = build_end_card(qr_path, input_data.get("title"), tmpdir)
        final_path = tmpdir / "output.mp4"
        concat_videos(main_path, end_card_path, final_path)
    else:
        log("no QR — skipping end card")
        final_path = main_path

    render_time = time.time() - t0
    log(f"━━━ render complete: {final_path.stat().st_size:,} bytes  {render_time:.1f}s ━━━")
    return final_path, audio_duration, render_time


# ─────────────────────────────────────────────
# Supabase upload
# ─────────────────────────────────────────────

def upload_to_supabase(local_path: Path, song_id: str) -> str:
    """
    Stephen 2026-06-15: bypass the supabase-py client entirely and
    PUT to the Storage REST API with httpx.

    Upload-body shape matters: passing `content=open(file)` made
    httpx stream the body with chunked transfer encoding and no
    Content-Length, and Supabase Storage rejects that with a 400.
    Read the whole file into memory before sending so httpx sets
    Content-Length and uses a single PUT. Files are 10-150MB, fine
    in RAM on any RunPod worker.
    """
    import httpx
    storage_path = f"{song_id}/song.mp4"
    url = f"{SUPABASE_URL}/storage/v1/object/{SUPABASE_BUCKET}/{storage_path}"
    size = local_path.stat().st_size
    log(f"uploading to Supabase via httpx: {url}  size={size:,}")
    t0 = time.time()
    with open(local_path, "rb") as f:
        body = f.read()
    r = httpx.put(
        url,
        content=body,
        headers={
            "Authorization": f"Bearer {SUPABASE_KEY}",
            "Content-Type": "video/mp4",
            "x-upsert": "true",
        },
        timeout=300,
    )
    log(f"  HTTP {r.status_code}  {time.time()-t0:.1f}s")
    if r.status_code >= 400:
        # Surface the actual server message so future failures
        # don't bury the why behind a generic httpx exception.
        log(f"  response body: {r.text[:2000]}", "ERROR")
    r.raise_for_status()
    public_url = f"{SUPABASE_URL}/storage/v1/object/public/{SUPABASE_BUCKET}/{storage_path}"
    log(f"✓ upload done → {public_url}")
    return public_url


# ─────────────────────────────────────────────
# RunPod handler
# ─────────────────────────────────────────────

def handler(job: dict) -> dict:
    log(f"━━━ job received: id={job.get('id')} ━━━")
    log(f"  raw input keys: {list(job.get('input', {}).keys())}")

    input_data = job["input"]

    if not input_data.get("audio_url"):
        raise ValueError("Missing required field: audio_url")
    if not input_data.get("slides"):
        raise ValueError("Missing required field: slides (must be a non-empty list)")

    song_id = input_data.get("song_id", "untitled")
    log(f"  song_id={song_id}  slides={len(input_data['slides'])}")

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        log(f"  working tmpdir: {tmpdir}")
        video_path, audio_duration, render_time = render_video(input_data, tmpdir)
        size_bytes = video_path.stat().st_size
        video_url = upload_to_supabase(video_path, song_id)

    result = {
        "video_url": video_url,
        "duration_sec": round(audio_duration, 3),
        "size_bytes": size_bytes,
        "render_time_sec": round(render_time, 2),
    }
    log(f"━━━ job done: {result} ━━━")
    return result


if __name__ == "__main__":
    runpod.serverless.start({"handler": handler})
