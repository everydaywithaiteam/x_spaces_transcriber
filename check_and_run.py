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
from html import escape
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
    fetch_space_recorded_date, fetch_space_meta, space_not_ready_reason,
    SpaceNotReady, XSessionBlocked,
    step_download, step_transcribe, step_summarize, save_run_record, log,
    discard_audio, state_lock,
)
from ticker_alerts import alerts_enabled, match_watchlist, tickers_for_summary
from deliver import deliver_pending
from email_notify import send_email

# A lone challenge can clear by itself on the next run; three in a row
# (~15 min on the launchd timer) means a human has to step in.
BLOCKED_RUNS_BEFORE_EMAIL = 3

# A single Space failing a few times can be a flaky network or a slow replay.
# Six runs in a row (~30 min) is a broken pipeline: in Nov 2026 X's longer
# Space IDs made every download fail ~2,900 times over three days, silently.
SPACE_FAILURE_RUNS_BEFORE_EMAIL = 6


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def notify_blocked(record: dict):
    """Email once when X keeps blocking discovery.

    Without this a blocked session looks exactly like a quiet week: every run
    logs "0 new Spaces" and no summary ever arrives. Gated on `notified` so it
    cannot mail you every five minutes; cleared when discovery works again.
    """
    if record.get("notified") or record.get("runs", 0) < BLOCKED_RUNS_BEFORE_EMAIL:
        return
    reason = record.get("reason", "unknown")
    since = record.get("since", "")
    subject = f"X Spaces discovery blocked — {datetime.now().strftime('%Y-%m-%d')}"
    steps = ("1. Log in to x.com in Chrome and clear any account check it shows.\n"
             "2. Re-export cookies.txt for x.com into the project root (see README, "
             "'Space discovery: cookies.txt').\n"
             "The next scheduled run picks it up; no restart needed.")
    plain = (f"No new Spaces can be found because X is blocking the scraper's session.\n\n"
             f"Reason: {reason}\nBlocked since: {since}\n\n{steps}")
    html = (f"<p>No new Spaces can be found because X is blocking the scraper's session.</p>"
            f"<p><b>Reason:</b> {reason}<br><b>Blocked since:</b> {since}</p>"
            f"<p>{steps.replace(chr(10), '<br>')}</p>")
    if send_email(subject, plain, html):
        record["notified"] = True
        log("Blocked-session email sent")


def discover(args, state: dict) -> list:
    """Recent Space URLs, tracking a blocked X session in state["x_blocked"]."""
    try:
        urls = fetch_recent_space_urls(args.account, cookies_from_browser="chrome",
                                       cookies_file=args.cookies_file)
    except XSessionBlocked as e:
        record = state.setdefault("x_blocked", {"since": now_iso(), "runs": 0, "notified": False})
        record["runs"] += 1
        record["reason"] = str(e)
        log(f"Discovery BLOCKED ({record['runs']} run(s) in a row since {record['since']}): {e}")
        if not args.dry_run:
            notify_blocked(record)
        return []

    if state.pop("x_blocked", None):
        log("Discovery working again — cleared the blocked-session flag")
    return urls


def record_space_failure(state: dict, space_id: str, url: str, error: str) -> dict:
    """Count one more consecutive failed run for this Space in state["space_failures"]."""
    failures = state.setdefault("space_failures", {})
    record = failures.setdefault(space_id, {"url": url, "since": now_iso(),
                                            "runs": 0, "notified": False})
    record["runs"] += 1
    record["error"] = error
    record["last_failed_at"] = now_iso()
    log(f"  {space_id} has failed {record['runs']} run(s) in a row since {record['since']}")
    return record


def clear_space_failure(state: dict, space_id: str):
    """Forget a Space's failure streak; returns the record if an alert had gone out."""
    record = state.get("space_failures", {}).pop(space_id, None)
    if not state.get("space_failures"):
        state.pop("space_failures", None)
    if record is None:
        return None
    log(f"{space_id} working again after {record['runs']} failed run(s)")
    return record if record.get("notified") else None


