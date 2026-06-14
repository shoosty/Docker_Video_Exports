"""
Docker_Video_Exports — RunPod serverless handler.

Renders a song's audio + slides into an MP4 video, uploads to
Supabase Storage, returns the public URL.

Input (job["input"]):
    audio_url        str          — Supabase URL of the song MP3
    slides           list[dict]   — [{ image_url, start_sec, end_sec }, ...]
    title            str | None   — for the end card
    artist           str | None   — for the end card
    qr_code_url      str | None   — if present, append a 4s end card
    song_id          str          — Supabase row id, used as storage prefix
    generation_id    str | None   — Supabase row id for the generation
    lyric_timing     None         — reserved for v2 karaoke caption burn-in

Output:
    video_url        str          — public Supabase URL of the rendered MP4
    duration_sec     float        — audio duration (not counting end card)
    size_bytes       int          — file size on disk
    render_time_sec  float        — wall-clock seconds spent in ffmpeg

Failure: raises an exception. RunPod surfaces the message to the caller.
"""

import os
import sys
import time
import tempfile
import subprocess
from pathlib import Path

import requests
import runpod
from supabase import create_client


# ──────────────────────────────────────────────────────────────
# Env + constants
# ──────────────────────────────────────────────────────────────

SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_KEY = (
    os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    or os.environ.get("SUPABASE_SECRET_KEY")
)
if not SUPABASE_KEY:
    print("WARNING: neither SUPABASE_SERVICE_ROLE_KEY nor SUPABASE_SECRET_KEY set", file=sys.stderr)

SUPABASE_BUCKET = os.environ.get("SUPABASE_VIDEO_BUCKET", "song-videos")

# Cream paper + dark ink — matches the dadbotstudio.com design system.
CREAM_HEX = "F5F0E8"
INK_HEX = "333333"

# Crossfade between adjacent slides. 1s feels right — fast enough to
# stay snappy, slow enough that the audience registers the change.
XFADE_SECONDS = 1.0

# End card length. Long enough to read the QR + tap it.
END_CARD_SECONDS = 4.0


# ──────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────

def download(url: str, dest: Path) -> None:
    """Stream a URL to disk. Raises on non-2xx."""
    r = requests.get(url, timeout=120, stream=True)
    r.raise_for_status()
    with open(dest, "wb") as f:
        for chunk in r.iter_content(chunk_size=1 << 14):
            if chunk:
                f.write(chunk)


