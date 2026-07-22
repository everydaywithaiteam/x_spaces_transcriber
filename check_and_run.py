#!/usr/bin/env python3
"""
check_and_run.py — Catch-up X Spaces checker for @StocksOnSpaces

Discovers the recent Spaces visible on the account's profile (the ~10 most
recent — X exposes no deeper history), filters out any already processed,
and runs the full pipeline (download → transcribe → summarize) on every new
one, oldest first. Each successfully summarized Space is emailed; a failed
send is retried automatically on the next run without re-running the
pipeline.

State is tracked in output/state.json — a dict of processed Space IDs keyed
by ID, so catch-up after a missed day (or several) picks up every Space that
hasn't been seen yet, not just the latest one.

Usage:
    python check_and_run.py [--dry-run] [--account HANDLE] [--speaker HANDLE]
                             [--model SIZE] [--claude-model MODEL] [--cookies-file FILE]

Space discovery prefers a Netscape-format cookies.txt export (./cookies.txt or
$COOKIES_FILE) over live Chrome cookie decryption, which can silently fail to
decrypt sensitive session cookies on newer Chrome versions — see README.
"""

import json
import os
import re
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

BASE_DIR   = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
STATE_FILE = OUTPUT_DIR / "state.json"
LOG_DIR    = BASE_DIR / "logs"

sys.path.insert(0, str(BASE_DIR))
from pipeline import (
    fetch_recent_space_urls, extract_space_id, make_file_stem,
    step_download, step_transcribe, step_summarize, save_run_record, log,
)
from email_notify import send_summary_email


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_date(file_stem: str) -> str:
    # Search anywhere, not just at the end — a disambiguated stem
    # (<stem>-<space_id> from a same-day collision) has the date in the middle.
    m = re.search(r"(\d{4}-\d{2}-\d{2})", file_stem)
    return m.group(1) if m else ""


def load_state() -> dict:
    """Load state, migrating from the old single-pointer schema (or building
    it from scratch out of existing output/*_run.json files) if needed."""
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if state.get("schema_version") == 2 and "processed" in state:
            return state
    else:
        state = {}

    # Migration / bootstrap: seed `processed` from existing run records so
    # spaces already summarized before this feature don't trigger a burst of
    # catch-up emails. Old entries are marked email_sent=True.
    processed = {}
    for record_path in OUTPUT_DIR.glob("*_run.json"):
        try:
            meta = json.loads(record_path.read_text())
        except Exception:
            continue
        if meta.get("status") != "success" or not meta.get("space_id"):
            continue
        summary_path = meta.get("summary")
        file_stem = Path(summary_path).stem
        if file_stem.endswith("_summary"):
            file_stem = file_stem[: -len("_summary")]
        processed[meta["space_id"]] = {
            "url": meta.get("url", ""),
            "account": meta.get("account", ""),
            "speaker": meta.get("speaker", ""),
            "file_stem": file_stem,
            "summary_path": summary_path,
            "processed_at": meta.get("timestamp", now_iso()),
            "email_sent": True,
            "email_sent_at": None,
            "email_attempts": 0,
            "email_last_error": None,
        }

    if processed:
        log(f"State migration: seeded {len(processed)} previously-processed space(s) from run records")

    return {"schema_version": 2, "last_run": None, "processed": processed}


