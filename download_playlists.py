#!/usr/bin/env python3
"""
Generic YouTube channel playlist → Apple Books audiobook downloader.

Edit CHANNEL_URL below, then:
  python3 download_playlists.py refresh     # fetch & cache playlists
  python3 download_playlists.py             # interactive picker
  python3 download_playlists.py list
  python3 download_playlists.py download 1 3-5
  python3 download_playlists.py download all
  python3 download_playlists.py test        # 1 episode smoke test

Each playlist becomes one "book" (folder of .m4b chapters numbered in order).
Downloads run --jobs at a time while finished ones convert/tag --convert-jobs at a time;
failed steps are retried, never silently skipped. In a terminal it opens a full-window
view (Textual): scroll with the arrow keys, r = retry selected, R = retry all issues,
1-5 = filters, / = search, q = quit. The window stays open after the run finishes.
Standalone channel videos (no playlist) are ignored.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

# =============================================================================
# CONFIG — paste any channel URL here
# =============================================================================
CHANNEL_URL = "https://www.youtube.com/@bayyinah"
OUTPUT_ROOT = Path(__file__).resolve().parent / "downloads"
# =============================================================================

CACHE_NAME = "playlists_cache.json"
CHANNEL_META_NAME = "channel.json"


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def require_tools() -> str:
    yt_dlp = shutil.which("yt-dlp")
    if not yt_dlp:
        die("yt-dlp not found. Install with: brew install yt-dlp")
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        die("ffmpeg/ffprobe not found. Install with: brew install ffmpeg")
    try:
        import mutagen  # noqa: F401
    except ImportError:
        die("mutagen not found. Install with: pip install mutagen")
    return yt_dlp


def sanitize_name(name: str, max_len: int = 120) -> str:
    name = (name or "").strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")
    return (name or "Untitled")[:max_len]


def playlists_tab_url(channel_url: str) -> str:
    url = channel_url.strip().rstrip("/")
    if url.endswith("/playlists"):
        return url
    # strip common tabs
    for tab in ("/videos", "/streams", "/shorts", "/community", "/about", "/featured"):
        if url.endswith(tab):
            url = url[: -len(tab)]
            break
    return url + "/playlists"


def channel_dir_from_meta(meta: dict) -> Path:
    name = sanitize_name(meta.get("channel") or meta.get("uploader") or "channel")
    return OUTPUT_ROOT / name


def cache_path(channel_dir: Path) -> Path:
    return channel_dir / CACHE_NAME


def channel_meta_path(channel_dir: Path) -> Path:
    return channel_dir / CHANNEL_META_NAME


def load_cache(channel_dir: Path) -> dict | None:
    path = cache_path(channel_dir)
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def save_cache(channel_dir: Path, channel_meta: dict, playlists: list[dict]) -> None:
    channel_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "channel_url": CHANNEL_URL,
        "playlists_url": playlists_tab_url(CHANNEL_URL),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "channel": channel_meta,
        "playlists": playlists,
    }
    cache_path(channel_dir).write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    channel_meta_path(channel_dir).write_text(
        json.dumps(channel_meta, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Cached {len(playlists)} playlist(s) → {cache_path(channel_dir)}")


def find_existing_channel_dir() -> Path | None:
    """Locate downloads/<Channel>/playlists_cache.json for current CHANNEL_URL."""
    if not OUTPUT_ROOT.exists():
        return None
    wanted = playlists_tab_url(CHANNEL_URL)
    for path in OUTPUT_ROOT.glob(f"*/{CACHE_NAME}"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        cached_url = data.get("playlists_url") or playlists_tab_url(data.get("channel_url") or "")
        if cached_url.rstrip("/") == wanted.rstrip("/"):
            return path.parent
    return None


def fetch_playlists(yt_dlp: str) -> tuple[dict, list[dict]]:
    url = playlists_tab_url(CHANNEL_URL)
    print(f"Fetching playlists from:\n  {url}\n")
    cmd = [
        yt_dlp,
        "--flat-playlist",
        "--dump-single-json",
        "--skip-download",
        url,
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        die(f"Failed to list playlists:\n{result.stderr or result.stdout}")

    data = json.loads(result.stdout)
    channel_meta = {
        "id": data.get("channel_id") or data.get("uploader_id") or data.get("id"),
        "channel": data.get("channel") or data.get("uploader") or data.get("title") or "channel",
        "uploader": data.get("uploader") or data.get("channel"),
        "webpage_url": data.get("channel_url") or data.get("webpage_url") or CHANNEL_URL,
    }

    playlists: list[dict] = []
    for entry in data.get("entries") or []:
        if not entry:
            continue
        playlist_id = entry.get("id")
        if not playlist_id:
            continue
        title = entry.get("title") or entry.get("playlist_title") or playlist_id
        pl_url = entry.get("url") or entry.get("webpage_url") or ""
        if "playlist?list=" not in pl_url:
            pl_url = f"https://www.youtube.com/playlist?list={playlist_id}"
        playlists.append({"id": playlist_id, "title": title, "url": pl_url})

    if not playlists:
        die("No playlists found. Check CHANNEL_URL at the top of this file.")
    return channel_meta, playlists


def print_playlists(playlists: list[dict], channel_name: str, fetched_at: str | None = None) -> None:
    stamp = f"  (cached {fetched_at})" if fetched_at else ""
    print(f"\n{channel_name} — {len(playlists)} playlist(s){stamp}\n")
    for i, pl in enumerate(playlists, 1):
        print(f"  {i:3d}. {pl['title']}")
    print()


def parse_selection(text: str, count: int) -> list[int] | None:
    """Parse 'all', '1', '1,3', '2-5', '1 3-4'. Returns 1-based indices or None if quit."""
    text = text.strip().lower()
    if text in {"q", "quit", "exit"}:
        return None
    if text in {"all", "a", "*"}:
        return list(range(1, count + 1))

    indices: set[int] = set()
    for part in re.split(r"[,\s]+", text):
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
            if start > end:
                start, end = end, start
            for n in range(start, end + 1):
                if 1 <= n <= count:
                    indices.add(n)
        else:
            n = int(part)
            if 1 <= n <= count:
                indices.add(n)
    return sorted(indices)


AUDIO_EXTS = {".m4a", ".webm", ".opus", ".mp4", ".mp3", ".ogg", ".aac", ".mka", ".weba"}
BACKOFF = (5, 20, 60)  # seconds to wait before retry 1, 2, 3+
BOT_BACKOFF = (90, 240, 480)  # YouTube bot check / rate limit: wait minutes, for ALL workers
NETWORK_BACKOFF = (5, 15, 45, 90, 180)  # flaky connection: more tries, growing waits
CONVERT_BACKOFF = (3,)  # a failed conversion is rarely fixed by waiting: 1 retry, then a fresh download
NETWORK_MARKERS = (
    "timed out",
    "timeout",
    "connection",
    "reset by peer",
    "unable to download",
    "http error 5",
    "incomplete read",
    "name or service not known",
    "nodename nor servname",
    "ssl",
    "transporterror",
    "temporary failure",
)
PERMANENT_MARKERS = (
    "video unavailable",
    "video is unavailable",
    "video is not available",
    "private video",
    "has been removed",
    "members-only",
    "join this channel",
    "this video is not available",
)
ACTIVE_STAGES = {"downloading", "to convert", "converting", "tagging", "retrying"}
STAGE_STYLE = {
    "queued": "dim",
    "downloading": "cyan",
    "to convert": "blue",
    "converting": "magenta",
    "tagging": "magenta",
    "retrying": "yellow",
    "stopped": "yellow",
    "done": "green",
    "failed": "bold red",
}


class Stopped(Exception):
    """Raised inside a worker when the user interrupted the run."""


class BotCheckError(RuntimeError):
    """YouTube asked to sign in / confirm we're not a bot; needs a long pause, not a quick retry."""


