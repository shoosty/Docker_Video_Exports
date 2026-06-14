"""
Local smoke test for handler.py.

Drop these files into ./fixtures/ before running:
    sample-audio.mp3   (any ~30s MP3 will do)
    slide1.jpg         (any image)
    slide2.jpg         (any image)
    slide3.jpg         (any image, optional)
    qr.png             (any PNG, optional)

Then from the repo root:
    python test/test_handler.py

Output lands in ./test/out/test.mp4 — open it in any video player.

This bypasses RunPod's serverless wrapper and calls handler(job)
directly. It also bypasses Supabase upload (the test version of
upload_to_supabase below just copies the file to ./test/out/).
"""

import os
import sys
import shutil
from pathlib import Path
from unittest.mock import patch


HERE = Path(__file__).resolve().parent
FIXTURES = HERE / "fixtures"
OUT_DIR = HERE / "out"
OUT_DIR.mkdir(exist_ok=True)

# Add the repo root to sys.path so we can import handler.py
sys.path.insert(0, str(HERE.parent))


def fake_download(url: str, dest: Path) -> None:
    """Treat 'url' as a fixture filename and copy from ./fixtures/."""
    src = FIXTURES / url
    if not src.exists():
        raise FileNotFoundError(f"fixture missing: {src}")
    shutil.copyfile(src, dest)


def fake_upload(local_path: Path, song_id: str, generation_id) -> str:
    """Copy to ./test/out/ instead of uploading."""
    leaf = f"{generation_id}.mp4" if generation_id else "test.mp4"
    out = OUT_DIR / leaf
    shutil.copyfile(local_path, out)
    return f"file://{out}"


def main() -> None:
    # Stub env so handler imports without real Supabase credentials.
    os.environ.setdefault("SUPABASE_URL", "http://localhost")
    os.environ.setdefault("SUPABASE_SERVICE_ROLE_KEY", "stub")

    import handler  # noqa: E402

    job = {
        "input": {
            "audio_url": "sample-audio.mp3",
            "slides": [
                {"image_url": "slide1.jpg", "start_sec": 0.0, "end_sec": 10.0},
                {"image_url": "slide2.jpg", "start_sec": 10.0, "end_sec": 20.0},
            ],
            "title": "Test Song",
            "artist": "Shoosty",
            "qr_code_url": "qr.png" if (FIXTURES / "qr.png").exists() else None,
            "song_id": "test-song-id",
            "generation_id": "test-gen-id",
        }
    }
    # Add slide3 if present.
    if (FIXTURES / "slide3.jpg").exists():
        job["input"]["slides"].append(
            {"image_url": "slide3.jpg", "start_sec": 20.0, "end_sec": 30.0}
        )

    with patch.object(handler, "download", fake_download), \
         patch.object(handler, "upload_to_supabase", fake_upload):
        result = handler.handler(job)

    print("\n--- handler returned ---")
    for k, v in result.items():
        print(f"  {k}: {v}")
    print(f"\nOpen this file to inspect:\n  {OUT_DIR / 'test-gen-id.mp4'}\n")


if __name__ == "__main__":
    main()
