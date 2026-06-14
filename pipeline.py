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
    --skip-if-exists     Skip if today's output already exists

Environment:
    ANTHROPIC_API_KEY    Required for summarization
    HF_TOKEN             Required for speaker diarization (optional feature)
    SPACE_URL            Can set the Space URL via env var (useful for cron)

Running daily via cron (example — runs at 9am):
    0 9 * * * cd /path/to/x_spaces_downloader && SPACE_URL=https://x.com/i/spaces/... ANTHROPIC_API_KEY=sk-... python pipeline.py >> logs/pipeline.log 2>&1

Or with launchd on macOS — see README for setup.
"""

import argparse
import os
import sys
import json
import re
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


def save_run_record(output_dir: Path, space_id: str, meta: dict):
    """Save a JSON record of this run for deduplication and history."""
    record_path = output_dir / f"{space_id}_run.json"
    meta["space_id"] = space_id
    meta["timestamp"] = datetime.now().isoformat()
    record_path.write_text(json.dumps(meta, indent=2), encoding="utf-8")


# ── Pipeline steps ────────────────────────────────────────────────────────────

def step_download(url: str, output_dir: Path, file_stem: str, cookies_from_browser: str = None) -> Path:
    import yt_dlp

    candidates = list(output_dir.glob(f"{file_stem}.*"))
    existing = [f for f in candidates if f.suffix in (".m4a", ".mp3", ".aac", ".opus", ".webm", ".mp4")]
    if existing:
        log(f"Audio already exists: {existing[0]} — skipping download")
        return existing[0]

    log(f"Downloading Space ({file_stem})...")
    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / f"{file_stem}.%(ext)s"),
        "quiet": True,
    }
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        ext = info.get("ext", "m4a")

    audio_path = output_dir / f"{file_stem}.{ext}"
    log(f"Downloaded: {audio_path}")
    return audio_path


def step_transcribe(audio_path: Path, output_dir: Path, file_stem: str, model_size: str = "base") -> Path:
    from faster_whisper import WhisperModel

    transcript_path = output_dir / f"{file_stem}.txt"
    if transcript_path.exists():
        log(f"Transcript already exists: {transcript_path} — skipping transcription")
        return transcript_path

    log(f"Transcribing audio (model={model_size})...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(audio_path), beam_size=5)
    log(f"Detected language: {info.language}")

    lines = []
    for seg in segments:
        lines.append(f"[{seg.start:.1f}s - {seg.end:.1f}s] {seg.text.strip()}")

    transcript_path.write_text("\n".join(lines), encoding="utf-8")
    log(f"Transcript saved: {transcript_path} ({len(lines)} segments)")
    return transcript_path


def step_summarize(transcript_path: Path, output_dir: Path, file_stem: str,
                   speaker: str, space_url: str, model: str = "claude-opus-4-5") -> Path:
    summary_path = output_dir / f"{file_stem}_summary.md"
    if summary_path.exists():
        log(f"Summary already exists: {summary_path} — skipping summarization")
        return summary_path

    # Import the summarize module from the same directory
    sys.path.insert(0, str(Path(__file__).parent))
    from summarize import summarize
    return summarize(transcript_path, speaker, space_url, summary_path, model)


# ── Main ──────────────────────────────────────────────────────────────────────

def _find_space_via_twitter_api(account: str) -> Optional[str]:
    """Twitter API v2 lookup — requires TWITTER_BEARER_TOKEN in environment."""
    bearer = os.environ.get("TWITTER_BEARER_TOKEN")
    if not bearer:
        return None

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
        return None
    user_id = data["data"]["id"]

    # Step 2: check for live / scheduled Spaces
    data, _ = _get("/spaces/by/creator_ids",
                   {"user_ids": user_id, "space.fields": "state,created_at"})
    if data and data.get("data"):
        url = f"https://x.com/i/spaces/{data['data'][0]['id']}"
        log(f"Found live/scheduled Space via Twitter API: {url}")
        return url

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
                log(f"Found recent Space via Twitter API: {url}")
                return url

    log(f"Twitter API: no recent Spaces found for @{account}")
    return None


def _find_space_via_playwright(account: str) -> Optional[str]:
    """Navigate to the account's /spaces tab using Playwright + Chrome cookies."""
    try:
        from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout
    except ImportError:
        return None

    import http.cookiejar

    # Extract Chrome cookies via yt-dlp's Python API (no subprocess PATH issues)
    try:
        import yt_dlp
        ydl = yt_dlp.YoutubeDL({"cookiesfrombrowser": ("chrome",), "quiet": True})
        jar = ydl.cookiejar
        ydl.__exit__(None, None, None)
    except Exception as e:
        log(f"Playwright: cookie extraction failed — {e}")
        return None

    def _is_x_domain(domain: str) -> bool:
        d = domain.lstrip(".")
        return d in ("twitter.com", "x.com") or d.endswith(".twitter.com") or d.endswith(".x.com")

    # WebKit timestamp epoch offset (microseconds between 1601-01-01 and 1970-01-01)
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
            if exp > 10_000_000_000:  # WebKit microseconds — convert to Unix seconds
                exp = (exp - _WEBKIT_OFFSET_US) // 1_000_000
            if exp > 0:
                entry["expires"] = exp
        pw_cookies.append(entry)

    if not pw_cookies:
        log("Playwright: no Twitter/X cookies found in Chrome — log in to x.com first")
        return None

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
                url = f"https://x.com/i/spaces/{space_ids[0]}"
                log(f"Found Space via Playwright (intercepted {len(space_ids)} ids): {url}")
                return url

            log(f"Playwright: no AudioSpaceById calls fired for @{account} — no recent Spaces in timeline")
        except PWTimeout:
            log("Playwright: page timed out")
        except Exception as e:
            log(f"Playwright: error — {e}")
        finally:
            browser.close()

    return None


