#!/usr/bin/env python3
"""
cleanup_output.py — Reclaim disk from intermediates the pipeline no longer needs.

The pipeline keeps a summary (~16 KB) and a transcript (~150 KB) per episode.
It also used to keep the downloaded audio, which is 60-100 MB per Space and is
never read again once the transcript exists. Nothing ever deleted it, so
output/ reached 2.9 GB of which 2.9 GB was audio.

New runs now discard audio as they go (see discard_audio in pipeline.py). This
sweeps up what accumulated before that, and stays around for the occasional
manual pass.

What it removes:
  - output/*.{m4a,mp3,aac,opus,webm,mp4,wav} that have a matching .txt
  - transcripts_in/processed/*.vtt  (superseded by output/<stem>.txt)

What it keeps:
  - every _summary.md, _summary.json, .txt and _run.json
  - any audio with no transcript beside it — that audio is still the only copy
    of the episode, so it is reported and left alone rather than guessed at

Dry-run by default. It deletes gigabytes irreversibly, so you have to ask:

    python cleanup_output.py            # show what would go
    python cleanup_output.py --apply    # actually delete
"""

import argparse
import sys
from pathlib import Path

BASE_DIR = Path(__file__).parent
OUTPUT_DIR = BASE_DIR / "output"
PROCESSED_DIR = BASE_DIR / "transcripts_in" / "processed"

sys.path.insert(0, str(BASE_DIR))
from pipeline import AUDIO_EXTS


def human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if abs(n) < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def audio_candidates(output_dir: Path):
    """Split audio into (safe to delete, orphaned).

    The matching .txt is the interlock: without it the audio has not actually
    been transcribed, and deleting it would lose the episode.
    """
    safe, orphans = [], []
    for path in sorted(output_dir.glob("*")):
        if path.suffix.lower() not in AUDIO_EXTS or not path.is_file():
            continue
        transcript = path.with_suffix(".txt")
        if transcript.exists() and transcript.stat().st_size > 0:
            safe.append(path)
        else:
            orphans.append(path)
    return safe, orphans


def main():
    parser = argparse.ArgumentParser(
        description="Delete pipeline intermediates that have been superseded")
    parser.add_argument("--apply", action="store_true",
                        help="Actually delete (default: dry run)")
    parser.add_argument("--output-dir", default=str(OUTPUT_DIR))
    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    if not output_dir.exists():
        print(f"No such directory: {output_dir}")
        return

    safe, orphans = audio_candidates(output_dir)
    vtts = sorted(PROCESSED_DIR.glob("*.vtt")) if PROCESSED_DIR.exists() else []

    targets = safe + vtts
    total = sum(p.stat().st_size for p in targets)

    if not targets and not orphans:
        print("Nothing to clean.")
        return

    verb = "Deleting" if args.apply else "Would delete"
    print(f"{verb} {len(targets)} file(s), {human(total)}:\n")
    for path in targets:
        print(f"  {human(path.stat().st_size):>9}  {path.relative_to(BASE_DIR)}")

    if orphans:
        print(f"\nKeeping {len(orphans)} audio file(s) with no transcript:")
        for path in orphans:
            print(f"  {human(path.stat().st_size):>9}  {path.relative_to(BASE_DIR)}")

    if not args.apply:
        print(f"\nDry run — nothing deleted. Re-run with --apply to free {human(total)}.")
        return

    freed, failed = 0, 0
    for path in targets:
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
        except OSError as e:
            print(f"  could not remove {path.name}: {e}")
            failed += 1

    # Only tidy the archive away once it is actually empty — never recursively.
    if PROCESSED_DIR.exists() and not any(PROCESSED_DIR.iterdir()):
        try:
            PROCESSED_DIR.rmdir()
        except OSError:
            pass

    print(f"\nFreed {human(freed)}" + (f" ({failed} failed)" if failed else ""))


if __name__ == "__main__":
    main()
