#!/usr/bin/env python3
"""
Download a single YouTube video as best-quality audio (.m4b).

  ./download_video.sh
  ./download_video.sh "https://www.youtube.com/watch?v=..."
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

OUTPUT_DIR = Path(__file__).resolve().parent / "downloads" / "Single Videos"


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def require_tools() -> str:
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        die("yt-dlp not found. Install with: brew install yt-dlp")
    if not shutil.which("ffmpeg"):
        die("ffmpeg not found. Install with: brew install ffmpeg")
    return yt_dlp


def ask_url() -> str:
    if len(sys.argv) > 1:
        return sys.argv[1].strip()
    try:
        url = input("Paste YouTube video URL: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        sys.exit(0)
    return url


def is_youtube_url(url: str) -> bool:
    return bool(
        re.search(
            r"(youtube\.com/(watch|shorts|live)|youtu\.be/)",
            url,
            re.I,
        )
    )


def tag_m4b(path: Path) -> Path:
    """Rename .m4a → .m4b and set basic audiobook-friendly tags when possible."""
    try:
        from mutagen.mp4 import MP4
    except ImportError:
        if path.suffix.lower() == ".m4a":
            new = path.with_suffix(".m4b")
            path.rename(new)
            return new
        return path

    audio_path = path
    if path.suffix.lower() == ".m4a":
        audio_path = path.with_suffix(".m4b")
        path.rename(audio_path)

    audio = MP4(audio_path)
    if not audio.get("\xa9gen"):
        audio["\xa9gen"] = ["Audiobook"]
    audio["stik"] = [1]  # Normal (single file, not a multi-chapter book)
    audio.save()
    return audio_path


def download(yt_dlp: str, url: str) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    archive = OUTPUT_DIR / ".download-archive.txt"
    outtmpl = str(OUTPUT_DIR / "%(uploader).80B - %(title).180B [%(id)s].%(ext)s")

    cmd = [
        yt_dlp,
        "-f",
        "bestaudio/best",
        "-x",
        "--audio-format",
        "m4a",
        "--audio-quality",
        "0",
        "--embed-metadata",
        "--embed-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--add-metadata",
        "--no-overwrites",
        "--continue",
        "--download-archive",
        str(archive),
        "--output",
        outtmpl,
        "--windows-filenames",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--newline",
        "--progress",
        url,
    ]

    print(f"\nDownloading → {OUTPUT_DIR}\n")
    result = subprocess.run(cmd, check=False)
    if result.returncode not in (0, 1):
        die(f"yt-dlp failed with exit code {result.returncode}")

    # Tag newest matching audio if present
    candidates = sorted(
        list(OUTPUT_DIR.glob("*.m4a")) + list(OUTPUT_DIR.glob("*.m4b")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if candidates:
        final = tag_m4b(candidates[0]) if candidates[0].suffix.lower() == ".m4a" else candidates[0]
        # Also convert any leftover m4a from this run
        for p in OUTPUT_DIR.glob("*.m4a"):
            tag_m4b(p)
        print(f"\nSaved: {final if final.exists() else OUTPUT_DIR}")
    else:
        print(f"\nDone (check folder): {OUTPUT_DIR}")


def main() -> None:
    yt_dlp = require_tools()
    url = ask_url()
    if not url:
        die("No URL provided.")
    if not is_youtube_url(url):
        die("That does not look like a YouTube video link.")
    download(yt_dlp, url)


if __name__ == "__main__":
    main()