def prune_space_failures(state: dict, discovered_ids: set):
    """Drop streaks for Spaces no longer on the profile — nothing will retry them.

    Only called when discovery actually returned Spaces, so a blocked or empty
    discovery run never wipes a live streak.
    """
    failures = state.get("space_failures", {})
    for space_id in [sid for sid in failures if sid not in discovered_ids]:
        log(f"{space_id} dropped off the profile — no longer tracking its failures")
        del failures[space_id]
    if not failures:
        state.pop("space_failures", None)


def notify_space_failures(state: dict):
    """Email once per failure streak, batching every Space that crossed the threshold.

    Without this a broken download step looks like a quiet week: each run logs
    an ERROR, writes a failed run record, and no summary ever arrives. Gated on
    `notified` so it cannot mail you every five minutes; the streak is cleared
    when the Space succeeds (see notify_space_recovered).
    """
    due = {sid: r for sid, r in state.get("space_failures", {}).items()
           if not r.get("notified") and r.get("runs", 0) >= SPACE_FAILURE_RUNS_BEFORE_EMAIL}
    if not due:
        return
    n = len(due)
    subject = (f"X Spaces processing failing — {n} Space{'s' if n != 1 else ''} — "
               f"{datetime.now().strftime('%Y-%m-%d')}")
    lines = [f"- {sid}: failed {r['runs']} runs in a row since {r.get('since', '')}\n"
             f"  {r.get('url', '')}\n  Last error: {r.get('error', 'unknown')}"
             for sid, r in due.items()]
    intro = ("These Spaces keep failing to download or process, so no summary is "
             "being sent for them. The scheduled run keeps retrying every 5 minutes; "
             "check logs/ and output/<space_id>_run.json for the full error. "
             "You'll get another email when they succeed.")
    plain = intro + "\n\n" + "\n".join(lines)
    items = "".join(
        f"<li><b>{sid}</b>: failed {r['runs']} runs in a row since {escape(r.get('since', ''))}<br>"
        f"{escape(r.get('url', ''))}<br><code>{escape(r.get('error', 'unknown'))}</code></li>"
        for sid, r in due.items())
    html = f"<p>{escape(intro)}</p><ul>{items}</ul>"
    if send_email(subject, plain, html):
        for r in due.values():
            r["notified"] = True
        log(f"Space-failure email sent for {', '.join(due)}")


def notify_space_recovered(recovered: dict):
    """One 'working again' email for Spaces whose failure alert had been sent."""
    if not recovered:
        return
    subject = f"X Spaces processing working again — {datetime.now().strftime('%Y-%m-%d')}"
    lines = [f"- {sid}: processed after {r['runs']} failed run(s) since {r.get('since', '')}"
             for sid, r in recovered.items()]
    plain = ("These Spaces you were alerted about have now been processed; "
             "their summaries follow separately.\n\n" + "\n".join(lines))
    html = ("<p>These Spaces you were alerted about have now been processed; "
            "their summaries follow separately.</p><ul>"
            + "".join(f"<li>{escape(l[2:])}</li>" for l in lines) + "</ul>")
    if send_email(subject, plain, html):
        log(f"Space-recovered email sent for {', '.join(recovered)}")


def _inert_feature_fields() -> dict:
    """Default watchlist-alert/Notion-sync fields for spaces that predate these
    features — marked as already-done so nothing fires retroactively."""
    return {
        "tickers_mentioned": [],
        "watchlist_matches": [],
        "watchlist_alert_sent": True,
        "watchlist_alert_sent_at": None,
        "watchlist_alert_attempts": 0,
        "watchlist_alert_last_error": None,
        "notion_synced": True,
        "notion_synced_at": None,
        "notion_page_id": None,
        "notion_sync_attempts": 0,
        "notion_sync_last_error": None,
    }


