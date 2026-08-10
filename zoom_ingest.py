#!/usr/bin/env python3
"""
zoom_ingest.py — Summarize Stock Talk Weekly Zoom episodes from their .vtt transcripts.

Zoom cloud recordings publish an "Audio Transcript" (.vtt) next to the video.
Feeding that in instead of the recording skips the two most expensive stages of
the X Spaces pipeline — there is no multi-GB download and no Whisper pass — and
the transcript arrives with speaker names already attached, so the summarizer
works from real attribution instead of inferring who the host is.

Drop .vtt files into transcripts_in/ and run. Everything downstream (summary
email, watchlist alerts, Notion sync) is the same machinery the Spaces pipeline
uses, driven off the same output/state.json, so a failed send retries on the
next run without re-summarizing.

Episode date is taken from a YYYY-MM-DD in the filename when present, otherwise
the file's modification time. Anything else in the filename becomes the title.

Usage:
    python zoom_ingest.py [--dry-run] [--speaker NAME] [--show NAME]
                          [--indir DIR] [--claude-model MODEL] [--keep]
"""

import argparse
import json
import os
import re
import shutil
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

BASE_DIR    = Path(__file__).parent
OUTPUT_DIR  = BASE_DIR / "output"
STATE_FILE  = OUTPUT_DIR / "state.json"
INBOX_DIR   = BASE_DIR / "transcripts_in"
DONE_DIR    = INBOX_DIR / "processed"

sys.path.insert(0, str(BASE_DIR))
from pipeline import log, save_run_record
from summarize import summarize
from vtt_ingest import parse_cues, merge_cues, render, speaker_stats, has_speaker_labels
from email_notify import send_summary_email
from ticker_alerts import alerts_enabled, extract_tickers, match_watchlist, send_watchlist_alert
from notion_sync import sync_summary_to_notion

DEFAULT_SHOW = "Stock Talk Weekly"


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_state() -> dict:
    """Shared with check_and_run.py — same file, same entry shape, so the
    delivery retry logic there also covers anything ingested here."""
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
    else:
        state = {"processed": {}, "last_run": None}
    state.setdefault("processed", {})
    return state


def save_state(state: dict):
    state["last_run"] = now_iso()
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def episode_meta(vtt_path: Path) -> dict:
    """Derive (date, title, file_stem, episode_id) from the filename.

    Zoom's own export names are like "GMT20260807-140233_Recording.transcript.vtt";
    a hand-renamed "2026-08-07 Episode 36.vtt" works too. Both yield a date and
    a stable ID, so re-running never double-processes an episode.
    """
    name = vtt_path.stem
    # Browsers append " (1)" to a repeat download, which would otherwise sit
    # between the name and the ".transcript" suffix and block the strip below.
    name = re.sub(r"\s*\(\d+\)\s*$", "", name)
    for suffix in (".transcript", ".cc"):
        if name.lower().endswith(suffix):
            name = name[: -len(suffix)]

    date_match = re.search(r"(20\d{2})-?(\d{2})-?(\d{2})", name)
    if date_match:
        date = f"{date_match.group(1)}-{date_match.group(2)}-{date_match.group(3)}"
        # Drop the date from the title so it isn't repeated in the file stem,
        # along with the GMT prefix and HHMMSS stamp Zoom's own exports carry.
        name = name[: date_match.start()] + " " + name[date_match.end():]
        name = re.sub(r"\bGMT\b", " ", name, flags=re.I)
        # `\b` would fail on "140233_Recording" — "_" is a word character.
        name = re.sub(r"[_\-\s]\d{6}(?!\d)", " ", name)
    else:
        date = datetime.fromtimestamp(vtt_path.stat().st_mtime).strftime("%Y-%m-%d")

    title = re.sub(r"[_\-\s]+", " ", name).strip(" -_") or date
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()[:60] or "episode"
    return {
        "date": date,
        "title": title,
        "file_stem": f"zoom-{date}-{slug}",
        # Namespaced so a Zoom episode can never collide with an X Space ID.
        "episode_id": f"zoom:{slug}:{date}",
    }


