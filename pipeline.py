#!/usr/bin/env python3
"""
Daily X Spaces Pipeline
=======================
Downloads the latest X Space for a given account, transcribes it,
and summarizes the host's contributions using Claude.

Usage:
    python pipeline.py [options]

Options:
    --url URL            Space URL to process (overrides auto-detection)
    --account HANDLE     Twitter handle to watch (default: stocktalkweekly)
    --speaker HANDLE     Speaker to focus summary on (default: same as --account)
    --model MODEL        Whisper model: tiny/base/small/medium (default: base)
    --output-dir DIR     Output directory (default: ./output)
    --cookies-from-browser BROWSER  Browser for cookies: chrome/firefox/safari
    --cookies-file FILE   Netscape cookies.txt for x.com (preferred — see README).
                          Defaults to ./cookies.txt or $COOKIES_FILE if present.
    --keep-audio         Keep the downloaded audio (default: delete after transcribing)
    --skip-if-exists     Skip if today's output already exists

Environment:
    ANTHROPIC_API_KEY    Required for summarization
    HF_TOKEN             Required for speaker diarization (optional feature)
    SPACE_URL            Can set the Space URL via env var (useful for cron)
    COOKIES_FILE         Path to a Netscape cookies.txt for x.com (see README)

Running daily via cron (example — runs at 9am):
    0 9 * * * cd /path/to/x_spaces_transcriber && SPACE_URL=https://x.com/i/spaces/... ANTHROPIC_API_KEY=sk-... python pipeline.py >> logs/pipeline.log 2>&1

Or with launchd on macOS — see README for setup.
"""

import argparse
import fcntl
import os
import signal
import subprocess
import sys
import json
import re
import shutil
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional

# Load .env file if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

# Homebrew and other common bin dirs that launchd does NOT put on PATH. Under
# launchd the job only gets PATH=/usr/bin:/bin:/usr/sbin:/sbin, so yt-dlp fails
# with "m3u8 download detected but ffmpeg could not be found" even though
# ffmpeg is installed. Resolve it once here, for every entry point.
_EXTRA_BIN_DIRS = ("/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin")


