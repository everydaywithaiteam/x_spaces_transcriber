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
    --model MODEL        Claude model to use (default: claude-opus-4-5)

Environment:
    ANTHROPIC_API_KEY    Required. Get from https://console.anthropic.com
"""

import argparse
import os
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
Every ticker/stock @{speaker} mentioned with context: what they said about it, their stance (bullish/bearish/neutral), \
and any price levels or catalysts they cited.

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
Any stocks, tickers, or assets discussed.

## Notable Quotes
Any particularly striking statements with approximate timestamps.

## Participants
Any speakers or hosts identifiable from context.

---
Transcript:
{transcript}
"""


def summarize(transcript_path: Path, speaker: str = None, space_url: str = None,
              output_path: Path = None, model: str = "claude-opus-4-5") -> Path:
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

    if speaker:
        prompt = SUMMARY_PROMPT.format(speaker=speaker, transcript=transcript)
    else:
        prompt = GENERIC_PROMPT.format(transcript=transcript)

    if output_path is None:
        output_path = transcript_path.with_name(transcript_path.stem + "_summary.md")

    print(f"Sending to Claude ({model})...")
    client = anthropic.Anthropic(api_key=api_key)
    message = client.messages.create(
        model=model,
        max_tokens=8192,
        messages=[{"role": "user", "content": prompt}],
    )

    summary_text = message.content[0].text

    header = "# X Space Summary\n\n"
    if space_url:
        header += f"**URL:** {space_url}\n"
    if speaker:
        header += f"**Focus:** @{speaker}\n"
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
    parser.add_argument("--model", default="claude-opus-4-5",
                        help="Claude model (default: claude-opus-4-5)")
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
