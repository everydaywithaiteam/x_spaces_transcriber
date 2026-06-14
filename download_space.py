#!/usr/bin/env python3
"""
X Spaces Downloader, Transcriber, Diarizer, and Summarizer

Usage:
    python download_space.py <space_url> [options]

Options:
    --model MODEL               faster-whisper model: tiny, base, small, medium, large (default: base)
    --cookies-from-browser BROWSER  Use cookies from browser: chrome, firefox, safari
    --cookies FILE              Path to cookies.txt file
    --diarize                   Enable speaker diarization (labels who is speaking)
    --hf-token TOKEN            HuggingFace token for pyannote diarization model
    --speaker HANDLE            Twitter handle of speaker to focus summary on (e.g. stocktalkweekly)
    --skip-download             Skip download, use existing audio file
    --skip-transcribe           Skip transcription, use existing transcript
    --output-dir DIR            Output directory (default: ./output)

HuggingFace setup (one-time, free):
    1. Create account at huggingface.co
    2. Accept pyannote/speaker-diarization-3.1 model terms at:
       https://hf.co/pyannote/speaker-diarization-3.1
    3. Create access token at https://hf.co/settings/tokens
    4. Pass via --hf-token or set HF_TOKEN env var
"""

import argparse
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


def extract_space_id(url: str) -> str:
    match = re.search(r"/spaces/([A-Za-z0-9]+)", url)
    if match:
        return match.group(1)
    return re.sub(r"[^A-Za-z0-9_-]", "_", url)[:40]


def download_space(url: str, output_dir: Path, space_id: str, cookies_from_browser: str = None, cookies_file: str = None) -> Path:
    import yt_dlp

    ydl_opts = {
        "format": "bestaudio/best",
        "outtmpl": str(output_dir / f"{space_id}.%(ext)s"),
        "quiet": False,
        "no_warnings": False,
    }

    if cookies_from_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_from_browser,)
    elif cookies_file:
        ydl_opts["cookiefile"] = cookies_file

    print(f"\n[1/4] Downloading Space {space_id}...")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        ext = info.get("ext", "m4a")
        audio_path = output_dir / f"{space_id}.{ext}"

    print(f"      Saved to: {audio_path}")
    return audio_path


def transcribe_audio(audio_path: Path, model_size: str = "base") -> list:
    """Returns list of segment dicts: {start, end, text}"""
    from faster_whisper import WhisperModel

    print(f"\n[2/4] Transcribing audio (model={model_size}, this may take a while)...")
    model = WhisperModel(model_size, device="cpu", compute_type="int8")
    segments, info = model.transcribe(str(audio_path), beam_size=5)
    print(f"      Detected language: {info.language} (probability {info.language_probability:.2f})")

    result = []
    for seg in segments:
        result.append({"start": seg.start, "end": seg.end, "text": seg.text.strip()})
        print(f"  [{seg.start:.1f}s - {seg.end:.1f}s] {seg.text.strip()}")

    print(f"      Transcribed {len(result)} segments.")
    return result


def convert_to_wav(audio_path: Path) -> Path:
    """Convert audio to 16kHz mono WAV (required by pyannote)."""
    import subprocess
    wav_path = audio_path.with_suffix(".wav")
    if wav_path.exists():
        print(f"      WAV already exists: {wav_path}")
        return wav_path
    print(f"      Converting to WAV: {wav_path}")
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(audio_path), "-ar", "16000", "-ac", "1", str(wav_path)],
        check=True, capture_output=True,
    )
    return wav_path


def diarize_audio(audio_path: Path, hf_token: str) -> list:
    """Returns list of diarization dicts: {start, end, speaker}"""
    from pyannote.audio import Pipeline
    from huggingface_hub import login
    import torch

    print(f"\n[3/4] Running speaker diarization (this may take a while)...")
    wav_path = convert_to_wav(audio_path)

    login(token=hf_token)
    pipeline = Pipeline.from_pretrained("pyannote/speaker-diarization-3.1", use_auth_token=hf_token)

    diarization = pipeline(str(wav_path))

    segments = []
    for turn, _, speaker in diarization.itertracks(yield_label=True):
        segments.append({"start": turn.start, "end": turn.end, "speaker": speaker})

    print(f"      Found {len(set(s['speaker'] for s in segments))} speakers, {len(segments)} segments.")
    return segments


