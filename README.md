# X Spaces Downloader

Automatically downloads X (Twitter) Spaces, transcribes them with Whisper, and generates summaries of a specific speaker's contributions using Claude.

## What it does

1. **Detects** the latest Space from a given X account (via Twitter API v2 or yt-dlp)
2. **Downloads** the Space audio with yt-dlp
3. **Transcribes** audio using faster-whisper with optional speaker diarization (pyannote)
4. **Summarizes** the target speaker's contributions using Claude (Anthropic API)

Outputs per Space: `.m4a` audio, `.txt` transcript, `_summary.md` summary, `_run.json` metadata.

## Setup

### Requirements

```bash
pip install -r requirements.txt
playwright install chromium
```

### Environment variables

Copy `.env.example` to `.env` and fill in your keys:

```
ANTHROPIC_API_KEY=sk-ant-...         # Required for summarization
HF_TOKEN=hf_...                      # Required for speaker diarization
TWITTER_BEARER_TOKEN=AAAA...         # Required for Space auto-detection
```

Get a free Twitter Bearer Token at [developer.twitter.com](https://developer.twitter.com/en/portal/dashboard) — create a project → app → Keys and Tokens → Bearer Token.

## Usage

### One-off: process a specific Space URL

```bash
python pipeline.py --url https://x.com/i/spaces/SPACE_ID
```

### Auto-detect and process the latest Space for an account

```bash
python check_and_run.py
```

Options:
- `--dry-run` — show what would be done without downloading
- `--force` — re-process the latest Space even if already done

State is tracked in `output/state.json` so the same Space is never processed twice.

### pipeline.py options

```
--url URL                Space URL to process (overrides auto-detection)
--account HANDLE         X handle to watch (default: stocktalkweekly)
--speaker HANDLE         Speaker to focus summary on (default: same as --account)
--model MODEL            Whisper model: tiny/base/small/medium (default: base)
--output-dir DIR         Output directory (default: ./output)
--cookies-from-browser   Browser for cookies: chrome/firefox/safari
--skip-if-exists         Skip if today's output already exists
```

### Running daily via cron

```cron
0 9 * * * cd /path/to/x_spaces_downloader && python check_and_run.py >> logs/pipeline.log 2>&1
```

## Output

```
output/
  <space_id>.m4a           # downloaded audio
  <space_id>.txt           # full transcript
  <space_id>_summary.md    # speaker summary
  <space_id>_run.json      # metadata (duration, model, tokens, etc.)
  state.json               # tracks last processed Space ID
```

## Notes

- Speaker diarization requires accepting [pyannote's terms](https://huggingface.co/pyannote/speaker-diarization-3.1) on Hugging Face and providing `HF_TOKEN`.
- Without `HF_TOKEN`, the pipeline still transcribes but cannot attribute segments by speaker.
- If the Twitter API is unavailable, the pipeline falls back to yt-dlp for Space discovery.
