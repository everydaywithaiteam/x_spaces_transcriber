#!/usr/bin/env python3
"""
deliver.py — Send whatever is still pending for processed episodes.

Both entry points (check_and_run.py for X Spaces, zoom_ingest.py for Zoom .vtt
episodes) write the same entry shape into the same output/state.json, so
delivery is one shared pass rather than a copy in each runner.

Each of the three channels is tracked independently in state, so a failure in
one never blocks the others and never re-runs the expensive pipeline stages:
a send that fails today is retried on the next run of *either* runner.
"""

import re
from datetime import datetime, timezone
from pathlib import Path

from email_notify import send_summary_email
from ticker_alerts import alerts_enabled, send_watchlist_alert
from notion_sync import sync_summary_to_notion


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _extract_date(file_stem: str) -> str:
    # Search anywhere, not just at the end — a disambiguated stem
    # (<stem>-<space_id> from a same-day collision) has the date in the middle.
    match = re.search(r"(\d{4}-\d{2}-\d{2})", file_stem or "")
    return match.group(1) if match else ""


def display_date(entry: dict) -> str:
    """The date shown to readers: when the episode was recorded.

    Falls back to the date baked into the file stem (the processing date) for
    older entries and whenever the source's metadata was unavailable — that's
    the same thing only when an episode is processed the day it aired, which a
    catch-up run over a backlog is precisely not.
    """
    return entry.get("recorded_date") or _extract_date(entry.get("file_stem", ""))


def _meta(entry_id: str, entry: dict) -> dict:
    """The common metadata block the three senders expect.

    `space_id` is the historical key name; for a Zoom episode it carries the
    namespaced episode ID instead, which is what the senders use for subject
    lines and Notion titles.
    """
    return {
        "account": entry.get("account"),
        "speaker": entry.get("speaker"),
        "url": entry.get("url"),
        "space_id": entry_id,
        "date": display_date(entry),
        # Lets the senders label which show an email came from. Entries created
        # before Zoom ingest existed have no `source` and are X Spaces.
        "source": entry.get("source"),
    }


def deliver_pending(state: dict, save_state, log=print) -> dict:
    """Flush pending emails, watchlist alerts, and Notion syncs.

    Covers every processed entry regardless of which runner created it, so a
    Zoom run also retries a Space email that failed earlier, and vice versa.
    Returns {"emails": n, "alerts": n, "notion": n} counting successes.
    """
    sent = {"emails": 0, "alerts": 0, "notion": 0}
    processed = state.get("processed", {})

    pending = [(i, e) for i, e in processed.items() if not e.get("email_sent")]
    if pending:
        log(f"Sending {len(pending)} pending summary email(s)...")
    for entry_id, entry in pending:
        ok = send_summary_email(Path(entry["summary_path"]), _meta(entry_id, entry))
        entry["email_attempts"] = entry.get("email_attempts", 0) + 1
        if ok:
            entry["email_sent"] = True
            entry["email_sent_at"] = now_iso()
            entry["email_last_error"] = None
            sent["emails"] += 1
        else:
            entry["email_last_error"] = "send failed — see log"
        save_state(state)

    # Matches stay recorded in watchlist_matches either way; with alerts off,
    # entries are created with nothing pending, so re-enabling won't replay them.
    pending_watchlist = [(i, e) for i, e in processed.items()
                         if not e.get("watchlist_alert_sent") and e.get("watchlist_matches")] \
                        if alerts_enabled() else []
    if pending_watchlist:
        log(f"Sending {len(pending_watchlist)} pending watchlist alert(s)...")
    for entry_id, entry in pending_watchlist:
        meta = _meta(entry_id, entry)
        meta["summary_path"] = entry["summary_path"]
        ok = send_watchlist_alert(Path(entry["summary_path"]), meta, entry["watchlist_matches"])
        entry["watchlist_alert_attempts"] = entry.get("watchlist_alert_attempts", 0) + 1
        if ok:
            entry["watchlist_alert_sent"] = True
            entry["watchlist_alert_sent_at"] = now_iso()
            entry["watchlist_alert_last_error"] = None
            sent["alerts"] += 1
        else:
            entry["watchlist_alert_last_error"] = "send failed — see log"
        save_state(state)

    pending_notion = [(i, e) for i, e in processed.items() if not e.get("notion_synced")]
    if pending_notion:
        log(f"Syncing {len(pending_notion)} pending summary(ies) to Notion...")
    for entry_id, entry in pending_notion:
        page_id = sync_summary_to_notion(Path(entry["summary_path"]),
                                         _meta(entry_id, entry),
                                         entry.get("tickers_mentioned", []))
        entry["notion_sync_attempts"] = entry.get("notion_sync_attempts", 0) + 1
        if page_id:
            entry["notion_synced"] = True
            entry["notion_synced_at"] = now_iso()
            entry["notion_page_id"] = page_id
            entry["notion_sync_last_error"] = None
            sent["notion"] += 1
        else:
            entry["notion_sync_last_error"] = "sync failed — see log"
        save_state(state)

    return sent