class PermanentError(Exception):
    """An error retrying cannot fix (private / removed video)."""


@dataclass
class Job:
    book: str
    book_dir: Path
    playlist_title: str
    index: int
    total: int
    vid: str
    title: str
    url: str
    stage: str = "queued"
    pct: float = 0.0
    speed: float | None = None
    eta: float | None = None
    size: int | None = None
    attempt: int = 0
    retries: int = 0  # times this episode was retried (in-stage, re-download, or re-queued)
    tries: dict = field(default_factory=dict)  # stage -> total attempts made, never reset
    redownloaded: bool = False
    note: str = ""
    error: str = ""
    failed_stage: str = ""
    permanent: bool = False
    raw: Path | None = None
    finished_at: float = 0.0

    @property
    def work_dir(self) -> Path:
        return self.book_dir / ".work"

    @property
    def label(self) -> str:
        return f"{self.book} #{self.index:03d} {self.title[:50]}"


def fmt_size(n: float | None) -> str:
    if not n:
        return ""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return ""


def fmt_secs(n: float | None) -> str:
    if n is None:
        return ""
    n = int(n)
    h, rem = divmod(n, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


class Reporter:
    """Shared run state. Prints log lines only in plain mode (the Textual window reads the jobs directly)."""

    def __init__(self, jobs: list[Job], plain: bool = True) -> None:
        self.jobs = jobs
        self.started = time.monotonic()
        self.plain = plain
        self.status_line = ""
        self._print_lock = threading.Lock()

    def stage(self, job: Job, stage: str, note: str = "") -> None:
        job.stage = stage
        job.note = note
        if stage in ("done", "failed"):
            job.finished_at = time.monotonic()
            job.speed = job.eta = None
        if stage == "done":
            job.pct = 100.0
        if self.plain:
            msg = f"{job.label}: {stage}" + (f" — {note}" if note else "")
            with self._print_lock:
                print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


@dataclass
class Context:
    yt_dlp: str
    ffmpeg: str
    ffprobe: str
    cookie_args: list[str]
    sleep_interval: float
    attempts: int
    rep: Reporter
    stop: threading.Event = field(default_factory=threading.Event)
    procs: set = field(default_factory=set)
    proc_lock: threading.Lock = field(default_factory=threading.Lock)
    archive_lock: threading.Lock = field(default_factory=threading.Lock)
    cooldown_until: float = 0.0  # monotonic time before which no new download may start

    def kill_all(self) -> None:
        with self.proc_lock:
            for proc in list(self.procs):
                try:
                    proc.terminate()
                except OSError:
                    pass


def resolve_browser(spec: str) -> str:
    """yt-dlp has no 'zen' browser; Zen is Firefox-based, so point yt-dlp's firefox reader at Zen's profile."""
    if spec.split(":", 1)[0].strip().lower() != "zen":
        return spec
    root = Path.home() / "Library/Application Support/zen/Profiles"
    jars = sorted(root.glob("*/cookies.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not jars:
        die(f"No Zen profile with cookies found under {root}")
    return f"firefox:{jars[0].parent}"


def cookie_args(cookies_from_browser: str | None, cookies_file: Path | None) -> list[str]:
    args: list[str] = []
    if cookies_from_browser:
        args += ["--cookies-from-browser", resolve_browser(cookies_from_browser)]
    if cookies_file:
        args += ["--cookies", str(cookies_file)]
    return args


def run_cmd(ctx: Context, cmd: list[str], on_line) -> tuple[int, list[str]]:
    """Run cmd, feeding each output line to on_line (returns True if consumed)."""
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        bufsize=1,
    )
    with ctx.proc_lock:
        ctx.procs.add(proc)
    tail: deque[str] = deque(maxlen=6)
    try:
        assert proc.stdout is not None
        for line in proc.stdout:
            line = line.rstrip()
            if line and not on_line(line):
                tail.append(line)
        rc = proc.wait()
    finally:
        with ctx.proc_lock:
            ctx.procs.discard(proc)
    if ctx.stop.is_set():
        raise Stopped()
    return rc, list(tail)


def _num(text: str) -> float | None:
    try:
        return float(text)
    except ValueError:
        return None


def find_raw(job: Job) -> Path | None:
    if not job.work_dir.exists():
        return None
    for p in sorted(job.work_dir.glob(f"{job.vid}.*")):
        if p.suffix.lower() in AUDIO_EXTS:
            return p
    return None


def do_download(ctx: Context, job: Job) -> None:
    job.work_dir.mkdir(parents=True, exist_ok=True)
    cmd = [
        ctx.yt_dlp,
        *ctx.cookie_args,
        "--no-playlist",
        "-f",
        "bestaudio/best",
        "--write-thumbnail",
        "--convert-thumbnails",
        "jpg",
        "--no-overwrites",
        "--continue",
        "--retries",
        "10",
        "--fragment-retries",
        "10",
        "--sleep-interval",
        str(ctx.sleep_interval),
        "--max-sleep-interval",
        str(max(ctx.sleep_interval * 2, ctx.sleep_interval + 1)),
        "--newline",
        "--progress",
        "--progress-template",
        "download:[dl] %(progress.downloaded_bytes)s|%(progress.total_bytes)s"
        "|%(progress.total_bytes_estimate)s|%(progress.speed)s|%(progress.eta)s",
        "--output",
        str(job.work_dir / f"{job.vid}.%(ext)s"),
        job.url,
    ]

    def on_line(line: str) -> bool:
        if not line.startswith("[dl] "):
            return False
        done, total, est, speed, eta = (line[5:].split("|") + [""] * 5)[:5]
        size = _num(total) or _num(est)
        got = _num(done)
        if size and got is not None:
            job.pct = min(got / size * 100, 100.0)
            job.size = int(size)
        job.speed = _num(speed)
        job.eta = _num(eta)
        return True

    rc, tail = run_cmd(ctx, cmd, on_line)
    errors = [ln for ln in tail if "ERROR" in ln]
    msg = " | ".join((errors or tail)[-2:])
    if rc != 0:
        if any(m in msg.lower() for m in PERMANENT_MARKERS):
            raise PermanentError(msg)
        if "sign in to confirm" in msg.lower() or "not a bot" in msg.lower():
            raise BotCheckError(
                "YouTube bot check — retry with --cookies-from-browser <browser>, fewer --jobs, higher --sleep"
            )
        if "429" in msg or "too many requests" in msg.lower():
            raise BotCheckError("YouTube rate limit (HTTP 429) — pausing; try fewer --jobs, higher --sleep")
        raise RuntimeError(msg or f"yt-dlp exited {rc}")
    raw = find_raw(job)
    if not raw:
        raise RuntimeError("yt-dlp finished but no audio file was found")
    job.raw = raw
    job.size = raw.stat().st_size
    job.pct = 100.0
    job.speed = job.eta = None


def probe_audio(ctx: Context, path: Path) -> tuple[str, float]:
    out = subprocess.run(
        [
            ctx.ffprobe,
            "-v",
            "error",
            "-select_streams",
            "a:0",
            "-show_entries",
            "stream=codec_name:format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {out.stderr.strip() or out.returncode}")
    data = json.loads(out.stdout or "{}")
    streams = data.get("streams") or []
    if not streams:
        raise RuntimeError("no audio stream in downloaded file")
    return streams[0].get("codec_name", ""), float((data.get("format") or {}).get("duration") or 0)


def write_tags(path: Path, job: Job, artist: str, cover: Path | None) -> None:
    from mutagen.mp4 import MP4, MP4Cover  # type: ignore

    audio = MP4(path)
    audio["\xa9nam"] = [job.title]
    audio["\xa9alb"] = [job.playlist_title]
    audio["\xa9ART"] = [artist]
    audio["aART"] = [artist]
    audio["\xa9gen"] = ["Audiobook"]
    audio["trkn"] = [(job.index, job.total)]
    audio["stik"] = [2]  # Audiobook media kind
    if cover and cover.exists():
        audio["covr"] = [MP4Cover(cover.read_bytes(), imageformat=MP4Cover.FORMAT_JPEG)]
    audio.save()


def final_path(job: Job) -> Path:
    return job.book_dir / f"{job.index:03d} - {sanitize_name(job.title, 180)}.m4b"


def do_convert(ctx: Context, job: Job, artist: str) -> None:
    raw = job.raw or find_raw(job)
    if not raw or not raw.exists():
        raise RuntimeError("raw download missing")
    codec, duration = probe_audio(ctx, raw)
    tmp = job.work_dir / f"converting-{job.vid}.m4a"
    tmp.unlink(missing_ok=True)
    job.pct = 0.0
    audio_args = ["-c:a", "copy"] if codec == "aac" else ["-c:a", "aac", "-b:a", "192k"]
    cmd = [
        ctx.ffmpeg,
        "-y",
        "-nostdin",
        "-nostats",
        "-loglevel",
        "error",
        "-progress",
        "pipe:1",
        "-i",
        str(raw),
        "-vn",
        "-map",
        "0:a:0",
        "-map_metadata",
        "-1",
        *audio_args,
        "-f",
        "mp4",
        str(tmp),
    ]

    def on_line(line: str) -> bool:
        key, _, value = line.partition("=")
        if key == "out_time_us" and duration:
            us = _num(value)
            if us is not None:
                job.pct = min(us / 1e6 / duration * 100, 99.0)
            return True
        return key in {
            "progress",
            "out_time",
            "out_time_ms",
            "total_size",
            "bitrate",
            "speed",
            "stream_0_0_q",
        } or key.startswith(("frame", "fps", "dup_frames", "drop_frames"))

    rc, tail = run_cmd(ctx, cmd, on_line)
    if rc != 0 or not tmp.exists() or tmp.stat().st_size == 0:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(" | ".join(tail[-3:]) or f"ffmpeg exited {rc}")

    ctx.rep.stage(job, "tagging")
    cover = job.work_dir / f"{job.vid}.jpg"
    write_tags(tmp, job, artist, cover)
    os.replace(tmp, final_path(job))

    with ctx.archive_lock:
        with (job.book_dir / ".download-archive.txt").open("a", encoding="utf-8") as fh:
            fh.write(f"youtube {job.vid}\n")

    raw.unlink(missing_ok=True)
    cover.unlink(missing_ok=True)
    try:
        job.work_dir.rmdir()
    except OSError:
        pass  # other episodes of this book still in progress


def retry_policy(ctx: Context, stage: str, exc: Exception) -> tuple[int, tuple]:
    """(max tries, backoff table) for this kind of failure. ctx.attempts is the base for ordinary errors."""
    if isinstance(exc, BotCheckError):
        return ctx.attempts, BOT_BACKOFF
    if stage == "converting":
        return min(ctx.attempts, 2), CONVERT_BACKOFF
    if any(m in str(exc).lower() for m in NETWORK_MARKERS):
        return ctx.attempts + 2, NETWORK_BACKOFF
    return ctx.attempts, BACKOFF


def run_stage(ctx: Context, job: Job, stage: str, fn) -> None:
    """Run fn(job), retrying according to the cause of each failure; raise when retries run out."""
    attempt = 0
    while True:
        if ctx.stop.is_set():
            raise Stopped()
        attempt += 1
        job.attempt = attempt
        job.tries[stage] = job.tries.get(stage, 0) + 1
        ctx.rep.stage(job, stage)
        try:
            fn(job)
            return
        except (Stopped, PermanentError):
            raise
        except Exception as exc:  # noqa: BLE001
            job.error = str(exc)
            max_tries, table = retry_policy(ctx, stage, exc)
            if attempt >= max_tries:
                raise
            job.retries += 1
            delay = table[min(attempt - 1, len(table) - 1)]
            if isinstance(exc, BotCheckError):
                ctx.cooldown_until = max(ctx.cooldown_until, time.monotonic() + delay)  # pause every worker
            ctx.rep.stage(job, "retrying", f"{stage} failed (try {attempt}/{max_tries}), retry in {delay}s: {exc}")
            if ctx.stop.wait(delay):
                raise Stopped()


def fail_job(ctx: Context, job: Job, stage: str, exc: Exception) -> None:
    job.failed_stage = stage
    job.error = str(exc) or exc.__class__.__name__
    job.permanent = isinstance(exc, PermanentError)
    ctx.rep.stage(job, "failed", ("unavailable: " if job.permanent else "") + job.error)


def reset_job(ctx: Context, job: Job, note: str = "") -> None:
    """Put a job back to its starting state (first submit, auto-retry round, or a retry from the UI)."""
    if job.failed_stage == "converting" and job.raw:
        job.raw.unlink(missing_ok=True)  # converting kept failing → assume the raw file is bad
        job.raw = None
    if job.stage in ("failed", "stopped"):
        job.retries += 1  # a re-queue after a problem is a retry; the first submit is not
    job.redownloaded = False
    job.pct = 0.0
    job.attempt = 0
    job.permanent = False
    job.failed_stage = ""
    job.speed = job.eta = None
    ctx.rep.stage(job, "queued", note)


class Engine:
    """Long-lived download + convert worker pools; jobs can be submitted at any time (retries too).

    Worker counts can change while running: raising starts threads at once, lowering lets a
    worker finish its current episode and then exit. Pausing only stops *new* downloads.
    """

    LIMITS = {"dl": (1, 8), "cv": (1, 12)}

    def __init__(self, ctx: Context, artist: str, n_dl: int, n_cv: int) -> None:
        self.ctx = ctx
        self.artist = artist
        self.dl_q: queue.Queue[Job] = queue.Queue()
        self.cv_q: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        self._outstanding = 0
        self._closed = False
        self.paused = False
        self.target = {"dl": self._clamp("dl", n_dl), "cv": self._clamp("cv", n_cv)}
        self.live = {"dl": 0, "cv": 0}

    @classmethod
    def _clamp(cls, kind: str, n: int) -> int:
        low, high = cls.LIMITS[kind]
        return max(low, min(high, n))

    def start(self) -> None:
        for kind in ("dl", "cv"):
            self._grow(kind)

    def shutdown(self) -> None:
        self._closed = True

    def resize(self, kind: str, delta: int) -> int:
        """Change the target worker count of 'dl' or 'cv'; returns the new target."""
        with self._lock:
            self.target[kind] = self._clamp(kind, self.target[kind] + delta)
        self._grow(kind)
        return self.target[kind]

    def _grow(self, kind: str) -> None:
        worker = self._dl_worker if kind == "dl" else self._cv_worker
        while True:
            with self._lock:
                if self.live[kind] >= self.target[kind]:
                    return
                self.live[kind] += 1
            threading.Thread(target=worker, daemon=True).start()

    @property
    def busy(self) -> bool:
        return self._outstanding > 0

    def submit(self, job: Job, *, only_if_problem: bool = False, note: str = "") -> bool:
        with self._lock:
            if only_if_problem and job.stage not in ("failed", "stopped"):
                return False  # already re-queued (e.g. double click)
            self._outstanding += 1
            reset_job(self.ctx, job, note)
        self.dl_q.put(job)
        return True

    def _finish(self) -> None:
        with self._lock:
            self._outstanding -= 1

    def _next(self, kind: str, q: queue.Queue) -> Job | None:
        """Block for the next job; None means this worker should exit (stopped / scaled down)."""
        while True:
            if self.ctx.stop.is_set() or self._closed:
                return None
            with self._lock:
                if self.live[kind] > self.target[kind]:
                    self.live[kind] -= 1
                    return None
            if kind == "dl" and self.paused:
                self.ctx.stop.wait(0.3)
                continue
            try:
                return q.get(timeout=0.5)
            except queue.Empty:
                continue

    def _dl_worker(self) -> None:
        ctx = self.ctx
        while (job := self._next("dl", self.dl_q)) is not None:
            wait = ctx.cooldown_until - time.monotonic()
            if wait > 0:
                ctx.rep.stage(job, "retrying", f"paused {wait:.0f}s (YouTube bot check)")
                if ctx.stop.wait(wait):
                    return
            try:
                run_stage(ctx, job, "downloading", lambda j: do_download(ctx, j))
            except Stopped:
                return
            except Exception as exc:  # noqa: BLE001
                fail_job(ctx, job, "downloading", exc)
                self._finish()
                continue
            ctx.rep.stage(job, "to convert")
            self.cv_q.put(job)

    def _cv_worker(self) -> None:
        ctx = self.ctx
        while (job := self._next("cv", self.cv_q)) is not None:
            try:
                run_stage(ctx, job, "converting", lambda j: do_convert(ctx, j, self.artist))
            except Stopped:
                return
            except Exception as exc:  # noqa: BLE001
                if not job.redownloaded:  # the raw file may be the problem: download it again once
                    job.redownloaded = True
                    job.retries += 1
                    if job.raw:
                        job.raw.unlink(missing_ok=True)
                        job.raw = None
                    job.attempt = 0
                    job.pct = 0.0
                    ctx.rep.stage(job, "queued", f"conversion failed, downloading again: {exc}"[:200])
                    self.dl_q.put(job)
                    continue
                fail_job(ctx, job, "converting", exc)
                self._finish()
                continue
            ctx.rep.stage(job, "done")
            self._finish()


def job_key(job: Job) -> str:
    return f"{job.book}|{job.index}"


def have_textual() -> bool:
    return importlib.util.find_spec("textual") is not None


def make_app(ctx: Context, engine: Engine, jobs: list[Job], channel_name: str):
    """Full-window terminal UI (Textual): live scrollable table, filters, search, retry keys."""
    from rich.text import Text
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.screen import ModalScreen
    from textual.widgets import DataTable, Footer, Header, Input, Static

    filters = {
        "all": lambda j: True,
        "active": lambda j: j.stage in ACTIVE_STAGES,
        "queued": lambda j: j.stage == "queued",
        "issues": lambda j: j.stage in ("failed", "stopped"),
        "done": lambda j: j.stage == "done",
    }
    columns = [
        ("book", "Book", 18),
        ("idx", "#", 3),
        ("title", "Episode", 46),
        ("stage", "Stage", 11),
        ("progress", "Progress", 16),
        ("speed", "Speed", 9),
        ("eta", "ETA", 7),
        ("size", "Size", 8),
        ("retries", "Retries", 7),
        ("info", "Info", None),
    ]

    def clip(text: str, n: int) -> str:
        return text if len(text) <= n else text[: n - 1] + "…"

    def bar(pct: float, width: int = 10) -> str:
        filled = int(pct / 100 * width)
        return "█" * filled + "░" * (width - filled) + f" {pct:3.0f}%"

    def cells(j: Job) -> dict:
        downloading = j.stage == "downloading"
        return {
            "book": clip(j.book, 18),
            "idx": f"{j.index:03d}",
            "title": clip(j.title, 46),
            "stage": Text(j.stage, style=STAGE_STYLE.get(j.stage, "")),
            "progress": bar(j.pct),
            "speed": f"{fmt_size(j.speed)}/s" if downloading and j.speed else "",
            "eta": fmt_secs(j.eta) if downloading else "",
            "size": fmt_size(j.size),
            "retries": str(j.retries) if j.retries else "",
            "info": j.note or (j.error if j.stage == "failed" else ""),
        }

    def signature(j: Job) -> tuple:
        return (j.stage, int(j.pct), int(j.speed or 0), int(j.eta or 0), int(j.size or 0), j.retries, j.note, j.error)

    class ConfirmQuit(ModalScreen[bool]):
        BINDINGS = [("y", "yes", "Yes"), ("n", "no", "No"), ("escape", "no", "No")]
        CSS = """
        ConfirmQuit { align: center middle; }
        #confirm { width: 54; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
        """

        def compose(self) -> ComposeResult:
            yield Static("Downloads are still running.\nStop them and quit?\n\n[b]y[/b] stop & quit    [b]n[/b] keep going", id="confirm")

        def action_yes(self) -> None:
            self.dismiss(True)

        def action_no(self) -> None:
            self.dismiss(False)

    class DownloadApp(App):
        TITLE = "Downloads"
        CSS = """
        #stats { height: 1; padding: 0 1; }
        #banner { height: auto; padding: 0 1; background: $warning 20%; display: none; }
        #books { height: auto; max-height: 8; padding: 0 1; margin-top: 1; }
        #filters { height: 1; padding: 0 1; margin-top: 1; color: $text-muted; }
        #search { display: none; }
        #table { height: 1fr; }
        #detail { height: auto; max-height: 8; border-top: solid $primary; padding: 0 1; }
        """
        BINDINGS = [
            Binding("q", "request_quit", "Quit"),
            Binding("ctrl+c", "request_quit", "Quit", show=False, priority=True),
            Binding("ctrl+q", "request_quit", "Quit", show=False, priority=True),
            Binding("r", "retry_selected", "Retry"),
            Binding("R", "retry_all", "Retry all issues"),
            Binding("1", "view('all')", "All", show=False),
            Binding("2", "view('active')", "Active", show=False),
            Binding("3", "view('queued')", "Queued", show=False),
            Binding("4", "view('issues')", "Issues", show=False),
            Binding("5", "view('done')", "Done", show=False),
            Binding("slash", "search", "Search"),
            Binding("p", "toggle_pause", "Pause"),
            Binding("plus", "workers('dl', 1)", "Downloads +"),
            Binding("equals_sign", "workers('dl', 1)", "Downloads +", show=False),
            Binding("minus", "workers('dl', -1)", "Downloads -"),
            Binding("right_square_bracket", "workers('cv', 1)", "Converts +"),
            Binding("left_square_bracket", "workers('cv', -1)", "Converts -"),
            Binding("escape", "clear_search", "Clear", show=False),
        ]

        def __init__(self) -> None:
            super().__init__()
            self.sub_title = channel_name
            self.view_filter = "all"
            self.search_text = ""
            self._row_keys: list[str] = []
            self._row_sig: dict[str, tuple] = {}
            self._by_key = {job_key(j): j for j in jobs}
            self._elapsed = 0.0

        def compose(self) -> ComposeResult:
            yield Header(show_clock=True)
            yield Static(id="stats")
            yield Static(id="banner")
            yield Static(id="books")
            yield Static(id="filters")
            yield Input(placeholder="search title / book — Enter to keep, Esc to clear", id="search")
            yield DataTable(id="table", cursor_type="row", zebra_stripes=True)
            yield Static(id="detail")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#table", DataTable)
            for key, label, width in columns:
                table.add_column(label, width=width, key=key)
            table.focus()
            self.refresh_view()
            self.set_interval(0.5, self.refresh_view)

        # ---- rendering -------------------------------------------------------
        def _selected(self) -> Job | None:
            table = self.query_one("#table", DataTable)
            if self._row_keys and 0 <= table.cursor_row < len(self._row_keys):
                return self._by_key[self._row_keys[table.cursor_row]]
            return None

        def refresh_view(self) -> None:
            counts: dict[str, int] = {}
            for j in jobs:
                counts[j.stage] = counts.get(j.stage, 0) + 1
            issues = counts.get("failed", 0) + counts.get("stopped", 0)
            done = counts.get("done", 0)
            if engine.busy:
                self._elapsed = time.monotonic() - ctx.rep.started
            speed = sum(j.speed or 0 for j in jobs if j.stage == "downloading")

            stats = Text()
            stats.append(f"{done}/{len(jobs)} done", style="bold green")
            for label, n, style in (
                ("downloading", counts.get("downloading", 0), "cyan"),
                ("converting", counts.get("to convert", 0) + counts.get("converting", 0) + counts.get("tagging", 0), "magenta"),
                ("queued", counts.get("queued", 0), "dim"),
                ("retrying", counts.get("retrying", 0), "yellow"),
                ("issues", issues, "bold red"),
            ):
                if n:
                    stats.append(f"  ·  {n} {label}", style=style)
            if speed:
                stats.append(f"  ·  {fmt_size(speed)}/s", style="cyan")
            stats.append(f"  ·  elapsed {fmt_secs(self._elapsed)}", style="dim")
            stats.append(f"  ·  workers: {engine.target['dl']} download / {engine.target['cv']} convert", style="dim")
            if engine.paused:
                stats.append("  ·  PAUSED", style="bold yellow")
            self.query_one("#stats", Static).update(stats)

            pause = ctx.cooldown_until - time.monotonic()
            if pause > 0:
                banner = f"Paused {pause:.0f}s — YouTube bot check. Resumes automatically. Tip: run with --cookies-from-browser zen (or chrome)."
            elif engine.paused:
                banner = "Paused — running downloads finish, no new ones start (conversions continue). Press p to resume."
            elif ctx.rep.status_line:
                banner = ctx.rep.status_line
            elif not engine.busy and issues:
                banner = f"Finished with {issues} issue(s) — press 4 to list them, r to retry the selected one, R to retry all."
            elif not engine.busy:
                banner = f"All done — {done} episode(s) complete. Press q to quit."
            else:
                banner = ""
            banner_w = self.query_one("#banner", Static)
            banner_w.update(banner)
            banner_w.display = bool(banner)

            self.query_one("#books", Static).update(self._books_text())

            line = Text()
            for i, name in enumerate(filters, 1):
                n = sum(1 for j in jobs if filters[name](j))
                label = f" {i} {name.capitalize()} {n} "
                line.append(label, style="reverse bold" if name == self.view_filter else "")
                line.append("  ")
            if self.search_text:
                line.append(f"search: {self.search_text!r}", style="yellow")
            self.query_one("#filters", Static).update(line)

            self._refresh_table()
            sel = self._selected()
            self.query_one("#detail", Static).update(self._detail_text(sel))

        def _books_text(self) -> Text:
            books: dict[str, list[Job]] = {}
            for j in jobs:
                books.setdefault(j.book, []).append(j)
            ordered = sorted(books.items(), key=lambda kv: (all(j.stage == "done" for j in kv[1]), kv[0]))
            text = Text()
            for name, js in ordered[:6]:
                done = sum(j.stage == "done" for j in js)
                bad = sum(j.stage in ("failed", "stopped") for j in js)
                active = sum(j.stage in ACTIVE_STAGES for j in js)
                filled = int(done / len(js) * 24)
                text.append(f"{clip(name, 36):<36} ")
                text.append("█" * filled, style="green" if done == len(js) else "cyan")
                text.append("░" * (24 - filled), style="dim")
                text.append(f" {done}/{len(js)}")
                if active:
                    text.append(f"  {active} active", style="cyan")
                if bad:
                    text.append(f"  {bad} issue(s)", style="bold red")
                text.append("\n")
            if len(ordered) > 6:
                text.append(f"+ {len(ordered) - 6} more book(s)", style="dim")
            return text

        def _refresh_table(self) -> None:
            table = self.query_one("#table", DataTable)
            match = filters[self.view_filter]
            needle = self.search_text.strip().lower()
            visible = [j for j in jobs if match(j) and (not needle or needle in f"{j.title} {j.book}".lower())]
            keys = [job_key(j) for j in visible]
            if keys != self._row_keys:
                selected = self._row_keys[table.cursor_row] if self._row_keys and table.cursor_row < len(self._row_keys) else None
                old_cursor = table.cursor_row
                table.clear()
                self._row_sig = {}
                for j in visible:
                    c = cells(j)
                    table.add_row(*[c[k] for k, _, _ in columns], key=job_key(j))
                    self._row_sig[job_key(j)] = signature(j)
                self._row_keys = keys
                if keys:
                    row = keys.index(selected) if selected in keys else min(old_cursor, len(keys) - 1)
                    table.move_cursor(row=row, animate=False)
                return
            for j in visible:
                key = job_key(j)
                sig = signature(j)
                if self._row_sig.get(key) != sig:
                    self._row_sig[key] = sig
                    c = cells(j)
                    for col, _, _ in columns:
                        table.update_cell(key, col, c[col])

        def _detail_text(self, j: Job | None) -> Text:
            text = Text()
            if not j:
                text.append("No episode selected.", style="dim")
                return text
            text.append(f"{j.book} #{j.index:03d}  ", style="bold")
            text.append(j.title)
            text.append("\nStage: ")
            text.append(j.stage, style=STAGE_STYLE.get(j.stage, ""))
            if j.tries:
                names = {"downloading": "download", "converting": "convert"}
                text.append("  ·  tries: " + ", ".join(f"{names.get(k, k)} ×{v}" for k, v in j.tries.items()))
            if j.retries:
                text.append(f"  ·  retried {j.retries}×", style="yellow")
            text.append(f"\nFolder: {j.book_dir}\nVideo:  {j.url}")
            if j.error:
                text.append(f"\nError ({j.failed_stage or 'last attempt'}): {j.error}", style="red")
            return text

        # ---- actions ---------------------------------------------------------
        def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
            self.query_one("#detail", Static).update(self._detail_text(self._selected()))

        def action_view(self, name: str) -> None:
            self.view_filter = name
            self.refresh_view()

        def action_search(self) -> None:
            box = self.query_one("#search", Input)
            box.display = True
            box.focus()

        def action_clear_search(self) -> None:
            box = self.query_one("#search", Input)
            box.value = ""
            box.display = False
            self.search_text = ""
            self.query_one("#table", DataTable).focus()
            self.refresh_view()

        def on_input_changed(self, event: Input.Changed) -> None:
            self.search_text = event.value
            self.refresh_view()

        def on_input_submitted(self, event: Input.Submitted) -> None:
            event.input.display = False
            self.query_one("#table", DataTable).focus()

        def _retry(self, targets: list[Job]) -> None:
            n = 0
            for j in targets:
                if j.stage in ("failed", "stopped"):
                    note = f"retry requested: {j.error[:80]}" if j.error else "retry requested"
                    n += engine.submit(j, only_if_problem=True, note=note)
            if n:
                self.notify(f"Retrying {n} episode(s)")
            else:
                self.notify("Nothing to retry — select a failed or stopped episode (press 4 to list them).", severity="warning")
            self.refresh_view()

        def action_retry_selected(self) -> None:
            sel = self._selected()
            self._retry([sel] if sel else [])

        def action_retry_all(self) -> None:
            self._retry(jobs)

        def action_workers(self, kind: str, delta: int) -> None:
            n = engine.resize(kind, delta)
            low, high = engine.LIMITS[kind]
            name = "Downloads" if kind == "dl" else "Converts"
            edge = " (minimum)" if n == low and delta < 0 else " (maximum)" if n == high and delta > 0 else ""
            self.notify(f"{name} at once: {n}{edge}")
            self.refresh_view()

        def action_toggle_pause(self) -> None:
            engine.paused = not engine.paused
            self.notify("Paused — no new downloads will start" if engine.paused else "Resumed")
            self.refresh_view()

        def action_request_quit(self) -> None:
            if engine.busy:
                self.push_screen(ConfirmQuit(), lambda yes: self.exit() if yes else None)
            else:
                self.exit()

    return DownloadApp()


def write_report(channel_dir: Path, jobs: list[Job]) -> Path:
    path = channel_dir / "last-run-report.tsv"
    with path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter="\t")
        w.writerow(["book", "episode", "title", "status", "failed_stage", "error", "url"])
        for j in jobs:
            w.writerow([j.book, f"{j.index:03d}", j.title, j.stage, j.failed_stage, j.error, j.url])
    return path


def list_playlist_entries(yt_dlp: str, playlist: dict, cookies: list[str]) -> list[dict]:
    cmd = [yt_dlp, *cookies, "--flat-playlist", "--dump-single-json", "--skip-download", playlist["url"]]
    result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"could not list '{playlist['title']}': {(result.stderr or result.stdout).strip()[-300:]}")
    data = json.loads(result.stdout)
    entries = []
    for i, entry in enumerate(data.get("entries") or [], 1):
        if entry and entry.get("id"):
            entries.append(
                {
                    "index": i,
                    "id": entry["id"],
                    "title": entry.get("title") or entry["id"],
                    "duration": entry.get("duration"),
                    "url": f"https://www.youtube.com/watch?v={entry['id']}",
                }
            )
    return entries


def archived_ids(book_dir: Path) -> set[str]:
    path = book_dir / ".download-archive.txt"
    if not path.exists():
        return set()
    return {
        line.split(maxsplit=1)[1].strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.startswith("youtube ")
    }


LEFTOVER_SUFFIXES = {".part", ".ytdl", ".webm", ".jpg", ".webp", ".png", ".opus"}


def audio_complete(ffprobe: str, path: Path, expected: float | None) -> bool:
    """A finished chapter must be readable and about as long as the video (not a cut-off conversion)."""
    r = subprocess.run(
        [ffprobe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        duration = float(r.stdout.strip())
    except ValueError:
        return False
    return duration > 0 and (not expected or duration >= expected * 0.97)


def build_jobs(
    yt_dlp: str,
    playlists: list[dict],
    channel_dir: Path,
    cookies: list[str],
    max_downloads: int | None,
    clean: bool = True,
) -> list[Job]:
    jobs: list[Job] = []
    for pl in playlists:
        book = sanitize_name(pl["title"])
        book_dir = channel_dir / book
        book_dir.mkdir(parents=True, exist_ok=True)
        try:
            entries = list_playlist_entries(yt_dlp, pl, cookies)
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: {exc}", file=sys.stderr)
            continue
        total = len(entries)
        if max_downloads is not None:
            entries = entries[:max_downloads]
        have = archived_ids(book_dir)
        ffprobe = shutil.which("ffprobe") or "ffprobe"
        todo = cleaned = 0
        for e in entries:
            prefix = f"{e['index']:03d} - "
            candidates = [p for p in book_dir.glob(f"{prefix}*.m4[ab]") if ".temp." not in p.name]
            if e["id"] in have:
                done_file = candidates[0] if candidates else None
                is_done = True
            else:
                done_file = next((p for p in candidates if audio_complete(ffprobe, p, e.get("duration"))), None)
                is_done = done_file is not None
            if is_done and done_file:
                junk = [
                    p
                    for p in book_dir.glob(f"{prefix}*")
                    if p.is_file() and p != done_file and (p.suffix.lower() in LEFTOVER_SUFFIXES or ".temp." in p.name)
                ]
            elif not is_done:
                junk = [p for p in book_dir.glob(f"{prefix}*") if p.is_file()]  # cut-off / partial files
            else:
                junk = []
            for p in junk:
                if clean:
                    p.unlink(missing_ok=True)
            cleaned += len(junk)
            job = Job(
                book=book,
                book_dir=book_dir,
                playlist_title=pl["title"],
                index=e["index"],
                total=total,
                vid=e["id"],
                title=e["title"],
                url=e["url"],
            )
            if is_done:
                job.stage, job.pct = "done", 100.0
                job.size = done_file.stat().st_size if done_file else None
                job.note = "already downloaded"
            else:
                todo += 1
                if junk:
                    job.note = "incomplete file found, re-downloading"
            jobs.append(job)
        extra = f", {'removed' if clean else 'would remove'} {cleaned} partial/leftover file(s)" if cleaned else ""
        print(f"Book: {pl['title']} — {total} episode(s), {todo} to download{extra}  ({book_dir})")
    return jobs


def print_summary(jobs: list[Job], interrupted: bool) -> None:
    done = [j for j in jobs if j.stage == "done"]
    failed = [j for j in jobs if j.stage == "failed"]
    stopped = [j for j in jobs if j.stage == "stopped"]
    left = [j for j in jobs if j.stage not in ("done", "failed")]
    print(f"\nFinished: {len(done)} done, {len(failed)} failed, {len(left)} not completed (of {len(jobs)}).")
    for j in stopped:
        print(f"  ■ {j.book} #{j.index:03d} {j.title}\n      interrupted mid-{j.failed_stage or 'transfer'} — will resume next run")
    for j in failed:
        kind = "unavailable" if j.permanent else f"failed while {j.failed_stage}"
        print(f"  ✗ {j.book} #{j.index:03d} {j.title}\n      {kind}: {j.error}")
    if interrupted or failed or left:
        print("Re-run the same command to resume — finished episodes are skipped, the rest are retried.")


def ensure_cache(yt_dlp: str, force_refresh: bool = False) -> tuple[Path, dict]:
    existing = find_existing_channel_dir()
    if existing and not force_refresh:
        cache = load_cache(existing)
        if cache and cache.get("playlists"):
            return existing, cache

    channel_meta, playlists = fetch_playlists(yt_dlp)
    channel_dir = channel_dir_from_meta(channel_meta)
    save_cache(channel_dir, channel_meta, playlists)
    cache = load_cache(channel_dir)
    assert cache
    return channel_dir, cache


def cmd_refresh(yt_dlp: str) -> None:
    channel_meta, playlists = fetch_playlists(yt_dlp)
    channel_dir = channel_dir_from_meta(channel_meta)
    save_cache(channel_dir, channel_meta, playlists)
    print_playlists(playlists, channel_meta["channel"])


def cmd_list(yt_dlp: str) -> None:
    channel_dir, cache = ensure_cache(yt_dlp, force_refresh=False)
    channel_name = (cache.get("channel") or {}).get("channel") or channel_dir.name
    print_playlists(cache["playlists"], channel_name, cache.get("fetched_at"))
    print(f"Cache: {cache_path(channel_dir)}")
    print("Run `refresh` to update the list from YouTube.")


def cmd_download(
    yt_dlp: str,
    selection: str,
    *,
    max_downloads: int | None = None,
    sleep_interval: float = 1.0,
    cookies_from_browser: str | None = None,
    cookies_file: Path | None = None,
    dry_run: bool = False,
    jobs: int = 1,
    convert_jobs: int = 5,
    attempts: int = 3,
    retry_rounds: int = 1,
) -> None:
    channel_dir, cache = ensure_cache(yt_dlp, force_refresh=False)
    playlists = cache["playlists"]
    channel_name = (cache.get("channel") or {}).get("channel") or channel_dir.name
    artist = (cache.get("channel") or {}).get("uploader") or channel_name

    try:
        indices = parse_selection(selection, len(playlists))
    except ValueError:
        die(f"Invalid selection: {selection!r}")

    if indices is None:
        print("Cancelled.")
        return
    if not indices:
        die("No valid playlist numbers selected.")

    chosen = [playlists[i - 1] for i in indices]
    cookies = cookie_args(cookies_from_browser, cookies_file)
    print(f"Scanning {len(chosen)} playlist(s)…")
    all_jobs = build_jobs(yt_dlp, chosen, channel_dir, cookies, max_downloads, clean=not dry_run)
    todo = [j for j in all_jobs if j.stage != "done"]
    if dry_run:
        if not todo:
            print("\n[dry-run] nothing to download — everything is already done.")
            return
        print(f"\n[dry-run] would download {len(todo)} episode(s) with {jobs} download + {convert_jobs} convert workers:")
        for j in todo:
            print(f"  {j.book} #{j.index:03d} {j.title}")
        return
    if not all_jobs:
        print("\nNo episodes found for the selected playlist(s).")
        return

    use_tui = sys.stdout.isatty() and have_textual()
    if sys.stdout.isatty() and not use_tui:
        print("Tip: `pip install textual` for the full-window interface. Using plain log output instead.")
    if not todo and not use_tui:
        print("\nNothing to download — everything is already done.")
        return

    ctx = Context(
        yt_dlp=yt_dlp,
        ffmpeg=shutil.which("ffmpeg") or "ffmpeg",
        ffprobe=shutil.which("ffprobe") or "ffprobe",
        cookie_args=cookies,
        sleep_interval=sleep_interval,
        attempts=max(attempts, 1),
        rep=Reporter(all_jobs, plain=not use_tui),
    )
    engine = Engine(ctx, artist, jobs, convert_jobs)
    runner_error: list[BaseException] = []

    def runner() -> None:
        try:
            engine.start()
            for j in todo:
                engine.submit(j)
            rounds_left = retry_rounds
            while not ctx.stop.is_set():
                if engine.busy:
                    ctx.stop.wait(0.3)
                    continue
                failed = [j for j in all_jobs if j.stage == "failed" and not j.permanent]
                if failed and rounds_left > 0:
                    rounds_left -= 1
                    ctx.rep.status_line = f"{len(failed)} failed — retrying automatically in 15s"
                    if ctx.stop.wait(15):
                        return
                    ctx.rep.status_line = ""
                    for j in failed:
                        engine.submit(j, only_if_problem=True, note=f"auto-retry: {j.error[:80]}")
                    continue
                return
        except BaseException as exc:  # noqa: BLE001
            runner_error.append(exc)

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    interrupted = False
    try:
        if use_tui:
            make_app(ctx, engine, all_jobs, channel_name).run()  # stays open after the run finishes, until q
        else:
            print(f"\nDownloading {len(todo)} episode(s): {jobs} download(s) + {convert_jobs} conversion(s) in parallel. Ctrl+C to stop (resumable).\n")
            while thread.is_alive():
                thread.join(0.2)
    except KeyboardInterrupt:
        pass
    interrupted = engine.busy
    ctx.stop.set()
    ctx.kill_all()
    thread.join(5)
    for j in todo:
        if j.stage in ACTIVE_STAGES or j.stage == "to convert":
            j.failed_stage = "download" if j.stage == "downloading" else "conversion"
            ctx.rep.stage(j, "stopped", "interrupted — resumes next run")
    engine.shutdown()
    if runner_error:
        print(f"ERROR: {runner_error[0]!r}", file=sys.stderr)

    if todo:
        print_summary(todo, interrupted)
    else:
        print(f"\nAll {len(all_jobs)} episode(s) were already downloaded.")
    print(f"Per-episode report: {write_report(channel_dir, all_jobs)}")
    print(f"\nBooks are under:\n  {channel_dir}")
    print("In Apple Books: add the playlist folder — episodes should appear as chapters of one book.")


def pool_kwargs(args: argparse.Namespace) -> dict:
    return {
        "jobs": max(args.jobs, 1),
        "convert_jobs": max(args.convert_jobs, 1),
        "attempts": args.attempts,
        "retry_rounds": max(args.retry_rounds, 0),
    }


def cmd_interactive(yt_dlp: str, args: argparse.Namespace) -> None:
    channel_dir, cache = ensure_cache(yt_dlp, force_refresh=False)
    playlists = cache["playlists"]
    channel_name = (cache.get("channel") or {}).get("channel") or channel_dir.name

    while True:
        print_playlists(playlists, channel_name, cache.get("fetched_at"))
        print("Enter playlist number(s):  1   or  1,3,8   or  2-5")
        print("Or:  all  |  refresh  |  q")
        try:
            choice = input("> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return

        if not choice:
            continue
        if choice.lower() in {"q", "quit", "exit"}:
            return
        if choice.lower() in {"refresh", "r"}:
            cmd_refresh(yt_dlp)
            channel_dir, cache = ensure_cache(yt_dlp, force_refresh=False)
            playlists = cache["playlists"]
            channel_name = (cache.get("channel") or {}).get("channel") or channel_dir.name
            continue

        cmd_download(
            yt_dlp,
            choice,
            max_downloads=args.max_downloads,
            sleep_interval=args.sleep,
            cookies_from_browser=args.cookies_from_browser,
            cookies_file=args.cookies,
            dry_run=args.dry_run,
            **pool_kwargs(args),
        )
        return


def cmd_test(yt_dlp: str, args: argparse.Namespace) -> None:
    """Smoke test: refresh if needed, download 1 episode from playlist #1."""
    print("=== Smoke test (1 episode only) ===\n")
    channel_dir, cache = ensure_cache(yt_dlp, force_refresh=False)
    if not cache.get("playlists"):
        die("Cache empty — run refresh first.")
    pl = cache["playlists"][0]
    print(f"Using playlist #1: {pl['title']}")
    cmd_download(
        yt_dlp,
        "1",
        max_downloads=1,
        sleep_interval=args.sleep,
        cookies_from_browser=args.cookies_from_browser,
        cookies_file=args.cookies,
        dry_run=args.dry_run,
        **pool_kwargs(args),
    )
    book_dir = channel_dir / sanitize_name(pl["title"])
    chapters = sorted(book_dir.glob("*.m4b")) + sorted(book_dir.glob("*.m4a"))
    print("\n=== Test result ===")
    print(f"Folder: {book_dir}")
    for c in chapters[:5]:
        print(f"  chapter: {c.name}")
    if chapters:
        print("OK — open this folder in Apple Books to verify book/chapter grouping.")
    else:
        print("No chapter files found — check errors above.")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Download YouTube channel playlists as Apple Books audiobooks (playlist = book, episode = chapter)."
    )
    p.add_argument(
        "command",
        nargs="?",
        default="menu",
        choices=["menu", "refresh", "list", "download", "test"],
        help="menu (default) | refresh | list | download | test",
    )
    p.add_argument(
        "selection",
        nargs="*",
        help="For download: playlist numbers, ranges, or 'all'",
    )
    p.add_argument("--sleep", type=float, default=1.0)
    p.add_argument("--max-downloads", type=int, default=None, help="Limit episodes per playlist")
    p.add_argument("--cookies-from-browser", default=None, metavar="BROWSER", help="chrome | firefox | safari | brave | zen")
    p.add_argument("--cookies", type=Path, default=None)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--jobs", type=int, default=1, help="Parallel downloads (default 1: safest against YouTube bot checks)")
    p.add_argument("--convert-jobs", type=int, default=5, help="Parallel audio conversions (default 5)")
    p.add_argument("--attempts", type=int, default=3, help="Base tries per stage (network errors get 2 more, conversions 2, bot checks use long waits)")
    p.add_argument("--retry-rounds", type=int, default=1, help="Extra full passes over failed episodes")
    return p


def main() -> None:
    args = build_parser().parse_args()
    yt_dlp = require_tools()
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)

    if args.command == "refresh":
        cmd_refresh(yt_dlp)
    elif args.command == "list":
        cmd_list(yt_dlp)
    elif args.command == "download":
        if not args.selection:
            die("Usage: download <numbers|all>   e.g. download 1 3-5   or   download all")
        cmd_download(
            yt_dlp,
            " ".join(args.selection),
            max_downloads=args.max_downloads,
            sleep_interval=args.sleep,
            cookies_from_browser=args.cookies_from_browser,
            cookies_file=args.cookies,
            dry_run=args.dry_run,
            **pool_kwargs(args),
        )
    elif args.command == "test":
        cmd_test(yt_dlp, args)
    else:
        cmd_interactive(yt_dlp, args)


if __name__ == "__main__":
    main()