def find_ffmpeg() -> Optional[str]:
    """Absolute path to the ffmpeg binary, or None if it really isn't installed."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    for _d in _EXTRA_BIN_DIRS:
        candidate = os.path.join(_d, "ffmpeg")
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def ensure_ffmpeg_on_path() -> Optional[str]:
    """Put ffmpeg's directory on PATH so yt-dlp and ffmpeg subprocesses find it.

    Returns the directory containing ffmpeg, or None if ffmpeg is missing.
    """
    ffmpeg = find_ffmpeg()
    if not ffmpeg:
        return None
    bin_dir = os.path.dirname(ffmpeg)
    current = os.environ.get("PATH", "")
    if bin_dir not in current.split(os.pathsep):
        os.environ["PATH"] = os.pathsep.join([bin_dir, current]) if current else bin_dir
    return bin_dir


FFMPEG_DIR = ensure_ffmpeg_on_path()

# ── Helpers ──────────────────────────────────────────────────────────────────

def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def extract_space_id(url: str) -> str:
    match = re.search(r"/spaces/([A-Za-z0-9]+)", url)
    if match:
        return match.group(1)
    return re.sub(r"[^A-Za-z0-9_-]", "_", url)[:40]


def extract_space_name(url: str) -> Optional[str]:
    """Extract account name from URLs like x.com/<account>/spaces/... or x.com/i/spaces/..."""
    match = re.search(r"x\.com/([^/]+)/spaces/", url)
    if match and match.group(1) != "i":
        return match.group(1).lower()
    return None


def make_file_stem(url: str, account: str, recorded_date: str = None) -> str:
    """Return <space_name>-<YYYY-MM-DD> for use as output filename base.

    `recorded_date` is when the Space was broadcast; today's date is only a
    fallback for when that lookup failed. Naming by the processing date is
    wrong the moment a run is late — a catch-up filed a 09-02 Space under
    09-03, and because the stem is also the reuse key for the audio and
    transcript, each retry landed on a fresh name and redid a 3-hour
    transcription it already had on disk.
    """
    name = extract_space_name(url) or account.lower()
    date = recorded_date or datetime.now().strftime("%Y-%m-%d")
    return f"{name}-{date}"


# ── Live vs. replay ───────────────────────────────────────────────────────────

# yt-dlp maps a Space's `state` onto exactly four statuses:
#
#   is_upcoming  scheduled, not started
#   is_live      running RIGHT NOW
#   post_live    ended, replay not published yet
#   was_live     ended, full replay available  ← the only one worth downloading
#
# Downloading a running Space does not fail, and that is what made this
# expensive. yt-dlp attaches to the live edge and follows the rolling playlist
# in real time, so what lands on disk is whatever it managed to catch rather
# than the Space: on 2026-09-08/09/10 that was 5.9%, 15.8% and 3.8% of three
# ~2.5-hour Spaces, each transcript opening mid-sentence. Every other signal —
# exit status, run record, the email itself — said "success", so three days of
# summaries went out built on a fraction of the conversation.
#
# The replay is also strictly cheaper: an ended Space pulls a finite playlist
# at ~870 KiB/s in two or three minutes, where following one live costs two
# hours of wall clock to capture less.
SPACE_READY = "was_live"

SPACE_STATUS_REASON = {
    "is_upcoming": "has not started yet",
    "is_live": "is still live — only the replay is downloadable in full",
    "post_live": "has ended but its replay is not published yet",
}


class SpaceNotReady(RuntimeError):
    """The Space cannot be downloaded in full yet. Try again on a later run.

    Distinct from a failed download: nothing is wrong, the Space simply is not
    finished. Callers must NOT record it as processed, or the pipeline will
    skip the replay forever once it appears.
    """


def fetch_space_meta(url: str, cookies_from_browser: str = None,
                     cookies_file: str = None) -> dict:
    """Metadata-only probe: when the Space aired, and whether it has ended.

    Returns {"recorded_date": YYYY-MM-DD or None, "live_status": str or None}.
    One yt-dlp call answers both, so gating on live status costs no extra
    round-trip over the date lookup the catch-up loop already did.

    Never raises — an unreachable Space must fail only itself, not the whole
    catch-up run. `live_status` is None when the probe failed or yt-dlp did not
    report one; callers treat that as "unknown" and fall through to the
    download, which will surface the real error.
    """
    import yt_dlp

    ydl_opts = {"quiet": True, "skip_download": True}
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    elif cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    meta = {"recorded_date": None, "live_status": None}
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        log(f"Could not fetch metadata for {url}: {e}")
        return meta

    meta["live_status"] = info.get("live_status")

    ts = info.get("release_timestamp") or info.get("timestamp")
    if ts:
        meta["recorded_date"] = datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    else:
        # Date-only fallbacks, already YYYYMMDD strings
        for key in ("release_date", "upload_date"):
            raw = info.get(key)
            if raw and len(raw) == 8:
                meta["recorded_date"] = f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
                break
    return meta


def space_not_ready_reason(live_status: Optional[str]) -> Optional[str]:
    """Why this Space must not be downloaded yet, or None if it is ready.

    Unknown status (None) is deliberately treated as ready: a metadata blip
    should not stall an ended Space indefinitely, and the download itself
    fails loudly if the Space really is unavailable.
    """
    if live_status is None or live_status == SPACE_READY:
        return None
    return SPACE_STATUS_REASON.get(live_status, f"is not ready (state: {live_status})")


def fetch_space_recorded_date(url: str, cookies_from_browser: str = None,
                               cookies_file: str = None) -> Optional[str]:
    """Return the date the Space was actually broadcast, as YYYY-MM-DD (local time).

    Uses yt-dlp's metadata-only extraction: `release_timestamp` is the Space's
    `started_at` (falling back to `scheduled_start`), i.e. when the recording
    happened — not when we downloaded it. `timestamp` (`created_at`, when the
    Space was first scheduled) is the last resort; it can be days earlier.

    Returns None if metadata can't be fetched, so callers can fall back to the
    processing date.

    Thin wrapper over fetch_space_meta() for callers that only need the date.
    """
    return fetch_space_meta(url, cookies_from_browser, cookies_file)["recorded_date"]


def save_run_record(output_dir: Path, space_id: str, meta: dict):
    """Save a JSON record of this run for deduplication and history."""
    record_path = output_dir / f"{space_id}_run.json"
    meta["space_id"] = space_id
    meta["timestamp"] = datetime.now().isoformat()
    record_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# Extensions step_download can produce. Kept in one place so the cleanup sweep
# and the "already downloaded" check below can never drift apart.
AUDIO_EXTS = (".m4a", ".mp3", ".aac", ".opus", ".webm", ".mp4", ".wav")


def discard_audio(audio_path: Path, transcript_path: Path, keep: bool = False) -> int:
    """Delete the downloaded audio once its transcript exists. Returns bytes freed.

    A Space is 60-100 MB of m4a that is never read again: step_transcribe skips
    when the .txt is already there, and check_and_run skips the episode outright
    once it lands in state. Keeping the audio cost 2.9 GB before this existed.

    The transcript check is the safety interlock — without a non-empty .txt on
    disk the audio is still the only copy of the episode, so it stays. Failures
    here are logged and swallowed: losing a cleanup must never fail a run that
    already produced a summary.
    """
    if keep:
        return 0
    try:
        if not audio_path or not Path(audio_path).exists():
            return 0
        if not transcript_path or not Path(transcript_path).exists():
            log(f"Keeping {Path(audio_path).name} — no transcript to replace it")
            return 0
        if Path(transcript_path).stat().st_size == 0:
            log(f"Keeping {Path(audio_path).name} — transcript is empty")
            return 0
        size = Path(audio_path).stat().st_size
        Path(audio_path).unlink()
        log(f"Removed {Path(audio_path).name} ({size / 1e6:.0f} MB) — transcript kept")
        return size
    except Exception as e:
        log(f"Could not remove {audio_path}: {e}")
        return 0


@contextmanager
def state_lock(output_dir: Path, label: str = "run"):
    """Serialize the runners that share output/state.json.

    check_and_run.py and zoom_ingest.py both read-modify-write the same state
    file. That was safe while Zoom ingest was manual, but both now run on
    5-minute launchd timers and a Playwright fetch plus a Claude summarization
    comfortably outlasts the interval. Two overlapping runs would read the same
    state, write it back independently, and silently drop whichever entry lost
    the race — surfacing later as a duplicate email or a re-summarized episode.

    Non-blocking on purpose: if another run holds the lock there is nothing
    useful to wait for, the next timer tick will pick the work up.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    lock_path = output_dir / "state.lock"
    # "a+", never "w": open(..., "w") truncates *before* flock, so the run that
    # FAILS to acquire erases the holder's PID. A 31-hour hang once left an
    # empty lock file and no way to tell from disk which process to look at.
    handle = open(lock_path, "a+")
    try:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            holder = ""
            try:
                handle.seek(0)
                holder = handle.read().strip()
            except Exception:
                pass
            owner = f" (held by PID {holder})" if holder else ""
            log(f"Another run holds {lock_path.name}{owner} — skipping this {label}")
            yield False
            return
        # Only now that the lock is ours is it safe to rewrite the file.
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(str(os.getpid()))
            handle.flush()
        except Exception:
            pass
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
    finally:
        handle.close()