def pick_speaker(cues: list) -> str:
    """The speaker with the most airtime — for a hosted show, the host."""
    stats = [row for row in speaker_stats(cues) if row[0] != "(unlabelled)"]
    return stats[0][0] if stats else None


def process(vtt_path: Path, args, state: dict) -> bool:
    meta = episode_meta(vtt_path)
    episode_id, file_stem = meta["episode_id"], meta["file_stem"]

    if episode_id in state["processed"]:
        log(f"Already processed: {meta['title']} — skipping")
        return False

    cues = parse_cues(vtt_path.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        log(f"ERROR: no cues in {vtt_path.name} — not a WebVTT transcript?")
        return False

    labeled = has_speaker_labels(cues)
    speaker = args.speaker or pick_speaker(cues)
    duration = max((c["end"] for c in cues), default=0)
    named = [row for row in speaker_stats(cues) if row[0] != "(unlabelled)"]
    solo = len(named) == 1

    if not labeled:
        log(f"WARNING: {vtt_path.name} has few speaker labels — "
            f"summarizing without attribution")
    if not speaker:
        log(f"ERROR: no speaker could be determined for {vtt_path.name} — "
            f"pass --speaker to set one")
        return False

    log(f"{meta['title']}: {len(cues)} cues, {duration / 60:.0f} min, "
        f"{len(named)} speaker(s){' [solo]' if solo else ''}, focus on {speaker}")

    if args.dry_run:
        log(f"[DRY RUN] would summarize → {file_stem}_summary.md")
        return False

    try:
        transcript_path = OUTPUT_DIR / f"{file_stem}.txt"
        summary_path = OUTPUT_DIR / f"{file_stem}_summary.md"

        if transcript_path.exists():
            log(f"Transcript already exists: {transcript_path} — reusing")
        else:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            transcript_path.write_text(render(merge_cues(cues)), encoding="utf-8")
            log(f"Transcript saved: {transcript_path}")

        if summary_path.exists():
            log(f"Summary already exists: {summary_path} — skipping summarization")
        else:
            summarize(transcript_path, speaker=speaker, space_url=None,
                      output_path=summary_path, model=args.claude_model,
                      labeled=labeled, show=args.show, solo=solo)

        save_run_record(OUTPUT_DIR, episode_id, {
            "source": "zoom-vtt", "show": args.show, "title": meta["title"],
            "speaker": speaker, "vtt": str(vtt_path),
            "transcript": str(transcript_path), "summary": str(summary_path),
            "status": "success",
        })

        tickers_mentioned = extract_tickers(summary_path.read_text(encoding="utf-8"))
        watchlist_matches = match_watchlist(tickers_mentioned)

        state["processed"][episode_id] = {
            "url": None, "account": args.show, "speaker": speaker,
            "file_stem": file_stem, "summary_path": str(summary_path),
            "processed_at": now_iso(),
            "recorded_date": meta["date"],
            "recorded_date_checked": True,
            "source": "zoom-vtt", "title": meta["title"],
            "email_sent": False, "email_sent_at": None,
            "email_attempts": 0, "email_last_error": None,
            "tickers_mentioned": tickers_mentioned,
            "watchlist_matches": watchlist_matches,
            "watchlist_alert_sent": not (watchlist_matches and alerts_enabled()),
            "watchlist_alert_sent_at": None,
            "watchlist_alert_attempts": 0,
            "watchlist_alert_last_error": None,
            "notion_synced": False,
            "notion_synced_at": None,
            "notion_page_id": None,
            "notion_sync_attempts": 0,
            "notion_sync_last_error": None,
        }
        save_state(state)
        log(f"✓ Processed {meta['title']}")

        if not args.keep:
            DONE_DIR.mkdir(parents=True, exist_ok=True)
            shutil.move(str(vtt_path), str(DONE_DIR / vtt_path.name))
        return True

    except Exception as e:
        log(f"ERROR processing {vtt_path.name}: {e}")
        save_run_record(OUTPUT_DIR, episode_id, {
            "source": "zoom-vtt", "vtt": str(vtt_path),
            "status": "failed", "error": str(e),
        })
        return False


def deliver(state: dict):
    """Email / alert / Notion for anything still pending, Zoom or Space alike."""
    def meta_for(entry_id, entry):
        return {
            "account": entry["account"], "speaker": entry["speaker"],
            "url": entry.get("url"), "space_id": entry_id,
            "date": entry.get("recorded_date") or "",
        }

    pending = [(i, e) for i, e in state["processed"].items() if not e.get("email_sent")]
    if pending:
        log(f"Sending {len(pending)} pending summary email(s)...")
    for entry_id, entry in pending:
        ok = send_summary_email(Path(entry["summary_path"]), meta_for(entry_id, entry))
        entry["email_attempts"] = entry.get("email_attempts", 0) + 1
        if ok:
            entry["email_sent"], entry["email_sent_at"] = True, now_iso()
            entry["email_last_error"] = None
        else:
            entry["email_last_error"] = "send failed — see log"
        save_state(state)

    pending_alerts = [(i, e) for i, e in state["processed"].items()
                      if not e.get("watchlist_alert_sent") and e.get("watchlist_matches")] \
                     if alerts_enabled() else []
    if pending_alerts:
        log(f"Sending {len(pending_alerts)} pending watchlist alert(s)...")
    for entry_id, entry in pending_alerts:
        meta = meta_for(entry_id, entry)
        meta["summary_path"] = entry["summary_path"]
        ok = send_watchlist_alert(Path(entry["summary_path"]), meta, entry["watchlist_matches"])
        entry["watchlist_alert_attempts"] = entry.get("watchlist_alert_attempts", 0) + 1
        if ok:
            entry["watchlist_alert_sent"], entry["watchlist_alert_sent_at"] = True, now_iso()
            entry["watchlist_alert_last_error"] = None
        else:
            entry["watchlist_alert_last_error"] = "send failed — see log"
        save_state(state)

    pending_notion = [(i, e) for i, e in state["processed"].items() if not e.get("notion_synced")]
    if pending_notion:
        log(f"Syncing {len(pending_notion)} summary/summaries to Notion...")
    for entry_id, entry in pending_notion:
        page_id = sync_summary_to_notion(Path(entry["summary_path"]),
                                         meta_for(entry_id, entry),
                                         entry.get("tickers_mentioned", []))
        entry["notion_sync_attempts"] = entry.get("notion_sync_attempts", 0) + 1
        if page_id:
            entry["notion_synced"], entry["notion_synced_at"] = True, now_iso()
            entry["notion_page_id"], entry["notion_sync_last_error"] = page_id, None
        else:
            entry["notion_sync_last_error"] = "sync failed — see log"
        save_state(state)


def main():
    parser = argparse.ArgumentParser(description="Summarize Zoom episodes from .vtt transcripts")
    parser.add_argument("--indir", default=str(INBOX_DIR), help="Folder holding .vtt files")
    parser.add_argument("--speaker", help="Speaker to focus on (default: most airtime)")
    parser.add_argument("--show", default=os.environ.get("ZOOM_SHOW_NAME", DEFAULT_SHOW))
    parser.add_argument("--claude-model", default="claude-opus-4-5")
    parser.add_argument("--dry-run", action="store_true", help="Report what would run, change nothing")
    parser.add_argument("--keep", action="store_true", help="Leave .vtt files in place after processing")
    args = parser.parse_args()

    indir = Path(args.indir)
    if not indir.exists():
        indir.mkdir(parents=True, exist_ok=True)
        log(f"Created {indir} — drop Zoom .vtt transcripts there and re-run.")
        return

    # DONE_DIR lives inside the inbox, so exclude it from the scan.
    vtt_files = sorted(p for p in indir.glob("*.vtt") if p.parent == indir)
    if not vtt_files:
        log(f"No .vtt files in {indir}")
    else:
        log(f"Found {len(vtt_files)} .vtt file(s) in {indir}")

    state = load_state()
    for vtt_path in vtt_files:
        process(vtt_path, args, state)

    if not args.dry_run:
        deliver(state)
        save_state(state)


if __name__ == "__main__":
    main()
