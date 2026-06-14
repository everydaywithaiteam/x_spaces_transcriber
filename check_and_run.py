#!/usr/bin/env python3
"""
check_and_run.py — Daily X Spaces checker for @StocksOnSpaces

Checks the @StocksOnSpaces X feed for a new Space since the last run.
If one is found, runs the full pipeline: download → transcribe → summarize.

State is tracked in output/state.json so it never re-processes the same Space.

Usage:
    python check_and_run.py [--dry-run] [--force]

Options:
    --dry-run    Print what would be done without downloading/processing
    --force      Re-process the latest Space even if already processed
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

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
ACCOUNT    = "StocksOnSpaces"
SPEAKER    = "stocktalkweekly"


def log(msg: str):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line, flush=True)


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"last_space_id": None, "last_run": None, "runs": []}


def save_state(state: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def find_latest_space(account: str) -> Optional[dict]:
    """Find the most recent Space URL for a Twitter/X account."""
    sys.path.insert(0, str(BASE_DIR))
    from pipeline import fetch_latest_space_url

    log(f"Checking @{account} for latest Space...")
    url = fetch_latest_space_url(account, cookies_from_browser="chrome")
    if not url:
        return None
    m = re.search(r"/spaces/([A-Za-z0-9]+)", url)
    space_id = m.group(1) if m else re.sub(r"[^A-Za-z0-9_-]", "_", url)[:20]
    return {"id": space_id, "url": url}


def run_pipeline(space_url: str, dry_run: bool = False) -> bool:
    """Run the full download → transcribe → summarize pipeline."""
    cmd = [
        sys.executable, str(BASE_DIR / "pipeline.py"),
        "--url", space_url,
        "--speaker", SPEAKER,
        "--cookies-from-browser", "chrome",
        "--skip-if-exists",
    ]

    log(f"Running pipeline: {' '.join(cmd)}")

    if dry_run:
        log("[DRY RUN] Would execute the above command.")
        return True

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"pipeline_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log"

    with open(log_file, "w") as lf:
        result = subprocess.run(cmd, cwd=BASE_DIR, stdout=lf, stderr=subprocess.STDOUT)

    if result.returncode == 0:
        log(f"Pipeline succeeded. Log: {log_file}")
        return True
    else:
        log(f"Pipeline FAILED (exit {result.returncode}). Log: {log_file}")
        # Print last 20 lines of log for quick diagnosis
        lines = log_file.read_text().splitlines()
        for line in lines[-20:]:
            log(f"  {line}")
        return False


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Check @StocksOnSpaces for new Space and process it")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--force",   action="store_true", help="Re-process latest Space even if already done")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log("=" * 60)
    log(f"Checking @{ACCOUNT} for new X Space")

    state = load_state()
    log(f"Last processed Space ID: {state.get('last_space_id') or 'none'}")

    latest = find_latest_space(ACCOUNT)

    if not latest:
        log("No Space found on the feed. Nothing to do.")
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    space_id  = latest["id"]
    space_url = latest["url"]
    log(f"Latest Space found: {space_id} — {space_url}")

    if not args.force and space_id == state.get("last_space_id"):
        log("Already processed this Space. Nothing to do.")
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        save_state(state)
        return

    log(f"New Space detected! Processing...")
    success = run_pipeline(space_url, dry_run=args.dry_run)

    if success or args.dry_run:
        state["last_space_id"] = space_id
        state["last_run"] = datetime.now(timezone.utc).isoformat()
        state.setdefault("runs", []).append({
            "space_id":  space_id,
            "space_url": space_url,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "status":    "success" if success else "dry_run",
        })
        save_state(state)
        log(f"State updated. Space {space_id} marked as processed.")

    log("=" * 60)


if __name__ == "__main__":
    main()