# ── Download stall watchdog ───────────────────────────────────────────────────

# A Space whose m3u8 is still `type=live` makes ffmpeg poll a playlist that
# never ends. One did exactly that: it wrote 11 MB, then sat with the
# connection open — and the state lock held — for 31 hours, freezing both
# scheduled runners until it was killed by hand.
#
# A wall-clock cap is the wrong instrument: a legitimate Space once took
# 2h19m at 10.8 KiB/s, and that download was healthy. What separates "slow"
# from "wedged" is whether bytes are still landing on disk, so that is what
# this watches.
DOWNLOAD_STALL_SECONDS = int(os.environ.get("DOWNLOAD_STALL_SECONDS", 20 * 60))


def _download_bytes(output_dir: Path, file_stem: str) -> int:
    """Bytes on disk for this download so far, including the .part file."""
    total = 0
    for f in Path(output_dir).glob(f"{file_stem}.*"):
        try:
            total += f.stat().st_size
        except OSError:
            pass
    return total


def _kill_child_processes():
    """SIGKILL this process's direct children — i.e. yt-dlp's ffmpeg.

    Killing the child is what actually unblocks things: yt-dlp is sitting in
    wait(), so once ffmpeg dies the wait returns and the error propagates
    normally as a failed download instead of hanging forever.
    """
    try:
        found = subprocess.run(["pgrep", "-P", str(os.getpid())],
                               capture_output=True, text=True, timeout=10)
    except Exception:
        return []
    killed = []
    for pid in found.stdout.split():
        try:
            os.kill(int(pid), signal.SIGKILL)
            killed.append(int(pid))
        except (ProcessLookupError, ValueError, PermissionError):
            pass
    return killed


