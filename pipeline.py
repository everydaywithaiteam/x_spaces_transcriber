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
import os
import sys
import json
import re
import shutil
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


def make_file_stem(url: str, account: str) -> str:
    """Return <space_name>-<YYYY-MM-DD> for use as output filename base."""
    name = extract_space_name(url) or account.lower()
    date = datetime.now().strftime("%Y-%m-%d")
    return f"{name}-{date}"


def fetch_space_recorded_date(url: str, cookies_from_browser: str = None,
                               cookies_file: str = None) -> Optional[str]:
    """Return the date the Space was actually broadcast, as YYYY-MM-DD (local time).

    Uses yt-dlp's metadata-only extraction: `release_timestamp` is the Space's
    `started_at` (falling back to `scheduled_start`), i.e. when the recording
    happened — not when we downloaded it. `timestamp` (`created_at`, when the
    Space was first scheduled) is the last resort; it can be days earlier.

    Returns None if metadata can't be fetched, so callers can fall back to the
    processing date.
    """
    import yt_dlp

    ydl_opts = {"quiet": True, "skip_download": True}
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    elif cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        ts = info.get("release_timestamp") or info.get("timestamp")
        if ts:
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
        # Date-only fallbacks, already YYYYMMDD strings
        for key in ("release_date", "upload_date"):
            raw = info.get(key)
            if raw and len(raw) == 8:
                return f"{raw[0:4]}-{raw[4:6]}-{raw[6:8]}"
    except Exception as e:
        log(f"Could not fetch recording date for {url}: {e}")
    return None


def save_run_record(output_dir: Path, space_id: str, meta: dict):
    """Save a JSON record of this run for deduplication and history."""
    record_path = output_dir / f"{space_id}_run.json"
    meta["space_id"] = space_id
    meta["timestamp"] = datetime.now().isoformat()
    record_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ── Pipeline steps ────────────────────────────────────────────────────────────

def step_download(url: str, output_dir: Path, file_stem: str, cookies_from_browser: str = None,
                   cookies_file: str = None) -> Path:
    import yt_dlp

    candidates = list(output_dir.glob(f"{file_stem}.*"))
    existing = [f for f in candidates if f.suffix in (".m4a", ".mp3", ".aac", ".opus", ".webm", ".mp4")]
    if existing:
        log(f"Audio already exists: {existing[0]} — skipping download")
        return existing[0]

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
    }
    if cookies_file:
        ydl_opts["cookiefile"] = cookies_file
    elif cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        ext = info.get("ext", "m4a")

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


def _ticker_hint_prompt(limit: int = 40) -> Optional[str]:
    """Bias Whisper's decoder toward the tickers you actually care about.

    Whisper accepts an `initial_prompt` as vocabulary context. Seeding it with
    the watchlist makes correct ticker spellings materially more likely. The
    prompt window is small (~224 tokens), so this is capped and degrades to
    None when there's no watchlist.
    """
    try:
        from ticker_alerts import load_watchlist
        tickers = load_watchlist()
    except Exception:
        return None
    if not tickers:
        return None
    return ("This is a stock market discussion. Tickers mentioned may include: "
            + ", ".join(tickers[:limit]) + ".")


def step_transcribe(audio_path: Path, output_dir: Path, file_stem: str,
                    model_size: str = "large-v3") -> Path:
    transcript_path = output_dir / f"{file_stem}.txt"
    if transcript_path.exists():
        log(f"Transcript already exists: {transcript_path} — skipping transcription")
        return transcript_path

    initial_prompt = _ticker_hint_prompt()

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
        if initial_prompt:
            log("  biasing decoder with watchlist tickers")
        result = mlx_whisper.transcribe(
            str(audio_path), path_or_hf_repo=repo, initial_prompt=initial_prompt)
        log(f"Detected language: {result.get('language')}")
        lines = [f"[{s['start']:.1f}s - {s['end']:.1f}s] {s['text'].strip()}"
                 for s in result.get("segments", [])]
    else:
        # Non-Apple-Silicon (or mlx not installed): CPU via faster-whisper.
        from faster_whisper import WhisperModel
        fallback = model_size if model_size in ("tiny", "base", "small", "medium", "large") else "base"
        log(f"mlx-whisper unavailable — falling back to faster-whisper on CPU (model={fallback})")
        model = WhisperModel(fallback, device="cpu", compute_type="int8")
        segments, info = model.transcribe(str(audio_path), beam_size=5,
                                          initial_prompt=initial_prompt)
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
        for space in data["data"]:
            urls.append(f"https://x.com/i/spaces/{space['id']}")
        log(f"Found {len(urls)} live/scheduled Space(s) via Twitter API")

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
    file_stem = make_file_stem(args.url, args.account)
    log(f"Space ID: {space_id} | File stem: {file_stem}")

    # Skip if today's run already completed
    if args.skip_if_exists:
        summary_path = output_dir / f"{file_stem}_summary.md"
        if summary_path.exists():
            log(f"Summary already exists, exiting (--skip-if-exists): {summary_path}")
            sys.exit(0)

    try:
        # Step 1: Download
        audio_path = step_download(args.url, output_dir, file_stem, args.cookies_from_browser, args.cookies_file)

        # Step 2: Transcribe
        transcript_path = step_transcribe(audio_path, output_dir, file_stem, args.model)

        # Step 3: Summarize
        log(f"Summarizing with Claude (focus: @{speaker})...")
        summary_path = step_summarize(transcript_path, output_dir, file_stem, speaker, args.url, args.claude_model)

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
        log(f"  Audio:      {audio_path}")
        log(f"  Transcript: {transcript_path}")
        log(f"  Summary:    {summary_path}")

    except Exception as e:
        log(f"ERROR: Pipeline failed — {e}")
        save_run_record(output_dir, space_id, {"url": args.url, "status": "failed", "error": str(e)})
        raise


if __name__ == "__main__":
    main()
