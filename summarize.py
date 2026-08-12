#!/usr/bin/env python3
"""
Summarize an X Space transcript using Claude API.

Reads a transcript file, sends it to Claude, and saves a focused summary
of a specific speaker's contributions (e.g. the show host).

Usage:
    python summarize.py <transcript_file> [options]

Options:
    --speaker HANDLE     Twitter handle to focus on (e.g. stocktalkweekly)
    --space-url URL      Original space URL (for context in the summary)
    --output FILE        Output file path (default: <transcript>_summary.md)
    --model MODEL        Claude model to use (default: claude-opus-5)

Environment:
    ANTHROPIC_API_KEY    Required. Get from https://console.anthropic.com
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

# Load .env file if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())


SUMMARY_PROMPT = """This is a transcript of an X (Twitter) Space called "Stocks on Spaces" \
hosted by @{speaker}. Multiple people are speaking but there are no speaker labels.

Please:
1. Identify which voice is the main host (@{speaker}) — they likely open/close the show, \
say "Stocks on Spaces", introduce guests, and ask most of the questions.
2. Summarize **only their contributions** — their market takes, insights, questions, and conclusions. \
Be thorough and detailed — do not condense or omit points, capture the full substance of what they said.

Format your response as:

## Host Identification
Brief explanation of how you identified @{speaker} in the transcript.

## Overview
4-6 sentence summary covering the main themes, market context, and tone of the episode.

## Market Takes & Insights
Detailed bullet points of @{speaker}'s key views, predictions, and analysis. For each point include \
their reasoning and any supporting data or context they gave, not just the conclusion.

## Trades & Portfolio Moves
Any specific trades, entries, exits, or position changes @{speaker} mentioned, with their rationale.

## Stocks & Tickers Mentioned
List every publicly-traded ticker @{speaker} mentioned. Use EXACTLY this format, one ticker per \
bullet line (if a single discussion covers multiple tickers, e.g. two nuclear plays, give each \
its own line — do not combine tickers on one line):
- **$TICKER** — stance (bullish/bearish/neutral) — one-sentence context: what they said, any \
price levels or catalysts cited
Only use a real, uppercase market ticker after a literal `$`. Skip pre-IPO/private companies, \
ETF-less sector mentions, or anything without an actual public ticker symbol — describe those \
in prose in another section instead if relevant.

## Key Questions & Themes
The main topics @{speaker} drove the conversation around, with a brief description of each discussion.

## Guest Highlights
Key points made by guests that @{speaker} reacted to or built on, with @{speaker}'s response.

## Notable Quotes
3-5 direct quotes (with approximate timestamps) that best capture their perspective or were particularly insightful.

---
Transcript:
{transcript}
"""

GENERIC_PROMPT = """This is a transcript of an X (Twitter) Space audio call.

Please provide a comprehensive summary including:

## Overview
2-3 sentence description of what this Space was about.

## Key Topics Discussed
Bullet points of the main topics covered.

## Key Points & Takeaways
The most important insights and conclusions.

## Stocks & Tickers Mentioned
List every publicly-traded ticker discussed, one per bullet line, in exactly this format:
- **$TICKER** — stance (bullish/bearish/neutral/unclear) — brief context
Only tickers with a real public symbol; skip untickered/private/pre-IPO mentions.

## Notable Quotes
Any particularly striking statements with approximate timestamps.

## Participants
Any speakers or hosts identifiable from context.

---
Transcript:
{transcript}
"""

# Used for transcripts that already carry speaker names (e.g. a Zoom .vtt).
# Because attribution is given rather than inferred, the host-identification
# step is dropped and the model is told to rely on the labels instead of
# guessing. Section headings match SUMMARY_PROMPT exactly — ticker_alerts.py
# and notion_sync.py parse "Stocks & Tickers Mentioned" out of the output.
LABELED_SUMMARY_PROMPT = """This is a transcript of a "{show}" livestream hosted by {speaker}.

Each line is formatted as `[HH:MM:SS] Speaker Name: text`. The speaker labels are \
reliable — attribute statements using them rather than inferring who is talking.

{focus_instruction}

Note on transcript quality: this is machine-generated speech-to-text, so ticker symbols \
and company names are sometimes mis-transcribed (e.g. "in video" for NVIDIA/NVDA, "AMB" \
for AMD, "sales force" for Salesforce). Where context makes the intended ticker \
unambiguous, use the correct symbol. Where it does not, say so rather than guessing.

