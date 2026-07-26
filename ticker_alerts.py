#!/usr/bin/env python3
"""
Ticker watchlist alerts for X Space summaries.

Extracts the tickers Claude found in a summary's "Stocks & Tickers Mentioned"
section, checks them against a personal watchlist file, and sends a short
priority email when there's a match — separate from the full summary email.

Environment:
    WATCHLIST_FILE       Path to a plain text watchlist, one ticker per line
                         (default: watchlist.txt, relative to this file's dir).
                         Lines starting with # are ignored. Missing/empty file
                         means alerts are skipped — not an error.
"""

import os
import re
from pathlib import Path

import email_notify

BASE_DIR = Path(__file__).parent

# Load .env file if present
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

_TICKERS_SECTION_RE = re.compile(
    r"^##\s*Stocks\s*&\s*Tickers Mentioned\s*$(.*?)(?=^##\s|\Z)",
    re.MULTILINE | re.DOTALL,
)
_TICKER_BULLET_RE = re.compile(r"^\s*-\s*\*\*\$([A-Z]{1,6})\*\*", re.MULTILINE)


def extract_tickers(summary_text: str) -> list:
    """Return the list of tickers found in the summary's tickers section.

    Order-preserving, deduplicated. Returns [] if the section is missing or
    has no `- **$TICKER**` bullets (e.g. an older summary generated before the
    prompt required this format).
    """
    section_match = _TICKERS_SECTION_RE.search(summary_text)
    if not section_match:
        return []

    tickers = []
    seen = set()
    for ticker in _TICKER_BULLET_RE.findall(section_match.group(1)):
        if ticker not in seen:
            seen.add(ticker)
            tickers.append(ticker)
    return tickers


def load_watchlist() -> list:
    """Read the watchlist file fresh (no caching) — [] if unconfigured/missing."""
    watchlist_file = os.environ.get("WATCHLIST_FILE", "watchlist.txt")
    path = Path(watchlist_file)
    if not path.is_absolute():
        path = BASE_DIR / path

    if not path.exists():
        return []

    tickers = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip().upper()
        if line and not line.startswith("#"):
            tickers.append(line.lstrip("$"))
    return tickers


def match_watchlist(tickers_mentioned: list, watchlist: list = None) -> list:
    """Order-preserving intersection of tickers_mentioned and the watchlist."""
    watchlist = load_watchlist() if watchlist is None else watchlist
    watchlist_set = set(watchlist)
    return [t for t in tickers_mentioned if t in watchlist_set]


_SUBJECT_TICKER_LIMIT = 3


def _subject_tickers(matched_tickers: list) -> str:
    """Cap the subject line's ticker list so it doesn't run on for a big match set."""
    shown = matched_tickers[:_SUBJECT_TICKER_LIMIT]
    text = ", ".join(f"${t}" for t in shown)
    remaining = len(matched_tickers) - len(shown)
    if remaining > 0:
        text += f" and {remaining} more"
    return text


def send_watchlist_alert(summary_path: Path, space_meta: dict, matched_tickers: list) -> bool:
    """Send a short priority email for a watchlist match.

    space_meta: {"account", "speaker", "url", "space_id", "date"}
    Never raises — returns True on success, False on any failure (logged).
    """
    if not matched_tickers:
        return False

    try:
        account = space_meta.get("account", "")
        date = space_meta.get("date", "")
        url = space_meta.get("url", "")
        summary_link = space_meta.get("summary_path", str(summary_path))

        tickers_str = ", ".join(f"${t}" for t in matched_tickers)
        subject = f"\U0001F514 {_subject_tickers(matched_tickers)} mentioned — {account} {date}".strip()

        plain_text = (
            f"{tickers_str} came up in today's Space from {account}.\n\n"
            f"Space: {url}\n"
            f"Full summary: {summary_link}\n"
        )
        html = f"""\
<html>
<body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:700px;
             margin:0 auto;padding:20px;color:#1a1a1a;line-height:1.55;">
  <h1 style="font-size:18px;">\U0001F514 {tickers_str} mentioned — {account} {date}</h1>
  <p>{tickers_str} came up in today's Space from {account}.</p>
  <p><a href="{url}">Space link</a></p>
  <p>Full summary: {summary_link}</p>
</body>
</html>"""

        return email_notify.send_email(subject, plain_text, html)

    except Exception as e:
        print(f"ticker_alerts: alert send failed for {space_meta.get('space_id')} — {e}")
        return False