def probe_audio_duration(audio_path: Path) -> float:
    """Return audio duration in seconds via ffprobe."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def build_slides_command(
    slide_paths: list[Path],
    slides_meta: list[dict],
    audio_path: Path,
    audio_duration: float,
    output_path: Path,
) -> list[str]:
    """
    Build the ffmpeg command that composites N slides + audio into MP4.

    For each slide, we use -loop 1 -t <dur> -i <path>. Each slide is
    scaled+padded to 1920x1080 with letterboxing (no stretching).
    Adjacent slides are joined with a 1s xfade. The last slide's input
    duration extends so it stays on screen until the audio ends.
    """
    n = len(slide_paths)
    cmd = ["ffmpeg", "-y"]

    # ── Inputs: one -loop -t per slide, then the audio ──────────
    for i, meta in enumerate(slides_meta):
        # How long does the i-th input file need to last?
        # - For slides 0..N-2: until the next slide takes over + xfade overlap
        # - For slide N-1: until end of audio (+ small safety margin)
        if i < n - 1:
            input_duration = slides_meta[i + 1]["start_sec"] - meta["start_sec"] + XFADE_SECONDS
        else:
            input_duration = audio_duration - meta["start_sec"] + XFADE_SECONDS
        # Floor at 0.1s so a degenerate input doesn't crash ffmpeg.
        input_duration = max(input_duration, 0.1)
        cmd += ["-loop", "1", "-t", f"{input_duration:.3f}", "-i", str(slide_paths[i])]

    # Audio is the last input — index = n
    cmd += ["-i", str(audio_path)]

    # ── Filter graph ─────────────────────────────────────────────
    parts = []

    # 1) Normalize every slide to 1920x1080@30fps with black letterbox.
    for i in range(n):
        parts.append(
            f"[{i}:v]scale=1920:1080:force_original_aspect_ratio=decrease,"
            f"pad=1920:1080:(ow-iw)/2:(oh-ih)/2:color=black,"
            f"setsar=1,fps=30,format=yuv420p[v{i}]"
        )

    # 2) Chain crossfades. For N slides, we need N-1 xfade filters.
    #    xfade offset = the time (in the OUTPUT stream's clock) when the
    #    crossfade starts. Centered on slides_meta[i]["start_sec"], so
    #    offset = start_sec - XFADE_SECONDS/2.
    if n == 1:
        final_video_label = "v0"
    else:
        prev_label = "v0"
        for i in range(1, n):
            offset = slides_meta[i]["start_sec"] - (XFADE_SECONDS / 2.0)
            # Never let offset go negative.
            offset = max(offset, 0.0)
            new_label = "vout" if i == n - 1 else f"vx{i}"
            parts.append(
                f"[{prev_label}][v{i}]xfade=transition=fade:duration={XFADE_SECONDS}:"
                f"offset={offset:.3f}[{new_label}]"
            )
            prev_label = new_label
        final_video_label = "vout"

    filter_complex = ";".join(parts)

    # ── Encoder settings ────────────────────────────────────────
    cmd += [
        "-filter_complex", filter_complex,
        "-map", f"[{final_video_label}]",
        "-map", f"{n}:a",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        "-shortest",
        str(output_path),
    ]
    return cmd


def build_end_card_command(
    qr_path: Path,
    title: str | None,
    artist: str | None,
    output_path: Path,
) -> list[str]:
    """
    Render a 4-second end card: cream background, QR code centered,
    song title above, "made with Shoosty Studio" below. Silent audio
    is injected so we can concat with the main video without ffmpeg
    complaining about missing audio streams.
    """
    title_text = (title or "").replace(":", r"\:").replace("'", r"\'")
    artist_text = (artist or "").replace(":", r"\:").replace("'", r"\'")

    # Escape colons + single quotes for drawtext's odd parser.
    drawtext_filters = []
    if title_text:
        drawtext_filters.append(
            f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf:"
            f"text='{title_text}':fontsize=64:fontcolor=0x{INK_HEX}:"
            f"x=(w-text_w)/2:y=180"
        )
    if artist_text:
        drawtext_filters.append(
            f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
            f"text='{artist_text}':fontsize=36:fontcolor=0x{INK_HEX}:"
            f"x=(w-text_w)/2:y=260"
        )
    drawtext_filters.append(
        f"drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf:"
        f"text='made with Shoosty Studio':fontsize=32:fontcolor=0x{INK_HEX}:"
        f"x=(w-text_w)/2:y=h-150"
    )

    drawtext_chain = ",".join(drawtext_filters)

    filter_complex = (
        # Scale QR to 480x480, center it.
        "[1:v]scale=480:480[qr];"
        # Cream background → overlay QR → draw text on top.
        f"[0:v][qr]overlay=(W-w)/2:(H-h)/2,{drawtext_chain}[vcard]"
    )

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi", "-i", f"color=c=0x{CREAM_HEX}:s=1920x1080:d={END_CARD_SECONDS}:r=30",
        "-i", str(qr_path),
        # Silent audio matching the main video's spec so concat works.
        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate=44100",
        "-filter_complex", filter_complex,
        "-map", "[vcard]",
        "-map", "2:a",
        "-t", f"{END_CARD_SECONDS}",
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    return cmd


def concat_videos(main_path: Path, end_path: Path, output_path: Path, tmpdir: Path) -> None:
    """
    Concat main + end card using ffmpeg's concat demuxer. Re-encode
    is required because the two inputs may have different timebases.
    """
    concat_list = tmpdir / "concat.txt"
    with open(concat_list, "w") as f:
        f.write(f"file '{main_path}'\n")
        f.write(f"file '{end_path}'\n")

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c:v", "libx264", "-preset", "medium", "-crf", "22", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "192k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"concat failed:\n{result.stderr[-2000:]}")


def upload_to_supabase(local_path: Path, song_id: str, generation_id: str | None) -> str:
    """
    Upload the MP4 to Supabase Storage. Returns the public URL.
    Path: {song_id}/{generation_id or 'song'}.mp4
    """
    sb = create_client(SUPABASE_URL, SUPABASE_KEY)
    leaf = f"{generation_id}.mp4" if generation_id else "song.mp4"
    storage_path = f"{song_id}/{leaf}"

    with open(local_path, "rb") as f:
        sb.storage.from_(SUPABASE_BUCKET).upload(
            storage_path,
            f,
            file_options={"content-type": "video/mp4", "upsert": "true"},
        )
    public = sb.storage.from_(SUPABASE_BUCKET).get_public_url(storage_path)
    # supabase-py returns the URL with a trailing "?" on some versions; strip it.
    return public.rstrip("?")


# ──────────────────────────────────────────────────────────────
# RunPod entry point
# ──────────────────────────────────────────────────────────────

def render(input_data: dict, tmpdir: Path) -> tuple[Path, float, float]:
    """Do the rendering work. Returns (final_path, audio_duration, render_seconds)."""
    if "audio_url" not in input_data:
        raise ValueError("audio_url is required")
    slides = input_data.get("slides") or []
    if not slides:
        raise ValueError("at least one slide is required")

    # ── Download audio + slides ─────────────────────────────────
    audio_path = tmpdir / "audio.mp3"
    download(input_data["audio_url"], audio_path)
    audio_duration = probe_audio_duration(audio_path)

    slide_paths: list[Path] = []
    for i, slide in enumerate(slides):
        if "image_url" not in slide:
            raise ValueError(f"slide {i} missing image_url")
        ext = Path(slide["image_url"]).suffix or ".jpg"
        p = tmpdir / f"slide_{i}{ext}"
        download(slide["image_url"], p)
        slide_paths.append(p)

    qr_path = None
    if input_data.get("qr_code_url"):
        qr_path = tmpdir / "qr.png"
        download(input_data["qr_code_url"], qr_path)

    # ── Render the main slides+audio video ──────────────────────
    main_path = tmpdir / "main.mp4"
    main_cmd = build_slides_command(
        slide_paths, slides, audio_path, audio_duration, main_path
    )
    t0 = time.time()
    result = subprocess.run(main_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        # Last 2KB of stderr is usually where the real error is.
        raise RuntimeError(f"main render failed:\n{result.stderr[-2000:]}")

    # ── Optionally append the end card ──────────────────────────
    if qr_path is not None:
        end_path = tmpdir / "endcard.mp4"
        end_cmd = build_end_card_command(
            qr_path, input_data.get("title"), input_data.get("artist"), end_path
        )
        result = subprocess.run(end_cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"end card render failed:\n{result.stderr[-2000:]}")

        final_path = tmpdir / "final.mp4"
        concat_videos(main_path, end_path, final_path, tmpdir)
    else:
        final_path = main_path

    render_seconds = time.time() - t0
    return final_path, audio_duration, render_seconds


def handler(job: dict) -> dict:
    """RunPod entrypoint."""
    input_data = job.get("input") or {}
    song_id = input_data.get("song_id") or "untitled"
    generation_id = input_data.get("generation_id")

    print(f"[video-render] song_id={song_id} gen_id={generation_id} slides={len(input_data.get('slides') or [])}", flush=True)

    with tempfile.TemporaryDirectory() as td:
        tmpdir = Path(td)
        final_path, audio_duration, render_seconds = render(input_data, tmpdir)
        size_bytes = final_path.stat().st_size

        print(f"[video-render] rendered {size_bytes} bytes in {render_seconds:.1f}s; uploading", flush=True)
        video_url = upload_to_supabase(final_path, song_id, generation_id)

    return {
        "video_url": video_url,
        "duration_sec": audio_duration,
        "size_bytes": size_bytes,
        "render_time_sec": round(render_seconds, 2),
    }


if __name__ == "__main__":
    # When running inside RunPod's serverless runner, this starts the
    # worker loop. The local test harness calls handler(job) directly
    # and never reaches this branch.
    runpod.serverless.start({"handler": handler})