def load_state() -> dict:
    """Load state, migrating from the old single-pointer schema (or building
    it from scratch out of existing output/*_run.json files) if needed."""
    if STATE_FILE.exists():
        state = json.loads(STATE_FILE.read_text())
        if state.get("schema_version") == 2 and "processed" in state:
            for entry in state["processed"].values():
                for key, default in _inert_feature_fields().items():
                    entry.setdefault(key, default)
                entry.setdefault("recorded_date", None)
                entry.setdefault("recorded_date_checked", False)
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
            "recorded_date": None,
            "recorded_date_checked": False,
            **_inert_feature_fields(),
        }

    if processed:
        log(f"State migration: seeded {len(processed)} previously-processed space(s) from run records")

    return {"schema_version": 2, "last_run": None, "processed": processed}


def backfill_recorded_dates(state: dict, cookies_from_browser: str = None,
                             cookies_file: str = None) -> int:
    """Fill in `recorded_date` for spaces processed before it was tracked.

    Runs at most once per space (`recorded_date_checked` is set either way, so a
    Space that's since been deleted from X doesn't get re-fetched every run).
    """
    stale = [(sid, e) for sid, e in state["processed"].items()
             if not e.get("recorded_date_checked")]
    if not stale:
        return 0

    log(f"Backfilling recording dates for {len(stale)} space(s)...")
    filled = 0
    for space_id, entry in stale:
        recorded = fetch_space_recorded_date(entry.get("url", ""), cookies_from_browser, cookies_file)
        entry["recorded_date"] = recorded
        entry["recorded_date_checked"] = True
        if recorded:
            filled += 1
    save_state(state)
    log(f"Backfill complete: {filled}/{len(stale)} resolved")
    return filled


def save_state(state: dict):
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Catch up on new Spaces for an X account and email each summary")
    parser.add_argument("--dry-run", action="store_true", help="Print actions without executing")
    parser.add_argument("--no-deliver", action="store_true",
                        help="Process only — skip email, watchlist alerts, and Notion sync. "
                             "Pending sends stay queued for a later run.")
    parser.add_argument("--account", default="StocksOnSpaces", help="Twitter account handle to watch")
    parser.add_argument("--speaker", default=None, help="Speaker handle for summary focus (default: same as --account)")
    parser.add_argument("--model", default="turbo",
                        help="Whisper model: tiny, base, small, medium, large, large-v3, "
                             "turbo, or a full Hugging Face repo path (default: turbo). "
                             "Runs on the GPU via mlx-whisper when available.")
    parser.add_argument("--claude-model", default="claude-opus-5",
                        help="Claude model for summarization (default: claude-opus-5)")
    parser.add_argument("--keep-audio", action="store_true",
                        help="Keep the downloaded .m4a after transcription "
                             "(default: delete it — a Space is 60-100 MB and is "
                             "never read again once the transcript exists)")
    _default_cookies_file = os.environ.get("COOKIES_FILE") or str(BASE_DIR / "cookies.txt")
    parser.add_argument("--cookies-file", metavar="FILE",
                        default=_default_cookies_file if Path(_default_cookies_file).exists() else None,
                        help="Netscape-format cookies.txt for x.com (preferred over live Chrome "
                             "cookie decryption — see README). Defaults to ./cookies.txt or "
                             "$COOKIES_FILE if present.")
    args = parser.parse_args()

    with state_lock(OUTPUT_DIR, "catch-up run") as acquired:
        if not acquired:
            return
        run(args)


