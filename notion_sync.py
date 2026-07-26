#!/usr/bin/env python3
"""
Notion sync for X Space summaries.

Pushes each generated summary into a Notion database as one page: title,
date, account/speaker, tickers mentioned, a link back to the Space, and the
full summary as page body blocks — so summaries become searchable/filterable
instead of living only as markdown files in output/.

Environment:
    NOTION_TOKEN         Internal integration token from notion.so/my-integrations
    NOTION_DATABASE_ID   Database ID (from the database's URL), shared with the integration

Setup (one-time, see README): create an integration, create a database with
properties named exactly Name (title), Date (date), Account (rich_text),
Speaker (rich_text), URL (url), Tickers (multi_select), then share the
database with the integration.
"""

import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE_DIR = Path(__file__).parent

# Load .env file if present
_env_file = BASE_DIR / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

NOTION_API = "https://api.notion.com/v1"
NOTION_VERSION = "2022-06-28"
NOTION_TOKEN = os.environ.get("NOTION_TOKEN")
NOTION_DATABASE_ID = os.environ.get("NOTION_DATABASE_ID")

_RICH_TEXT_LIMIT = 2000
_BLOCKS_PER_REQUEST = 100


def _chunk_text(text: str, size: int = _RICH_TEXT_LIMIT) -> list:
    if not text:
        return [""]
    return [text[i:i + size] for i in range(0, len(text), size)]


def _parse_inline(text: str) -> list:
    """Split text on **bold** markers into Notion rich_text runs (2000-char safe)."""
    runs = []
    for part in re.split(r"(\*\*.*?\*\*)", text):
        if not part:
            continue
        bold = part.startswith("**") and part.endswith("**") and len(part) > 4
        content = part[2:-2] if bold else part
        for chunk in _chunk_text(content):
            if not chunk:
                continue
            run = {"type": "text", "text": {"content": chunk}}
            if bold:
                run["annotations"] = {"bold": True}
            runs.append(run)
    return runs or [{"type": "text", "text": {"content": ""}}]


def _heading_block(text: str) -> dict:
    return {"object": "block", "type": "heading_2", "heading_2": {"rich_text": _parse_inline(text)}}


def _bullet_block(text: str) -> dict:
    return {"object": "block", "type": "bulleted_list_item",
            "bulleted_list_item": {"rich_text": _parse_inline(text)}}


def _paragraph_block(text: str) -> dict:
    return {"object": "block", "type": "paragraph", "paragraph": {"rich_text": _parse_inline(text)}}


def markdown_to_blocks(md_text: str) -> list:
    """Convert a generated _summary.md into Notion blocks.

    Skips the "# X Space Summary" / **URL:**/**Focus:**/**Transcript:** preamble
    (that data becomes page properties instead) and converts the fixed shape of
    the rest of the file: "## heading" -> heading_2, "- bullet" -> bulleted_list_item,
    everything else -> paragraph.
    """
    blocks = []
    started = False
    paragraph_buf = []

    def flush_paragraph():
        if paragraph_buf:
            blocks.append(_paragraph_block(" ".join(paragraph_buf)))
            paragraph_buf.clear()

    for line in md_text.splitlines():
        stripped = line.strip()

        if stripped.startswith("## "):
            started = True
            flush_paragraph()
            blocks.append(_heading_block(stripped[3:].strip()))
            continue

        if not started:
            continue  # preamble before the first ## heading

        if stripped.startswith("# "):
            continue  # stray top-level heading

        if stripped.startswith("- ") or stripped.startswith("* "):
            flush_paragraph()
            blocks.append(_bullet_block(stripped[2:].strip()))
            continue

        if stripped == "" or stripped == "---":
            flush_paragraph()
            continue

        paragraph_buf.append(stripped)

    flush_paragraph()
    return blocks


def _notion_request(method: str, path: str, payload: dict) -> dict:
    req = urllib.request.Request(
        f"{NOTION_API}{path}",
        data=json.dumps(payload).encode("utf-8"),
        method=method,
        headers={
            "Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sync_summary_to_notion(summary_path: Path, space_meta: dict, tickers: list = None):
    """Push a summary into the Notion database as a new page.

    space_meta: {"account", "speaker", "url", "space_id", "date"}
    Never raises — returns the created page ID on success, None on skip/failure (logged).
    """
    if not NOTION_TOKEN or not NOTION_DATABASE_ID:
        print("notion_sync: Notion not configured (NOTION_TOKEN/NOTION_DATABASE_ID) — skipping")
        return None

    tickers = tickers or []

    try:
        md_text = summary_path.read_text(encoding="utf-8")
        blocks = markdown_to_blocks(md_text)

        account = space_meta.get("account", "")
        speaker = space_meta.get("speaker", "")
        date = space_meta.get("date", "")
        url = space_meta.get("url", "")

        properties = {
            "Name": {"title": [{"text": {"content": f"{account} — {date}".strip(" —")}}]},
            "Account": {"rich_text": [{"text": {"content": account}}]},
            "Speaker": {"rich_text": [{"text": {"content": speaker}}]},
        }
        if date:
            properties["Date"] = {"date": {"start": date}}
        if url:
            properties["URL"] = {"url": url}
        if tickers:
            properties["Tickers"] = {"multi_select": [{"name": t} for t in tickers]}

        result = _notion_request("POST", "/pages", {
            "parent": {"database_id": NOTION_DATABASE_ID},
            "properties": properties,
            "children": blocks[:_BLOCKS_PER_REQUEST],
        })
        page_id = result["id"]

        remaining = blocks[_BLOCKS_PER_REQUEST:]
        for i in range(0, len(remaining), _BLOCKS_PER_REQUEST):
            batch = remaining[i:i + _BLOCKS_PER_REQUEST]
            _notion_request("PATCH", f"/blocks/{page_id}/children", {"children": batch})
            if i + _BLOCKS_PER_REQUEST < len(remaining):
                time.sleep(0.34)  # stay under Notion's ~3 req/sec limit

        print(f"notion_sync: synced {summary_path.name} -> page {page_id}")
        return page_id

    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"notion_sync: sync failed for {space_meta.get('space_id')} — HTTP {e.code}: {body}")
        return None
    except Exception as e:
        print(f"notion_sync: sync failed for {space_meta.get('space_id')} — {e}")
        return None
