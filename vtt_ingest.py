#!/usr/bin/env python3
"""
vtt_ingest.py — Turn a Zoom WebVTT transcript into a speaker-labelled transcript.

Zoom cloud recordings expose an "Audio Transcript" (.vtt) alongside the video.
Using it skips the two most expensive pipeline stages entirely: there is no
multi-GB video to download and no Whisper pass to run. It also arrives with
speaker names already attached, so the summarizer never has to guess who the
host is.

Zoom emits one cue per breath, which is noisy and token-hungry:

    1
    00:00:04.130 --> 00:00:06.450
    Danny Rodriguez: all right good morning

    2
    00:00:06.450 --> 00:00:08.900
    Danny Rodriguez: welcome back to the stream

Consecutive cues from the same speaker are merged into one line, stamped with
the start time of the run, which is what the summary prompt cites quotes from:

    [00:00:04] Danny Rodriguez: all right good morning welcome back to the stream

Usage:
    python vtt_ingest.py <file.vtt> [--output FILE] [--speakers]
"""

import argparse
import re
import sys
from pathlib import Path

# "00:01:02.345" or "01:02.345" — Zoom omits the hour on short recordings.
_TIMESTAMP = r"(?:\d{1,3}:)?\d{1,2}:\d{2}[.,]\d{1,3}"
_CUE_TIMING = re.compile(rf"^\s*({_TIMESTAMP})\s*-->\s*({_TIMESTAMP})")

# "Danny Rodriguez: text" — but not "https: //..." or a bare clock reference.
# Speaker names are short, so cap the prefix length rather than trying to
# enumerate what a name can contain.
_SPEAKER_LINE = re.compile(r"^\s*([^:]{1,60}?)\s*:\s+(.*)$")

# WebVTT inline markup: <v Speaker>, <c.classname>, <00:00:01.000>, &nbsp; etc.
_TAG = re.compile(r"<[^>]+>")
_VOICE_SPAN = re.compile(r"^\s*<v\s+([^>]+)>\s*(.*)$", re.I)


def _to_seconds(stamp: str) -> float:
    """Parse a WebVTT timestamp into seconds. Accepts MM:SS.mmm and HH:MM:SS.mmm."""
    stamp = stamp.replace(",", ".")
    parts = stamp.split(":")
    seconds = float(parts[-1])
    if len(parts) >= 2:
        seconds += int(parts[-2]) * 60
    if len(parts) >= 3:
        seconds += int(parts[-3]) * 3600
    return seconds


def _fmt(seconds: float) -> str:
    total = int(seconds)
    return f"{total // 3600:02d}:{(total % 3600) // 60:02d}:{total % 60:02d}"


def _clean(text: str) -> str:
    text = _TAG.sub("", text)
    text = text.replace("&nbsp;", " ").replace("&amp;", "&")
    text = text.replace("&lt;", "<").replace("&gt;", ">")
    return " ".join(text.split())


def parse_cues(vtt_text: str) -> list:
    """Extract [{start, end, speaker, text}] from WebVTT source.

    Cue payloads can span several lines; NOTE/STYLE/REGION blocks and the
    optional numeric cue identifier are skipped. `speaker` is None when the
    line carries no "Name:" prefix.
    """
    # Strip a BOM and normalise line endings before splitting into blocks.
    vtt_text = vtt_text.lstrip("﻿").replace("\r\n", "\n").replace("\r", "\n")

    cues = []
    for block in re.split(r"\n\s*\n", vtt_text):
        lines = [ln for ln in block.split("\n") if ln.strip()]
        if not lines:
            continue
        # Metadata blocks carry no timing line and must not be treated as speech.
        if lines[0].strip().upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
            continue

        timing_idx = next((i for i, ln in enumerate(lines) if _CUE_TIMING.match(ln)), None)
        if timing_idx is None:
            continue

        match = _CUE_TIMING.match(lines[timing_idx])
        start, end = _to_seconds(match.group(1)), _to_seconds(match.group(2))

        payload = " ".join(lines[timing_idx + 1:]).strip()
        if not payload:
            continue

        speaker = None
        voice = _VOICE_SPAN.match(payload)
        if voice:
            # Standard WebVTT voice span: <v Speaker Name>text
            speaker, payload = voice.group(1).strip(), voice.group(2)
        else:
            # Zoom's own convention: "Speaker Name: text"
            named = _SPEAKER_LINE.match(payload)
            if named and not named.group(1).lower().startswith(("http", "https")):
                speaker, payload = named.group(1).strip(), named.group(2)

        text = _clean(payload)
        if text:
            cues.append({"start": start, "end": end, "speaker": speaker, "text": text})

    return cues


