#!/usr/bin/env python3
"""
zoom_queue.py — Watch a file of Zoom share links and ingest each new one.

Paste a share URL and its passcode into zoom_calls_input.txt:

    https://us06web.zoom.us/rec/share/<share-token>?startTime=<epoch-ms>
    Passcode: <passcode here>

and this downloads the transcript (zoom_fetch.py), hands it to zoom_ingest.py
to summarize and email, and records the link as done so the next run skips it.
Under launchd on a 5-minute timer, that makes pasting a link the only manual
step.

Which links have been handled is tracked in output/state.json under a
"zoom_queue" key, so the input file is an append-only list you never have to
prune — re-adding an old link is a no-op.

Usage:
    python zoom_queue.py [--dry-run] [--input FILE] [--headful]
                         [--no-deliver] [--max-attempts N]
"""

import argparse
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

# Load .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
STATE_FILE = OUTPUT_DIR / "state.json"
INBOX_DIR = BASE_DIR / "transcripts_in"
DEFAULT_INPUT = BASE_DIR / "zoom_calls_input.txt"

sys.path.insert(0, str(BASE_DIR))
from pipeline import log, state_lock
from zoom_fetch import fetch_vtt, share_token, ZoomFetchError, ZoomPasscodeRejected
from email_notify import send_email

MAX_ATTEMPTS = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def input_path(override: str = None) -> Path:
    if override:
        return Path(override)
    return Path(os.environ.get("ZOOM_INPUT_FILE") or DEFAULT_INPUT)


def parse_entries(path: Path) -> list:
    """Read (url, passcode) pairs out of the input file.

    A line starting with http opens an entry; a following "Passcode:" line
    attaches to it. Everything else is ignored, so you can paste the block
    straight out of an email without tidying it up.

    "#" only starts a comment at the beginning of a line — Zoom passcodes
    routinely contain #, $ and ?, and stripping a trailing comment would
    silently corrupt them. For the same reason the passcode is taken verbatim
    after the colon and never goes near a shell.
    """
    if not path.exists():
        return []

    entries, current = [], None
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith(("http://", "https://")):
            if current:
                entries.append(current)
            current = {"url": line, "passcode": ""}
        elif current and ":" in line and line.split(":", 1)[0].strip().lower() in ("passcode", "password", "pass"):
            current["passcode"] = line.split(":", 1)[1].strip()
    if current:
        entries.append(current)
    return entries


def load_state() -> dict:
    import json
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {"processed": {}, "last_run": None}
    state.setdefault("processed", {})
    state.setdefault("zoom_queue", {})
    return state


def save_state(state: dict):
    import json
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def notify_failure(entry: dict, record: dict):
    """Email once when a link is given up on.

    Without this the launchd job fails silently and the first you'd know is a
    summary that never arrived. Gated on `notified` so a dead link cannot mail
    you every five minutes forever.
    """
    if record.get("notified"):
        return
    url = entry["url"]
    reason = record.get("last_error", "unknown error")
    attempts = record.get("attempts", 0)
    subject = f"Zoom ingest failed — {datetime.now().strftime('%Y-%m-%d')}"
    plain = (f"Could not download the transcript for:\n\n{url}\n\n"
             f"Reason: {reason}\nAttempts: {attempts}\n\n"
             f"Any saved page dump is in logs/zoom_fail_*.html / .png.\n"
             f"Fix the entry in {input_path()} and it will be retried.")
    html = (f"<p>Could not download the transcript for:</p>"
            f"<p><a href=\"{url}\">{url}</a></p>"
            f"<p><b>Reason:</b> {reason}<br><b>Attempts:</b> {attempts}</p>"
            f"<p>Any saved page dump is in <code>logs/zoom_fail_*.html</code> / <code>.png</code>.</p>")
    if send_email(subject, plain, html):
        record["notified"] = True
        log("Failure email sent")