def save_state(state: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Catch up on new Spaces for an X account and email each summary")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--account", default="StocksOnSpaces", help="Twitter account handle to watch")
    parser.add_argument("--speaker", default=None, help="Speaker handle for summary focus (default: same as --account)")
    parser.add_argument("--model", default="base",
                        choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model size (default: base)")
    parser.add_argument("--claude-model", default="claude-opus-4-5",
                        help="Claude model for summarization (default: claude-opus-4-5)")
    _default_cookies_file = os.environ.get("COOKIES_FILE") or str(BASE_DIR / "cookies.txt")
    parser.add_argument("--cookies-file", metavar="FILE",
                        default=_default_cookies_file if Path(_default_cookies_file).exists() else None,
                        help="Netscape-format cookies.txt for x.com (preferred over live Chrome "
                             "cookie decryption — see README). Defaults to ./cookies.txt or "
                             "$COOKIES_FILE if present.")
    args = parser.parse_args()

    speaker = args.speaker or args.account

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log("=" * 60)
    log(f"Checking @{args.account} for new Spaces (catch-up mode)")

    state = load_state()
    log(f"Currently tracking {len(state['processed'])} processed space(s)")

    if args.cookies_file:
        log(f"Using cookies file: {args.cookies_file}")
    urls = fetch_recent_space_urls(args.account, cookies_from_browser="chrome", cookies_file=args.cookies_file)
    log(f"Discovered {len(urls)} recent Space(s) on the profile")

    new_urls = [u for u in urls if extract_space_id(u) not in state["processed"]]
    to_process = list(reversed(new_urls))  # oldest-first for correct chronological order
    log(f"{len(to_process)} new Space(s) to process")

    # make_file_stem() falls back to <account>-<today's date> whenever the
    # discovered URL doesn't embed an account name (the common case for
    # x.com/i/spaces/<id> URLs). That's fine for one space per run, but a
    # catch-up run can process several spaces in the same call — track stems
    # already claimed (by other spaces) so a same-day collision gets a
    # disambiguating suffix instead of silently overwriting/skipping past
    # the first space's audio/transcript/summary files.
    claimed_stems = {entry["file_stem"]: sid for sid, entry in state["processed"].items()}

    for url in to_process:
        space_id = extract_space_id(url)
        file_stem = make_file_stem(url, args.account)
        if claimed_stems.get(file_stem, space_id) != space_id:
            file_stem = f"{file_stem}-{space_id}"
        claimed_stems[file_stem] = space_id

        if args.dry_run:
            log(f"[DRY RUN] would process {space_id} ({file_stem})")
            continue

        log(f"Processing {space_id} ({file_stem})...")
        try:
            audio_path = step_download(url, OUTPUT_DIR, file_stem, "chrome", args.cookies_file)
            transcript_path = step_transcribe(audio_path, OUTPUT_DIR, file_stem, args.model)
            summary_path = step_summarize(transcript_path, OUTPUT_DIR, file_stem,
                                           speaker, url, args.claude_model)

            save_run_record(OUTPUT_DIR, space_id, {
                "url": url, "account": args.account, "speaker": speaker,
                "audio": str(audio_path), "transcript": str(transcript_path),
                "summary": str(summary_path), "status": "success",
            })

            state["processed"][space_id] = {
                "url": url, "account": args.account, "speaker": speaker,
                "file_stem": file_stem, "summary_path": str(summary_path),
                "processed_at": now_iso(),
                "email_sent": False, "email_sent_at": None,
                "email_attempts": 0, "email_last_error": None,
            }
            save_state(state)
            log(f"✓ Processed {space_id}")
        except Exception as e:
            log(f"ERROR processing {space_id}: {e}")
            save_run_record(OUTPUT_DIR, space_id, {"url": url, "status": "failed", "error": str(e)})
            continue

    if not args.dry_run:
        pending = [(sid, entry) for sid, entry in state["processed"].items()
                   if not entry.get("email_sent")]
        if pending:
            log(f"Sending {len(pending)} pending summary email(s)...")
        for space_id, entry in pending:
            ok = send_summary_email(Path(entry["summary_path"]), {
                "account": entry["account"],
                "speaker": entry["speaker"],
                "url": entry["url"],
                "space_id": space_id,
                "date": _extract_date(entry["file_stem"]),
            })
            entry["email_attempts"] = entry.get("email_attempts", 0) + 1
            if ok:
                entry["email_sent"] = True
                entry["email_sent_at"] = now_iso()
                entry["email_last_error"] = None
            else:
                entry["email_last_error"] = "send failed — see log"
            save_state(state)

    state["last_run"] = now_iso()
    save_state(state)
    log("=" * 60)


if __name__ == "__main__":
    main()