@contextmanager
def stall_watchdog(output_dir: Path, file_stem: str,
                   stall_seconds: int = None, poll_seconds: int = 30):
    """Kill the downloader if it stops writing bytes for `stall_seconds`."""
    stall_seconds = DOWNLOAD_STALL_SECONDS if stall_seconds is None else stall_seconds
    stop = threading.Event()
    fired = {"stalled": False}

    def watch():
        last_size = _download_bytes(output_dir, file_stem)
        last_change = time.monotonic()
        while not stop.wait(poll_seconds):
            size = _download_bytes(output_dir, file_stem)
            if size != last_size:
                last_size, last_change = size, time.monotonic()
                continue
            if time.monotonic() - last_change >= stall_seconds:
                fired["stalled"] = True
                log(f"Download stalled at {size} bytes for "
                    f"{stall_seconds // 60} min — killing the downloader")
                _kill_child_processes()
                return

    thread = threading.Thread(target=watch, daemon=True, name="stall-watchdog")
    thread.start()
    try:
        yield fired
    finally:
        stop.set()
        thread.join(timeout=5)


# ── Pipeline steps ────────────────────────────────────────────────────────────

def step_download(url: str, output_dir: Path, file_stem: str, cookies_from_browser: str = None,
                   cookies_file: str = None, live_status: str = "unchecked") -> Path:
    import yt_dlp

    candidates = list(output_dir.glob(f"{file_stem}.*"))
    existing = [f for f in candidates if f.suffix in AUDIO_EXTS]
    if existing:
        log(f"Audio already exists: {existing[0]} — skipping download")
        return existing[0]

    # Never follow a Space that is still running: that captures the tail, not
    # the Space. Callers that already probed pass their live_status through;
    # the sentinel means nobody checked, so check here rather than trust it.
    if live_status == "unchecked":
        live_status = fetch_space_meta(url, cookies_from_browser, cookies_file)["live_status"]
    not_ready = space_not_ready_reason(live_status)
    if not_ready:
        raise SpaceNotReady(f"Space {not_ready} — deferring until the replay is available")

    if not FFMPEG_DIR:
        raise RuntimeError(
            "ffmpeg not found — Spaces are m3u8 streams and cannot be downloaded "
            "without it. Install with: brew install ffmpeg"
        )

    log(f"Downloading Space ({file_stem})...")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / f"{file_stem}.%(ext)s"),
        "quiet": True,
        # Explicit, so the download does not depend on the inherited PATH.
        "ffmpeg_location": FFMPEG_DIR,
        # Bounds yt-dlp's own HTTP reads. It does NOT bound the ffmpeg
        # subprocess that pulls an m3u8 — stall_watchdog below covers that.
        "socket_timeout": 60,
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    elif cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    with stall_watchdog(output_dir, file_stem) as watch:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            ext = info.get("ext", "m4a")
    if watch["stalled"]:
        raise RuntimeError(
            f"Download stalled for {DOWNLOAD_STALL_SECONDS // 60} min and was "
            f"killed — the Space's m3u8 is most likely still type=live")

    audio_path = output_dir / f"{file_stem}.{ext}"
    log(f"Downloaded: {audio_path}")
    return audio_path


# Whisper sizes → MLX model repos. mlx-whisper runs on the GPU and Neural Engine
# via Metal; faster-whisper is built on CTranslate2, which has no Metal backend and
# is CPU-only on Apple Silicon no matter what device you ask for. On this hardware
# that made `large-v3` impractical, which is why the old default was `base` — the
# model that most often mishears tickers ("NVDA" as "in video").
_MLX_MODELS = {
    "tiny":     "mlx-community/whisper-tiny",
    "base":     "mlx-community/whisper-base",
    "small":    "mlx-community/whisper-small",
    "medium":   "mlx-community/whisper-medium",
    "large":    "mlx-community/whisper-large-v3-mlx",
    "large-v3": "mlx-community/whisper-large-v3-mlx",
    # ~2x faster than large-v3 at slightly lower accuracy.
    "turbo":    "mlx-community/whisper-large-v3-turbo",
}


# Deliberately NOT seeding Whisper's `initial_prompt` with the watchlist.
#
# It looks like an obvious win — tell the decoder which ticker symbols to expect
# — and it was in this pipeline briefly. Measured on real audio, it made things
# worse: Whisper treats initial_prompt as text that *preceded* the audio, and
# large-v3 responded by skipping ahead. On a 90-second clip it dropped a third of
# the words, including the very sentence the prompt was meant to help
# ("...NVIDIA I made ten dollars..." vanished entirely). On a full recording it
# swallowed the opening line, price levels and all.
#
# large-v3 transcribes these tickers correctly on its own, so there was nothing
# to buy in the first place. If you reintroduce this, measure content loss at the
# start of the file before trusting it.