def run_ingest(no_deliver: bool) -> bool:
    """Hand the downloaded .vtt files to zoom_ingest.py.

    A subprocess rather than an import: zoom_ingest already scans the inbox,
    summarizes, writes state and emails on its own, and keeping it at arm's
    length means this runner needs no say in how any of that works.
    """
    cmd = [sys.executable, str(BASE_DIR / "zoom_ingest.py")]
    if no_deliver:
        cmd.append("--no-deliver")
    log(f"Running {' '.join(Path(c).name for c in cmd[:2])}…")
    result = subprocess.run(cmd, cwd=str(BASE_DIR))
    if result.returncode != 0:
        log(f"zoom_ingest.py exited {result.returncode}")
        return False
    return True


def process_entry(entry: dict, state: dict, args) -> bool:
    """Download one link. Returns True if a new .vtt landed in the inbox."""
    key = share_token(entry["url"])
    record = state["zoom_queue"].get(key)

    if record and record.get("status") == "success":
        return False
    if record and record.get("status") == "failed":
        return False
    if not entry["passcode"]:
        log(f"No passcode for {key[:16]}… — skipping")
        return False

    record = record or {"url": entry["url"], "status": "pending", "attempts": 0,
                        "vtt_path": None, "last_error": None,
                        "downloaded_at": None, "notified": False}
    state["zoom_queue"][key] = record

    if args.dry_run:
        log(f"[DRY RUN] would download {entry['url']}")
        return False

    record["attempts"] = record.get("attempts", 0) + 1
    try:
        vtt_path = fetch_vtt(entry["url"], entry["passcode"], INBOX_DIR,
                             headful=args.headful or None)
        record.update(status="success", vtt_path=str(vtt_path),
                      downloaded_at=now_iso(), last_error=None)
        save_state(state)
        return True
    except ZoomFetchError as e:
        record["last_error"] = str(e)
        # A rejected passcode will be rejected again — only genuinely transient
        # failures are worth another five-minute cycle.
        terminal = isinstance(e, ZoomPasscodeRejected) or not getattr(e, "retryable", False)
        if terminal or record["attempts"] >= args.max_attempts:
            record["status"] = "failed"
            log(f"ERROR: giving up on {key[:16]}… — {e}")
            notify_failure(entry, record)
        else:
            record["status"] = "pending"
            log(f"Attempt {record['attempts']}/{args.max_attempts} failed — will retry: {e}")
        save_state(state)
        return False


def main():
    parser = argparse.ArgumentParser(description="Download and ingest queued Zoom recordings")
    parser.add_argument("--input", help="Input file (default: zoom_calls_input.txt or $ZOOM_INPUT_FILE)")
    parser.add_argument("--dry-run", action="store_true", help="Report what would run, change nothing")
    parser.add_argument("--headful", action="store_true", help="Show the browser window")
    parser.add_argument("--no-deliver", action="store_true",
                        help="Summarize only — skip email, alerts and Notion sync")
    parser.add_argument("--max-attempts", type=int, default=MAX_ATTEMPTS)
    args = parser.parse_args()

    path = input_path(args.input)
    entries = parse_entries(path)
    if not entries:
        log(f"No entries in {path}")
        return

    with state_lock(OUTPUT_DIR, "zoom queue") as acquired:
        if not acquired:
            return

        state = load_state()
        pending = [e for e in entries
                   if state["zoom_queue"].get(share_token(e["url"]), {}).get("status")
                   not in ("success", "failed")]
        if not pending:
            log(f"{len(entries)} entr{'y' if len(entries) == 1 else 'ies'} in {path.name}, all already handled")
            return

        log(f"{len(pending)} new entr{'y' if len(pending) == 1 else 'ies'} in {path.name}")
        downloaded = sum(process_entry(e, state, args) for e in pending)
        save_state(state)

        if downloaded:
            run_ingest(args.no_deliver)
        elif not args.dry_run:
            log("Nothing downloaded — not running zoom_ingest")


if __name__ == "__main__":
    main()