def run(args):
    """The actual catch-up pass, holding the shared state lock."""
    speaker = args.speaker or args.account

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    log("=" * 60)
    log(f"Checking @{args.account} for new Spaces (catch-up mode)")

    state = load_state()
    log(f"Currently tracking {len(state['processed'])} processed space(s)")

    if args.cookies_file:
        log(f"Using cookies file: {args.cookies_file}")

    if not args.dry_run:
        backfill_recorded_dates(state, "chrome", args.cookies_file)
    urls = discover(args, state)
    log(f"Discovered {len(urls)} recent Space(s) on the profile")

    if urls:
        prune_space_failures(state, {extract_space_id(u) for u in urls})

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

    deferred = 0
    recovered = {}
    for url in to_process:
        space_id = extract_space_id(url)
        # One probe answers both questions. Before the stem, not after: the stem
        # is derived from this date, and is also how step_download/step_transcribe
        # find work they already did.
        meta = fetch_space_meta(url, "chrome", args.cookies_file)
        recorded_date = meta["recorded_date"]

        # A Space that is still live has no replay yet, and following the live
        # edge captures only its tail. Leave it alone — the next scheduled run
        # picks up the replay, usually within half an hour of it ending.
        #
        # Critically, it must NOT go into state["processed"]: that is the only
        # record of what has been handled, so marking a live Space done would
        # make the pipeline skip the full replay forever.
        not_ready = space_not_ready_reason(meta["live_status"])
        if not_ready:
            deferred += 1
            log(f"Deferring {space_id}: {not_ready}")
            continue

        file_stem = make_file_stem(url, args.account, recorded_date)
        if claimed_stems.get(file_stem, space_id) != space_id:
            file_stem = f"{file_stem}-{space_id}"
        claimed_stems[file_stem] = space_id

        if args.dry_run:
            log(f"[DRY RUN] would process {space_id} ({file_stem})")
            continue

        log(f"Processing {space_id} ({file_stem})...")
        if recorded_date:
            log(f"  recorded {recorded_date}")
        try:
            audio_path = step_download(url, OUTPUT_DIR, file_stem, "chrome", args.cookies_file,
                                       live_status=meta["live_status"])
            transcript_path = step_transcribe(audio_path, OUTPUT_DIR, file_stem, args.model)
            summary_path = step_summarize(transcript_path, OUTPUT_DIR, file_stem,
                                           speaker, url, args.claude_model)

            # The m4a has done its job once the transcript exists, and a Space
            # is 60-100 MB of it. Nothing downstream reads audio again.
            discard_audio(audio_path, transcript_path, keep=args.keep_audio)

            save_run_record(OUTPUT_DIR, space_id, {
                "url": url, "account": args.account, "speaker": speaker,
                "audio": str(audio_path), "transcript": str(transcript_path),
                "summary": str(summary_path), "status": "success",
            })

            tickers_mentioned = tickers_for_summary(summary_path)
            watchlist_matches = match_watchlist(tickers_mentioned)

            state["processed"][space_id] = {
                "url": url, "account": args.account, "speaker": speaker,
                "file_stem": file_stem, "summary_path": str(summary_path),
                "processed_at": now_iso(),
                "recorded_date": recorded_date,
                "recorded_date_checked": True,
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
            was_alerted = clear_space_failure(state, space_id)
            if was_alerted:
                recovered[space_id] = was_alerted
            save_state(state)
            log(f"✓ Processed {space_id}")
        except SpaceNotReady as e:
            # Not a failure: nothing is wrong, the Space just is not finished.
            # No run record and no state entry, so the next run retries it.
            deferred += 1
            log(f"Deferring {space_id}: {e}")
            continue
        except Exception as e:
            log(f"ERROR processing {space_id}: {e}")
            save_run_record(OUTPUT_DIR, space_id, {"url": url, "status": "failed", "error": str(e)})
            record_space_failure(state, space_id, url, str(e))
            save_state(state)
            continue

    if not args.dry_run:
        notify_space_failures(state)
        notify_space_recovered(recovered)

    if deferred:
        log(f"{deferred} Space(s) deferred until their replay is published")

    if not args.dry_run and not args.no_deliver:
        deliver_pending(state, save_state, log)

    state["last_run"] = now_iso()
    save_state(state)
    log("=" * 60)


if __name__ == "__main__":
    main()