def step_transcribe(audio_path: Path, output_dir: Path, file_stem: str,
                    model_size: str = "large-v3") -> Path:
    transcript_path = output_dir / f"{file_stem}.txt"
    if transcript_path.exists():
        log(f"Transcript already exists: {transcript_path} — skipping transcription")
        return transcript_path

    try:
        import mlx_whisper
    except ImportError:
        mlx_whisper = None

    if mlx_whisper is not None:
        # A full HF repo path passes through untouched; a bare size is mapped.
        repo = model_size if "/" in model_size else _MLX_MODELS.get(model_size)
        if repo is None:
            raise ValueError(
                f"Unknown Whisper model '{model_size}' — use one of "
                f"{', '.join(_MLX_MODELS)} or a full Hugging Face repo path")
        log(f"Transcribing audio with mlx-whisper (model={repo})...")
        result = mlx_whisper.transcribe(str(audio_path), path_or_hf_repo=repo)
        log(f"Detected language: {result.get('language')}")
        lines = [f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text'].strip()}"
                 for s in result.get("segments", [])]
    else:
        # Non-Apple-Silicon (or mlx not installed): CPU via faster-whisper.
        from faster_whisper import WhisperModel
        fallback = model_size if model_size in ("tiny", "base", "small", "medium", "large") else "base"
        log(f"mlx-whisper unavailable — falling back to faster-whisper on CPU (model={fallback})")
        model = WhisperModel(fallback, device="cpu", compute_type="int8")
        segments, info = model.transcribe(str(audio_path), beam_size=5)
        log(f"Detected language: {info.language}")
        lines = [f"[{seg.start:.1f}s - {seg.end:.1f}s] {seg.text.strip()}" for seg in segments]

    transcript_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Transcript saved: {transcript_path} ({len(lines)} segments)")
    return transcript_path


def step_summarize(transcript_path: Path, output_dir: Path, file_stem: str,
                   speaker: str, space_url: str, model: str = "claude-opus-5") -> Path:
    summary_path = output_dir / f"{file_stem}_summary.md"
    if summary_path.exists():
        log(f"Summary already exists: {summary_path} — skipping summarization")
        return summary_path

    # Import the summarize module from the same directory
    sys.path.insert(0, str(Path(__file__).parent))
    from summarize import summarize
    return summarize(transcript_path, speaker, space_url, summary_path, model)


# ── Main ──────────────────────────────────────────────────────────────────────

def _find_spaces_via_twitter_api(account: str) -> list:
    """Twitter API v2 lookup — requires TWITTER_BEARER_TOKEN in environment."""
    bearer = os.environ.get("TWITTER_BEARER_TOKEN")
    if not bearer:
        return []

    import urllib.request
    import urllib.parse
    import urllib.error

    # .env may store the token URL-encoded (e.g. %2B → +, %3D → =)
    bearer = urllib.parse.unquote(bearer)

    headers = {"Authorization": f"Bearer {bearer}"}

    def _get(path: str, params: dict = None):
        url = "https://api.twitter.com/2" + path
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read()), r.status
        except urllib.error.HTTPError as e:
            log(f"Twitter API {path} → HTTP {e.code}: {e.read().decode(errors='replace')[:160]}")
            return None, e.code
        except Exception as e:
            log(f"Twitter API {path} error: {e}")
            return None, 0

    # Step 1: resolve username → user ID
    data, _ = _get(f"/users/by/username/{account}")
    if not data or "data" not in data:
        log(f"Twitter API: could not look up @{account}")
        return []
    user_id = data["data"]["id"]

    urls = []

    # Step 2: check for live / scheduled Spaces
    data, _ = _get("/spaces/by/creator_ids",
                   {"user_ids": user_id, "space.fields": "state,created_at"})
    if data and data.get("data"):
        # This endpoint returns live AND scheduled Spaces, which is why it asks
        # for `state`: a Space that has not ended has no replay to download, and
        # queueing one means capturing its tail in real time. Step 3 picks it up
        # once it ends.
        skipped = 0
        for space in data["data"]:
            if (space.get("state") or "").lower() != "ended":
                skipped += 1
                continue
            urls.append(f"https://x.com/i/spaces/{space['id']}")
        if skipped:
            log(f"Twitter API: skipped {skipped} live/scheduled Space(s) — not ended yet")
        if urls:
            log(f"Found {len(urls)} ended Space(s) via Twitter API")

    # Step 3: search for recently ended Spaces
    data, _ = _get("/spaces/search", {
        "query": account,
        "state": "ended",
        "max_results": "10",
        "space.fields": "created_at,creator_id",
        "expansions": "creator_id",
    })
    if data and data.get("data"):
        users = {u["id"]: u["username"].lower()
                 for u in (data.get("includes") or {}).get("users") or []}
        for space in data["data"]:
            if users.get(space.get("creator_id"), "").lower() == account.lower():
                url = f"https://x.com/i/spaces/{space['id']}"
                if url not in urls:
                    urls.append(url)

    if urls:
        log(f"Found {len(urls)} Space(s) via Twitter API: {urls}")
    else:
        log(f"Twitter API: no recent Spaces found for @{account}")
    return urls