def _find_space_via_ydlp(account: str, cookies_from_browser: str = None) -> Optional[str]:
    """Scrape the account's /spaces tab with yt-dlp as a fallback."""
    import yt_dlp

    ydl_opts = {"extract_flat": True, "quiet": True, "playlistend": 10}
    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)

    for candidate in [
        f"https://x.com/{account}/spaces",
        f"https://x.com/{account}",
    ]:
        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(candidate, download=False)
            for entry in (info or {}).get("entries") or []:
                for field in ("url", "webpage_url"):
                    m = re.search(r"https?://(?:x|twitter)\.com/i/spaces/([A-Za-z0-9]+)",
                                  entry.get(field) or "")
                    if m:
                        url = f"https://x.com/i/spaces/{m.group(1)}"
                        log(f"Found Space via yt-dlp ({candidate}): {url}")
                        return url
        except Exception as e:
            log(f"yt-dlp on {candidate}: {e}")

    return None


def fetch_latest_space_url(account: str, cookies_from_browser: str = None) -> Optional[str]:
    """Find the most recent Space from a Twitter/X account.

    Tries in order:
      1. Twitter API v2  — set TWITTER_BEARER_TOKEN in .env
         (free app token from developer.twitter.com is sufficient)
      2. yt-dlp /spaces tab scrape — works when --cookies-from-browser is set
    """
    url = _find_space_via_twitter_api(account)
    if url:
        return url

    url = _find_space_via_playwright(account)
    if url:
        return url

    url = _find_space_via_ydlp(account, cookies_from_browser)
    if url:
        return url

    log("Auto-detection could not find a Space URL.")
    log("  → Ensure you are logged in to x.com in Chrome (used for Playwright scraping)")
    if not os.environ.get("TWITTER_BEARER_TOKEN"):
        log("  → Or add TWITTER_BEARER_TOKEN to .env (requires Twitter API Basic plan)")
    log("  → Or pass --url <space_url> directly")
    return None


def main():
    parser = argparse.ArgumentParser(description="Daily X Spaces pipeline")
    parser.add_argument("--url", default=os.environ.get("SPACE_URL"),
                        help="Space URL to process (or set SPACE_URL env var)")
    parser.add_argument("--account", default="StocksOnSpaces",
                        help="Twitter account handle to watch (default: StocksOnSpaces)")
    parser.add_argument("--speaker", default=None,
                        help="Speaker handle for summary focus (default: same as --account)")
    parser.add_argument("--model", default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model size (default: base)")
    parser.add_argument("--claude-model", default="claude-opus-4-5",
                        help="Claude model for summarization (default: claude-opus-4-5)")
    parser.add_argument("--output-dir", default="output",
                        help="Output directory (default: ./output)")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="Load cookies from browser: chrome, firefox, safari")
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
        args.url = fetch_latest_space_url(args.account, args.cookies_from_browser)

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
        audio_path = step_download(args.url, output_dir, file_stem, args.cookies_from_browser)

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