Format your response as:

## Overview
4-6 sentence summary covering the main themes, market context, and tone of the episode.

## Market Takes & Insights
Detailed bullet points of {speaker}'s key views, predictions, and analysis. For each point \
include their reasoning and any supporting data or context they gave, not just the conclusion.

## Trades & Portfolio Moves
Any specific trades, entries, exits, or position changes {speaker} mentioned, with their rationale.

## Stocks & Tickers Mentioned
List every publicly-traded ticker {speaker} mentioned. Use EXACTLY this format, one ticker per \
bullet line (if a single discussion covers multiple tickers, e.g. two nuclear plays, give each \
its own line — do not combine tickers on one line):
- **$TICKER** — stance (bullish/bearish/neutral) — one-sentence context: what they said, any \
price levels or catalysts cited
Only use a real, uppercase market ticker after a literal `$`. Skip pre-IPO/private companies, \
ETF-less sector mentions, or anything without an actual public ticker symbol — describe those \
in prose in another section instead if relevant.

## Key Questions & Themes
The main topics {speaker} drove the conversation around, with a brief description of each discussion.
{guest_section}
## Notable Quotes
3-5 direct quotes from {speaker} that best capture their perspective or were particularly \
insightful. Use the real timestamp from the start of the line the quote came from.

---
Transcript:
{transcript}
"""


# Filled into LABELED_SUMMARY_PROMPT. A solo broadcast (one presenter, questions
# taken from a text channel rather than voice) has no second speaker to quote, so
# the Guest Highlights section is dropped entirely rather than left to be answered
# with an invented or empty response.
_FOCUS_MULTI = """Summarize **only {speaker}'s contributions** — their market takes, insights, \
questions, and conclusions. Be thorough and detailed — do not condense or omit points, \
capture the full substance of what they said."""

_FOCUS_SOLO = """{speaker} is the only speaker on this recording — it is a solo broadcast, and \
any questions came from a text channel rather than out loud. Summarize the full episode: \
their market takes, insights, and conclusions. Be thorough and detailed — do not condense \
or omit points, capture the full substance of what they said."""

_GUEST_SECTION = """
## Guest Highlights
Key points made by other named speakers that {speaker} reacted to or built on, with {speaker}'s \
response. Use their actual names from the transcript.
"""


# ── Structured output ────────────────────────────────────────────────────────
#
# The markdown summary used to be the source of truth, with ticker_alerts.py
# regex-parsing bullets back out of it. That works until Claude formats a bullet
# slightly differently, at which point a ticker is silently dropped — and a
# silent drop in an alerting system is the worst failure mode available.
#
# So the model now returns JSON against a fixed schema and the markdown is
# rendered from it. Section headings are unchanged, so the email and
# notion_sync.markdown_to_blocks() see exactly what they saw before.
#
# Schema constraints (Claude structured outputs): every object needs
# "additionalProperties": false and must list all its properties in "required".
# Optional sections are expressed as empty string / empty array, not omitted.