def _is_x_domain(domain: str) -> bool:
    d = domain.lstrip(".")
    return d in ("twitter.com", "x.com") or d.endswith(".twitter.com") or d.endswith(".x.com")


def _pw_cookies_from_jar(jar, webkit_timestamps: bool = False) -> list:
    """Convert a http.cookiejar-style jar into Playwright's add_cookies() format,
    filtered to Twitter/X domains."""
    # WebKit timestamp epoch offset (microseconds between 1601-01-01 and 1970-01-01) —
    # only relevant for cookies read straight out of Chrome's SQLite store.
    _WEBKIT_OFFSET_US = 11_644_473_600_000_000

    pw_cookies = []
    for c in jar:
        if not _is_x_domain(c.domain):
            continue
        entry: dict = {
            "domain": c.domain,
            "name": c.name,
            "value": c.value,
            "path": c.path,
            "secure": bool(c.secure),
        }
        exp = c.expires
        if exp and exp > 0:
            if webkit_timestamps and exp > 10_000_000_000:
                exp = (exp - _WEBKIT_OFFSET_US) // 1_000_000
            if exp > 0:
                entry["expires"] = exp
        pw_cookies.append(entry)
    return pw_cookies


def _find_spaces_via_playwright(account: str, cookies_file: str = None) -> list:
    """Navigate to the account's /spaces tab using Playwright.

    Cookies come from a Netscape-format cookies.txt export if provided (reliable —
    see README), otherwise fall back to live Chrome decryption via yt-dlp's Python
    API (can silently fail to decrypt sensitive cookies on newer Chrome versions
    even when logged in).
    """
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return []

    if cookies_file and Path(cookies_file).exists():
        import http.cookiejar
        jar = http.cookiejar.MozillaCookieJar(cookies_file)
        try:
            jar.load(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            log(f"Playwright: failed to load cookies file {cookies_file} — {e}")
            return []
        pw_cookies = _pw_cookies_from_jar(jar, webkit_timestamps=False)
        source = f"cookies file ({cookies_file})"
    else:
        # Extract Chrome cookies via yt-dlp's Python API (no subprocess PATH issues)
        try:
            import yt_dlp
            ydl = yt_dlp.YoutubeDL({"cookiesfrombrowser": ("chrome",), "quiet": True})
            jar = ydl.cookiejar
            ydl.__exit__(None, None, None)
        except Exception as e:
            log(f"Playwright: cookie extraction failed — {e}")
            return []
        pw_cookies = _pw_cookies_from_jar(jar, webkit_timestamps=True)
        source = "Chrome"

    if not pw_cookies:
        log(f"Playwright: no Twitter/X cookies found via {source} — log in to x.com first")
        return []

    if not any(c["name"] == "auth_token" for c in pw_cookies):
        log(f"Playwright: only guest/anonymous X cookies found via {source} ({len(pw_cookies)} found, "
            "no auth_token/ct0/twid). If you're logged in to x.com, this is likely Chrome's cookie "
            "encryption blocking automated decryption (e.g. on newer Chrome versions) rather than an "
            "actual logged-out session — a cookies.txt export is more reliable; see README.")
        return []

    # Intercept AudioSpaceById requests — Twitter fires one per Space card in the timeline
    import urllib.parse
    space_ids: list = []

    def _on_request(request):
        if "AudioSpaceById" not in request.url:
            return
        decoded = urllib.parse.unquote(request.url)
        m = re.search(r'"id"\s*:\s*"([A-Za-z0-9]+)"', decoded)
        if m and m.group(1) not in space_ids:
            space_ids.append(m.group(1))

    log(f"Playwright: loaded {len(pw_cookies)} X cookies, loading @{account} profile...")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context()
        ctx.add_cookies(pw_cookies)
        page = ctx.new_page()
        page.on("request", _on_request)
        try:
            page.goto(f"https://x.com/{account}",
                      wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(5000)  # let the SPA render and fire API calls

            if space_ids:
                urls = [f"https://x.com/i/spaces/{sid}" for sid in space_ids]
                log(f"Found {len(urls)} Space(s) via Playwright: {urls}")
                return urls

            log(f"Playwright: no AudioSpaceById calls fired for @{account} — no recent Spaces in timeline")
        except PWTimeout:
            log("Playwright: page timed out")
        except Exception as e:
            log(f"Playwright: error — {e}")
        finally:
            browser.close()

    return []


def _find_spaces_via_ydlp(account: str, cookies_from_browser: str = None, cookies_file: str = None) -> list:
    """Scrape the account's /spaces tab with yt-dlp as a fallback."""
    import yt_dlp

    ydl_opts = {"extract_flat": True, "quiet": True, "playlistend": 10}
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    elif cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    for candidate in [
        f"https://x.com/{account}/spaces",
        f"https://x.com/{account}",
    ]:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(candidate, download=False)
            found = []
            for entry in (info or {}).get("entries") or []:
                for field in ("url", "webpage_url"):
                    m = re.search(r"https?://(?:x|twitter)\.com/i/spaces/([A-Za-z0-9]+)",
                                  entry.get(field) or "")
                    if m:
                        url = f"https://x.com/i/spaces/{m.group(1)}"
                        if url not in found:
                            found.append(url)
                        break
            if found:
                log(f"Found {len(found)} Space(s) via yt-dlp ({candidate}): {found}")
                return found
        except Exception as e:
            log(f"yt-dlp on {candidate}: {e}")

    return []


def fetch_recent_space_urls(account: str, cookies_from_browser: str = None, cookies_file: str = None) -> list:
    """Find recent Spaces from a Twitter/X account, most-recent-first.

    Tries in order, using whichever method returns results first:
      1. Twitter API v2  — set TWITTER_BEARER_TOKEN in .env
         (free app token from developer.twitter.com is sufficient)
      2. Playwright scrape of the profile page (intercepts AudioSpaceById calls) —
         uses cookies_file (a Netscape cookies.txt export) if given, else live
         Chrome cookie decryption
      3. yt-dlp /spaces tab scrape — works when cookies_file or
         --cookies-from-browser is set
    """
    urls = _find_spaces_via_twitter_api(account)
    if urls:
        return urls

    urls = _find_spaces_via_playwright(account, cookies_file)
    if urls:
        return urls

    urls = _find_spaces_via_ydlp(account, cookies_from_browser, cookies_file)
    if urls:
        return urls

    log("Auto-detection could not find any Space URLs.")
    log("  → Ensure you are logged in to x.com (a cookies.txt export is more reliable than live Chrome — see README)")
    if not os.environ.get("TWITTER_BEARER_TOKEN"):
        log("  → Or add TWITTER_BEARER_TOKEN to .env (requires Twitter API Basic plan)")
    log("  → Or pass --url <space_url> directly")
    return []


def fetch_latest_space_url(account: str, cookies_from_browser: str = None, cookies_file: str = None) -> Optional[str]:
    """Find the single most recent Space from a Twitter/X account."""
    urls = fetch_recent_space_urls(account, cookies_from_browser, cookies_file)
    return urls[0] if urls else None


def main():
    parser = argparse.ArgumentParser(description="Daily X Spaces pipeline")
    parser.add_argument("--url", default=os.environ.get("SPACE_URL"),
                        help="Space URL to process (or set SPACE_URL env var)")
    parser.add_argument("--account", default="StocksOnSpaces",
                        help="Twitter account handle to watch (default: StocksOnSpaces)")
    parser.add_argument("--speaker", default=None,
                        help="Speaker handle for summary focus (default: same as --account)")
    parser.add_argument("--model", default="large-v3",
                        help="Whisper model: tiny, base, small, medium, large, large-v3, "
                             "turbo, or a full Hugging Face repo path (default: large-v3). "
                             "Runs on the GPU via mlx-whisper when available.")
    parser.add_argument("--claude-model", default="claude-opus-5",
                        help="Claude model for summarization (default: claude-opus-5)")
    parser.add_argument("--output-dir", default="output",
                        help="Output directory (default: ./output)")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="Load cookies from browser: chrome, firefox, safari")
    _default_cookies_file = os.environ.get("COOKIES_FILE") or str(Path(__file__).parent / "cookies.txt")
    parser.add_argument("--cookies-file", metavar="FILE",
                        default=_default_cookies_file if Path(_default_cookies_file).exists() else None,
                        help="Netscape-format cookies.txt for x.com (preferred over --cookies-from-browser; "
                             "see README). Defaults to ./cookies.txt or $COOKIES_FILE if present.")
    parser.add_argument("--keep-audio", action="store_true",
                        help="Keep the downloaded audio after transcription (default: "
                             "delete it — a Space is 60-100 MB and nothing reads it again)")
    parser.add_argument("--skip-if-exists", action="store_true",
                        help="Skip entire run if today's summary already exists")
    args = parser.parse_args()

    speaker = args.speaker or args.account
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    # Logging goes to stdout (redirect to file in cron)
    log("=" * 60)
    log(f"X Spaces Pipeline starting")
    log(f"Account: @{args.account} | Speaker focus: @{speaker}")

    # Auto-detect latest Space if no URL given
    if not args.url:
        log(f"No URL provided — checking @{args.account} for latest Space...")
        args.url = fetch_latest_space_url(args.account, args.cookies_from_browser, args.cookies_file)

    if not args.url:
        log("No Space URL found. Nothing to process today.")
        sys.exit(0)

    space_id = extract_space_id(args.url)

    # Resolve the broadcast date BEFORE the stem. The stem is derived from it,
    # and is also how step_download/step_transcribe find work they already did.
    # Passing no date at all made make_file_stem fall back to *today*, so this
    # CLI named every Space after the day it was run: re-processing the 09-08
    # Space on 09-10 filed it as `stocksonspaces-2026-09-10` and overwrote that
    # day's transcript and summary. check_and_run.py fixed this in its own loop;
    # the single-Space entry point kept the bug.
    meta = fetch_space_meta(args.url, args.cookies_from_browser, args.cookies_file)
    file_stem = make_file_stem(args.url, args.account, meta["recorded_date"])
    log(f"Space ID: {space_id} | File stem: {file_stem}")
    if meta["recorded_date"]:
        log(f"  recorded {meta['recorded_date']}")
    else:
        log("  broadcast date unknown — falling back to today's date for the stem")

    # Skip if today's run already completed
    if args.skip_if_exists:
        summary_path = output_dir / f"{file_stem}_summary.md"
        if summary_path.exists():
            log(f"Summary already exists, exiting (--skip-if-exists): {summary_path}")
            sys.exit(0)

    try:
        # Step 1: Download
        audio_path = step_download(args.url, output_dir, file_stem, args.cookies_from_browser,
                                   args.cookies_file, live_status=meta["live_status"])

        # Step 2: Transcribe
        transcript_path = step_transcribe(audio_path, output_dir, file_stem, args.model)

        # Step 3: Summarize
        log(f"Summarizing with Claude (focus: @{speaker})...")
        summary_path = step_summarize(transcript_path, output_dir, file_stem, speaker, args.url, args.claude_model)

        # Step 4: Drop the audio — the transcript supersedes it.
        discard_audio(audio_path, transcript_path, keep=args.keep_audio)

        save_run_record(output_dir, space_id, {
            "url": args.url,
            "account": args.account,
            "speaker": speaker,
            "audio": str(audio_path),
            "transcript": str(transcript_path),
            "summary": str(summary_path),
            "status": "success",
        })

        log("=" * 60)
        log("✓ Pipeline complete!")
        if audio_path.exists():
            log(f"  Audio:      {audio_path}")
        log(f"  Transcript: {transcript_path}")
        log(f"  Summary:    {summary_path}")

    except SpaceNotReady as e:
        # Not a failure — exit 0 with no run record so a retry is clean.
        log(f"Not ready: {e}")
        log("Re-run once the Space has ended and its replay is published.")
        sys.exit(0)

    except Exception as e:
        log(f"ERROR: Pipeline failed — {e}")
        save_run_record(output_dir, space_id, {"url": args.url, "status": "failed", "error": str(e)})
        raise


if __name__ == "__main__":
    main()