def merge_cues(cues: list, max_gap: float = 30.0, max_turn: float = 60.0) -> list:
    """Collapse consecutive cues from one speaker into single turns.

    A run is broken when the speaker changes, when more than `max_gap` seconds
    of silence pass, or once the run has covered `max_turn` seconds.

    That last cap matters more than it looks. On a solo livestream the speaker
    never changes and rarely pauses, so speaker/gap rules alone merge an entire
    hour into one line carrying a single timestamp — leaving the summarizer
    nothing to cite when it quotes. Capping run length keeps a timestamp
    roughly every minute, at a cost of ~25 bytes per turn.
    """
    turns = []
    for cue in cues:
        current = turns[-1] if turns else None
        if (current
                and current["speaker"] == cue["speaker"]
                and cue["start"] - current["end"] <= max_gap
                and cue["end"] - current["start"] <= max_turn):
            current["text"] += " " + cue["text"]
            current["end"] = cue["end"]
        else:
            turns.append(dict(cue))
    return turns


def speaker_stats(cues: list) -> list:
    """[(speaker, seconds, cue_count)] ordered by speaking time, descending.

    The dominant speaker is almost always the host, which makes this a
    reasonable default for --speaker without hardcoding a name.
    """
    totals = {}
    for cue in cues:
        name = cue["speaker"] or "(unlabelled)"
        seconds, count = totals.get(name, (0.0, 0))
        totals[name] = (seconds + max(0.0, cue["end"] - cue["start"]), count + 1)
    return sorted(((n, s, c) for n, (s, c) in totals.items()), key=lambda r: -r[1])


def render(turns: list) -> str:
    lines = []
    for turn in turns:
        who = f"{turn['speaker']}: " if turn["speaker"] else ""
        lines.append(f"[{_fmt(turn['start'])}] {who}{turn['text']}")
    return "\n".join(lines)


def vtt_to_transcript(vtt_path: Path, output_path: Path = None) -> Path:
    """Convert a .vtt file to a speaker-labelled .txt transcript."""
    cues = parse_cues(vtt_path.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        raise ValueError(f"No cues found in {vtt_path.name} — is this a WebVTT file?")

    turns = merge_cues(cues)
    if output_path is None:
        output_path = vtt_path.with_suffix(".txt")
    output_path.write_text(render(turns), encoding="utf-8")
    return output_path


def has_speaker_labels(cues: list) -> bool:
    """True when most cues are attributed — i.e. the summary can trust names."""
    if not cues:
        return False
    labelled = sum(1 for c in cues if c["speaker"])
    return labelled / len(cues) >= 0.5


def main():
    parser = argparse.ArgumentParser(description="Convert a Zoom .vtt transcript to labelled text")
    parser.add_argument("vtt", help="Path to the .vtt file")
    parser.add_argument("--output", metavar="FILE", help="Output .txt path")
    parser.add_argument("--speakers", action="store_true", help="Print speaking-time breakdown and exit")
    args = parser.parse_args()

    vtt_path = Path(args.vtt)
    if not vtt_path.exists():
        print(f"ERROR: file not found: {vtt_path}")
        sys.exit(1)

    cues = parse_cues(vtt_path.read_text(encoding="utf-8", errors="replace"))
    if not cues:
        print(f"ERROR: no cues found in {vtt_path.name} — is this a WebVTT file?")
        sys.exit(1)

    if args.speakers:
        total = sum(max(0.0, c["end"] - c["start"]) for c in cues) or 1.0
        print(f"{len(cues)} cues, {_fmt(cues[-1]['end'])} long, "
              f"labels: {'yes' if has_speaker_labels(cues) else 'NO'}\n")
        for name, seconds, count in speaker_stats(cues):
            print(f"  {name:<32} {_fmt(seconds)}  {seconds / total * 100:5.1f}%  {count:>5} cues")
        return

    turns = merge_cues(cues)
    output_path = Path(args.output) if args.output else vtt_path.with_suffix(".txt")
    output_path.write_text(render(turns), encoding="utf-8")
    print(f"{len(cues)} cues → {len(turns)} turns")
    print(f"Transcript saved to: {output_path}")


if __name__ == "__main__":
    main()