def merge_transcript_with_diarization(transcript_segments: list, diarization_segments: list) -> list:
    """
    Assign a speaker label to each transcript segment by finding the
    diarization segment with the most overlap.
    """
    merged = []
    for tseg in transcript_segments:
        t_start, t_end = tseg["start"], tseg["end"]
        t_mid = (t_start + t_end) / 2

        best_speaker = "UNKNOWN"
        best_overlap = 0.0

        for dseg in diarization_segments:
            d_start, d_end = dseg["start"], dseg["end"]
            overlap = max(0, min(t_end, d_end) - max(t_start, d_start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = dseg["speaker"]

        # Fallback: nearest diarization segment by midpoint
        if best_overlap == 0:
            best_speaker = min(
                diarization_segments,
                key=lambda d: abs((d["start"] + d["end"]) / 2 - t_mid),
            )["speaker"]

        merged.append({
            "start": t_start,
            "end": t_end,
            "speaker": best_speaker,
            "text": tseg["text"],
        })

    return merged


def format_transcript(segments: list, diarized: bool = False) -> str:
    lines = []
    for seg in segments:
        if diarized:
            lines.append(f"[{seg['speaker']} | {seg['start']:.1f}s-{seg['end']:.1f}s] {seg['text']}")
        else:
            lines.append(f"[{seg['start']:.1f}s - {seg['end']:.1f}s] {seg['text']}")
    return "\n".join(lines)


def summarize_transcript(transcript: str, summary_path: Path, space_url: str, speaker_handle: str = None) -> str:
    import anthropic

    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("\n[4/4] ERROR: ANTHROPIC_API_KEY environment variable not set. Skipping summary.")
        print("      Set it with: export ANTHROPIC_API_KEY=your_key_here")
        return ""

    print("\n[4/4] Summarizing transcript with Claude...")

    max_chars = 500_000
    if len(transcript) > max_chars:
        print(f"      Transcript is large ({len(transcript)} chars), truncating to {max_chars} chars...")
        transcript = transcript[:max_chars] + "\n\n[... transcript truncated ...]"

    client = anthropic.Anthropic(api_key=api_key)

    if speaker_handle:
        speaker_focus = f"""
The transcript has multiple labeled speakers (SPEAKER_0, SPEAKER_1, etc.).
Your first task is to **identify which speaker label corresponds to @{speaker_handle}**.
They are likely the host — look for clues like them saying "Stocks on Spaces", "Stock Talk Weekly",
introducing guests, asking most of the questions, or opening/closing the show.

Once identified, summarize **only their contributions** — their questions, market takes,
insights, and conclusions. Clearly state at the top which speaker label you identified as @{speaker_handle}.
"""
    else:
        speaker_focus = "Summarize the full discussion across all speakers."

    prompt = f"""You are summarizing an X (Twitter) Space audio call transcript.

Space URL: {space_url}
{speaker_focus}

Transcript (with speaker labels and timestamps):
{transcript}

Please provide:

## Speaker Identification
(Only if diarized) Which SPEAKER_N label is @{speaker_handle if speaker_handle else 'the main host'}, and why.

## Overview
2-3 sentence description of what this Space was about.

## Key Topics Discussed
Bullet points of the main topics covered{"by @" + speaker_handle if speaker_handle else ""}.

## Key Points & Takeaways
The most important insights, market takes, or conclusions.

## Notable Quotes or Moments
Striking statements with approximate timestamps.

## Participants
Any other speakers or guests mentioned.
"""

    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        messages=[{"role": "user", "content": prompt}],
    )

    summary = message.content[0].text
    header = f"# Summary of X Space\n\n**URL:** {space_url}\n"
    if speaker_handle:
        header += f"**Focus:** @{speaker_handle}\n"
    summary_path.write_text(header + "\n" + summary, encoding="utf-8")
    print(f"      Summary saved to: {summary_path}")
    return summary


def main():
    parser = argparse.ArgumentParser(description="Download, transcribe, diarize, and summarize an X Space")
    parser.add_argument("url", help="X Space URL (e.g. https://x.com/i/spaces/...)")
    parser.add_argument("--model", default="base", choices=["tiny", "base", "small", "medium", "large"],
                        help="Whisper model size (default: base)")
    parser.add_argument("--cookies-from-browser", metavar="BROWSER",
                        help="Load cookies from browser: chrome, firefox, safari")
    parser.add_argument("--cookies", metavar="FILE", help="Path to cookies.txt file")
    parser.add_argument("--diarize", action="store_true",
                        help="Enable speaker diarization (labels who is speaking)")
    parser.add_argument("--hf-token", metavar="TOKEN",
                        help="HuggingFace token for pyannote diarization model (or set HF_TOKEN env var)")
    parser.add_argument("--speaker", metavar="HANDLE",
                        help="Twitter handle of speaker to focus summary on (e.g. stocktalkweekly)")
    parser.add_argument("--skip-download", action="store_true",
                        help="Skip download step (use existing audio file)")
    parser.add_argument("--skip-transcribe", action="store_true",
                        help="Skip transcription/diarization step (use existing transcript)")
    parser.add_argument("--output-dir", default="output", help="Output directory (default: ./output)")
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    space_id = extract_space_id(args.url)
    print(f"Space ID: {space_id}")
    print(f"Output dir: {output_dir.resolve()}")

    hf_token = args.hf_token or os.environ.get("HF_TOKEN")
    if args.diarize and not hf_token:
        print("ERROR: --diarize requires a HuggingFace token.")
        print("  Pass via --hf-token or set HF_TOKEN env var.")
        print("  See: https://hf.co/pyannote/speaker-diarization-3.1")
        sys.exit(1)

    # ── Step 1: Download ──────────────────────────────────────────────────────
    audio_path = None
    if args.skip_download or args.skip_transcribe:
        candidates = list(output_dir.glob(f"{space_id}.*"))
        audio_candidates = [f for f in candidates if f.suffix in (".m4a", ".mp3", ".aac", ".opus", ".webm", ".mp4")]
        if audio_candidates:
            audio_path = audio_candidates[0]
            print(f"\n[1/4] Skipping download, using: {audio_path}")
        elif args.skip_download and not args.skip_transcribe:
            print(f"ERROR: --skip-download set but no audio file found in {output_dir} for space {space_id}")
            sys.exit(1)
    else:
        audio_path = download_space(args.url, output_dir, space_id, args.cookies_from_browser, args.cookies)

    # ── Step 2 & 3: Transcribe + Diarize ────────────────────────────────────
    diarized_path = output_dir / f"{space_id}_diarized.txt"
    plain_path = output_dir / f"{space_id}.txt"

    if args.skip_transcribe:
        # Load existing plain transcript and parse back into segments for diarization
        if plain_path.exists():
            print(f"\n[2/4] Skipping transcription, using: {plain_path}")
            raw_text = plain_path.read_text(encoding="utf-8")
            # Parse "[start - end] text" lines back into segment dicts
            transcript_segments = []
            for line in raw_text.splitlines():
                m = re.match(r"\[(\d+\.?\d*)s\s*-\s*(\d+\.?\d*)s\]\s*(.*)", line)
                if m:
                    transcript_segments.append({"start": float(m.group(1)), "end": float(m.group(2)), "text": m.group(3)})
        else:
            print(f"ERROR: --skip-transcribe set but {plain_path} not found")
            sys.exit(1)
    else:
        transcript_segments = transcribe_audio(audio_path, args.model)

    if args.diarize:
        diar_segments = diarize_audio(audio_path, hf_token)
        merged = merge_transcript_with_diarization(transcript_segments, diar_segments)
        transcript_text = format_transcript(merged, diarized=True)
        diarized_path.write_text(transcript_text, encoding="utf-8")
        print(f"      Diarized transcript saved to: {diarized_path}")
        transcript_out = diarized_path
    else:
        if args.skip_transcribe:
            # Already have plain transcript, nothing to re-save
            transcript_text = plain_path.read_text(encoding="utf-8")
        else:
            print(f"\n[3/4] Skipping diarization (use --diarize to enable).")
            transcript_text = format_transcript(transcript_segments, diarized=False)
            plain_path.write_text(transcript_text, encoding="utf-8")
            print(f"      Transcript saved to: {plain_path}")
        transcript_out = plain_path

    # ── Step 4: Summarize ────────────────────────────────────────────────────
    summary_path = output_dir / f"{space_id}_summary.md"
    summary = summarize_transcript(transcript_text, summary_path, args.url, args.speaker)

    print("\n✓ Done!")
    print(f"  Audio:      {audio_path}")
    print(f"  Transcript: {transcript_out}")
    if summary:
        print(f"  Summary:    {summary_path}")


if __name__ == "__main__":
    main()