_STR_ARRAY = {"type": "array", "items": {"type": "string"}}

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {
        "host_identification": {
            "type": "string",
            "description": "How the host was identified. Empty string when the "
                           "transcript already carries speaker labels.",
        },
        "overview": {"type": "string", "description": "4-6 sentences on themes, market context, tone."},
        "market_takes": dict(_STR_ARRAY, description="Key views and analysis, each with the reasoning given."),
        "trades": dict(_STR_ARRAY, description="Specific trades, entries, exits, position changes with rationale."),
        "tickers": {
            "type": "array",
            "description": "Every publicly-traded ticker mentioned, one entry each.",
            "items": {
                "type": "object",
                "properties": {
                    "symbol": {
                        "type": "string",
                        "description": "Uppercase market ticker, no $ prefix, e.g. NVDA. "
                                       "Real public symbols only — no pre-IPO or private companies.",
                    },
                    "stance": {
                        "type": "string",
                        "enum": ["bullish", "bearish", "neutral", "unclear"],
                    },
                    "context": {
                        "type": "string",
                        "description": "One sentence: what was said, plus any price levels or catalysts.",
                    },
                },
                "required": ["symbol", "stance", "context"],
                "additionalProperties": False,
            },
        },
        "themes": dict(_STR_ARRAY, description="Main topics driven, each briefly described."),
        "guest_highlights": dict(_STR_ARRAY, description="Points from other speakers. Empty for a solo broadcast."),
        "quotes": {
            "type": "array",
            "description": "3-5 direct quotes that best capture the speaker's perspective.",
            "items": {
                "type": "object",
                "properties": {
                    "timestamp": {"type": "string", "description": "HH:MM:SS from the transcript line."},
                    "text": {"type": "string"},
                },
                "required": ["timestamp", "text"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["host_identification", "overview", "market_takes", "trades",
                 "tickers", "themes", "guest_highlights", "quotes"],
    "additionalProperties": False,
}


# Each prompt introduces its markdown section spec with one of these lines.
_FORMAT_BLOCK_RE = re.compile(
    r"(?:Format your response as:|Please provide a comprehensive summary including:)"
    r".*?(?=\n---\nTranscript:)",
    re.DOTALL,
)

_JSON_INSTRUCTION = (
    "Return your analysis using the required JSON structure. Populate every field; use "
    "an empty array or empty string where a section does not apply. For tickers, use "
    "real uppercase market symbols only — skip pre-IPO or private companies and "
    "anything without an actual public ticker."
)


def _strip_markdown_format_block(prompt: str) -> str:
    """Swap a prompt's markdown section spec for the JSON-structure instruction.

    With structured output the schema dictates the shape, so leaving the markdown
    spec in place puts two sets of formatting instructions in competition. The
    framing above it (focus, transcript-quality note) still applies and stays.

    Raises if no known marker is found — a prompt that silently kept its markdown
    spec would quietly degrade every summary, which is worse than a hard failure.
    """
    stripped, count = _FORMAT_BLOCK_RE.subn(_JSON_INSTRUCTION, prompt)
    if not count:
        raise RuntimeError(
            "structured=True but the prompt has no recognised format block to replace — "
            "add its marker line to _FORMAT_BLOCK_RE")
    return stripped


def render_summary_markdown(data: dict) -> str:
    """Render the structured summary as the same markdown shape as before.

    Headings must stay byte-identical to the prompt-authored ones — ticker
    extraction and notion_sync both key off them.
    """
    out = []
    if data.get("host_identification"):
        out += ["## Host Identification", "", data["host_identification"], ""]
    if data.get("overview"):
        out += ["## Overview", "", data["overview"], ""]

    def bullets(heading, items):
        if items:
            out.append(f"## {heading}")
            out.append("")
            out.extend(f"- {item}" for item in items)
            out.append("")

    bullets("Market Takes & Insights", data.get("market_takes"))
    bullets("Trades & Portfolio Moves", data.get("trades"))

    if data.get("tickers"):
        out += ["## Stocks & Tickers Mentioned", ""]
        for t in data["tickers"]:
            symbol = str(t.get("symbol", "")).lstrip("$").upper()
            out.append(f"- **${symbol}** — {t.get('stance', 'unclear')} — {t.get('context', '')}".rstrip(" —"))
        out.append("")

    bullets("Key Questions & Themes", data.get("themes"))
    bullets("Guest Highlights", data.get("guest_highlights"))

    if data.get("quotes"):
        out += ["## Notable Quotes", ""]
        for q in data["quotes"]:
            stamp = q.get("timestamp", "").strip()
            out.append(f"- {'[' + stamp + '] ' if stamp else ''}\"{q.get('text', '').strip()}\"")
        out.append("")

    return "\n".join(out).rstrip() + "\n"


def tickers_from_structured(data: dict) -> list:
    """Ticker symbols in mention order, deduped — no regex, no format drift."""
    seen, symbols = set(), []
    for t in data.get("tickers", []):
        symbol = str(t.get("symbol", "")).lstrip("$").upper().strip()
        if symbol and symbol not in seen:
            seen.add(symbol)
            symbols.append(symbol)
    return symbols


def summarize(transcript_path: Path, speaker: str = None, space_url: str = None,
              output_path: Path = None, model: str = "claude-opus-5",
              labeled: bool = False, show: str = "Stock Talk Weekly",
              solo: bool = False, structured: bool = True) -> Path:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: ANTHROPIC_API_KEY environment variable not set.")
        print("  Get your key at: https://console.anthropic.com")
        print("  Then run: export ANTHROPIC_API_KEY=your_key_here")
        sys.exit(1)

    transcript = transcript_path.read_text(encoding="utf-8")
    print(f"Transcript: {len(transcript):,} chars, {transcript.count(chr(10))+1} lines")

    # Truncate if extremely long (safety net)
    max_chars = 600_000
    if len(transcript) > max_chars:
        print(f"Truncating transcript to {max_chars:,} chars...")
        transcript = transcript[:max_chars] + "\n\n[... transcript truncated ...]"

    if labeled and speaker:
        focus = (_FOCUS_SOLO if solo else _FOCUS_MULTI).format(speaker=speaker)
        guest = "" if solo else _GUEST_SECTION.format(speaker=speaker)
        prompt = LABELED_SUMMARY_PROMPT.format(
            speaker=speaker, show=show, transcript=transcript,
            focus_instruction=focus, guest_section=guest)
    elif speaker:
        prompt = SUMMARY_PROMPT.format(speaker=speaker, transcript=transcript)
    else:
        prompt = GENERIC_PROMPT.format(transcript=transcript)

    if structured:
        prompt = _strip_markdown_format_block(prompt)

    if output_path is None:
        output_path = transcript_path.with_name(transcript_path.stem + "_summary.md")

    print(f"Sending to Claude ({model}{', structured' if structured else ''})...")
    client = anthropic.Anthropic(api_key=api_key)
    request = {
        "model": model,
        # Generous: a long episode's structured summary plus JSON overhead. The
        # old 8192 was close enough to real output length to risk truncation.
        "max_tokens": 16000,
        "messages": [{"role": "user", "content": prompt}],
    }
    if structured:
        request["output_config"] = {"format": {"type": "json_schema", "schema": SUMMARY_SCHEMA}}
    message = client.messages.create(**request)

    if message.stop_reason == "refusal":
        raise RuntimeError("Claude declined this request (stop_reason=refusal)")
    if message.stop_reason == "max_tokens":
        raise RuntimeError(
            "Summary hit the max_tokens ceiling and would be truncated — "
            "raise max_tokens or shorten the transcript")

    raw = next((b.text for b in message.content if b.type == "text"), "")

    if structured:
        data = json.loads(raw)
        summary_text = render_summary_markdown(data)
        # Sidecar JSON: the source of truth for tickers, so downstream never has
        # to parse markdown back out again.
        json_path = output_path.with_suffix(".json")
        json_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
        print(f"Structured summary saved to: {json_path} "
              f"({len(data.get('tickers', []))} tickers)")
    else:
        summary_text = raw

    # Labelled transcripts come from named-speaker sources (Zoom), where the
    # speaker is a person's name rather than an X handle — so no "@" prefix.
    header = f"# {show} Summary\n\n" if labeled else "# X Space Summary\n\n"
    if space_url:
        header += f"**URL:** {space_url}\n"
    if speaker:
        header += f"**Focus:** {speaker}\n" if labeled else f"**Focus:** @{speaker}\n"
    header += f"**Transcript:** {transcript_path.name}\n\n"

    output_path.write_text(header + summary_text, encoding="utf-8")
    print(f"Summary saved to: {output_path}")
    return output_path


def main():
    parser = argparse.ArgumentParser(description="Summarize an X Space transcript with Claude")
    parser.add_argument("transcript", help="Path to transcript .txt file")
    parser.add_argument("--speaker", metavar="HANDLE",
                        help="Twitter handle of speaker to focus on (e.g. stocktalkweekly)")
    parser.add_argument("--space-url", metavar="URL", help="Original X Space URL")
    parser.add_argument("--output", metavar="FILE", help="Output file path")
    parser.add_argument("--model", default="claude-opus-5",
                        help="Claude model (default: claude-opus-5)")
    args = parser.parse_args()

    transcript_path = Path(args.transcript)
    if not transcript_path.exists():
        print(f"ERROR: Transcript file not found: {transcript_path}")
        sys.exit(1)

    output_path = Path(args.output) if args.output else None
    result = summarize(transcript_path, args.speaker, args.space_url, output_path, args.model)
    print(f"\n✓ Done! Summary: {result}")


if __name__ == "__main__":
    main()
