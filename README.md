# X Spaces Transcriber

Automatically downloads X (Twitter) Spaces, transcribes them with Whisper, and generates summaries of a specific speaker's contributions using Claude.

## What it does

1. **Detects** recent Spaces from a given X account (via Twitter API v2, Playwright, or yt-dlp)
2. **Downloads** the Space audio with yt-dlp
3. **Transcribes** audio using faster-whisper with optional speaker diarization (pyannote)
4. **Summarizes** the target speaker's contributions using Claude (Anthropic API)
5. **Emails** each summary as a formatted HTML message

Outputs per Space: `.m4a` audio, `.txt` transcript, `_summary.md` summary, `_run.json` metadata.

`check_and_run.py` tracks every successfully processed Space in `output/state.json`, so if a scheduled run is missed (e.g. the laptop was off), the next run catches up on every new Space since the last one processed — not just the latest — bounded by the ~10 most recent Spaces X exposes on the account's profile page.

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
COOKIES_FILE=cookies.txt             # Optional: path to Netscape cookies.txt (see below)

SMTP_HOST=smtp.gmail.com             # Required for email delivery
SMTP_PORT=587
SMTP_USER=youraddress@gmail.com      # Gmail address used to send
SMTP_APP_PASSWORD=xxxx xxxx xxxx xxxx
EMAIL_TO=pkamela@gmail.com           # Where summaries get sent
```

Get a free Twitter Bearer Token at [developer.twitter.com](https://developer.twitter.com/en/portal/dashboard) — create a project → app → Keys and Tokens → Bearer Token.

For email delivery, generate a Gmail **App Password** at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2-factor authentication enabled on the Google account) and use it as `SMTP_APP_PASSWORD` — not your regular Gmail password. Without SMTP configured, the pipeline still runs normally; email sending is skipped and logged rather than failing the run.

### Space discovery: cookies.txt (recommended)

Space auto-detection needs an authenticated x.com session. The pipeline can pull cookies live from a running Chrome (`--cookies-from-browser chrome`), but newer Chrome versions can silently fail to decrypt sensitive session cookies (`auth_token`, `ct0`, `twid`) for automated tools even while you're genuinely logged in — this shows up as "no recent Spaces" even when there are some. A **cookies.txt export** avoids this entirely and is more reliable for an unattended daily run:

1. Install a browser extension that exports cookies in Netscape format, e.g. [Get cookies.txt LOCALLY](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc).
2. Log in to x.com in Chrome, visit x.com, and export cookies for the `x.com` domain to `cookies.txt` in the project root (`/Users/sam/Claude/x_spaces_transcriber/cookies.txt`).
3. That's it — both `pipeline.py` and `check_and_run.py` auto-detect `./cookies.txt` (or `$COOKIES_FILE` in `.env` for a different path) and prefer it over live Chrome extraction.

`cookies.txt` contains a live session token — it's gitignored, just like `.env`, and should never be committed. Session cookies eventually expire or get invalidated (e.g. by a password change); if discovery starts failing again, just re-export.

## Usage

### One-off: process a specific Space URL

```bash
python pipeline.py --url https://x.com/i/spaces/SPACE_ID
```

### Catch up on and process all new Spaces for an account

```bash
python check_and_run.py
```

Options:
- `--dry-run` — show what would be processed without downloading
- `--account HANDLE` — X handle to watch (default: StocksOnSpaces)
- `--speaker HANDLE` — speaker to focus summary on (default: same as --account)
- `--model SIZE` / `--claude-model MODEL` — same as pipeline.py
- `--cookies-file FILE` — Netscape cookies.txt for x.com (default: `./cookies.txt` or `$COOKIES_FILE` if present)

Discovers the recent Spaces visible on the account's profile, skips any already in `output/state.json`, and processes every new one (oldest first) — so a missed day's Space isn't lost, it's picked up on the next run. Each summary is emailed; a failed send is retried on the next run without re-running the pipeline.

### pipeline.py options

```
--url URL                Space URL to process (overrides auto-detection)
--account HANDLE         X handle to watch (default: stocktalkweekly)
--speaker HANDLE         Speaker to focus summary on (default: same as --account)
--model MODEL            Whisper model: tiny/base/small/medium (default: base)
--output-dir DIR         Output directory (default: ./output)
--cookies-from-browser   Browser for cookies: chrome/firefox/safari
--cookies-file FILE      Netscape cookies.txt for x.com (preferred — see above)
--skip-if-exists         Skip if today's output already exists
```

### Running daily via cron

```cron
0 9 * * * cd /path/to/x_spaces_transcriber && python check_and_run.py >> logs/pipeline.log 2>&1
```

## Output

```
output/
  <space_id>.m4a           # downloaded audio
  <space_id>.txt           # full transcript
  <space_id>_summary.md    # speaker summary
  <space_id>_run.json      # metadata (duration, model, tokens, etc.)
  state.json               # tracks processed Space IDs and per-space email delivery status
```

`state.json` schema:

```json
{
  "schema_version": 2,
  "last_run": "2026-07-18T21:00:03+00:00",
  "processed": {
    "<space_id>": {
      "url": "https://x.com/i/spaces/...",
      "account": "StocksOnSpaces",
      "speaker": "StocksOnSpaces",
      "file_stem": "stocksonspaces-2026-07-18",
      "summary_path": "output/stocksonspaces-2026-07-18_summary.md",
      "processed_at": "2026-07-18T12:40:29+00:00",
      "email_sent": true,
      "email_sent_at": "2026-07-18T12:41:02+00:00",
      "email_attempts": 1,
      "email_last_error": null
    }
  }
}
```

A Space is added to `processed` only once download, transcription, and summarization all succeed — a failed pipeline run is retried next time automatically. Email delivery status is tracked separately per Space, so a bounced/failed send is retried on the next run without redoing any of the pipeline work.

## Notes

- Speaker diarization requires accepting [pyannote's terms](https://huggingface.co/pyannote/speaker-diarization-3.1) on Hugging Face and providing `HF_TOKEN`.
- Without `HF_TOKEN`, the pipeline still transcribes but cannot attribute segments by speaker.
- If the Twitter API is unavailable, the pipeline falls back to Playwright (using `cookies.txt` if present, otherwise local Chrome cookies) and then yt-dlp for Space discovery.
- Email delivery (`email_notify.py`) renders each `_summary.md` to HTML via the `markdown` package and sends it over SMTP with STARTTLS; it never raises — a misconfigured or failing SMTP setup is logged and skipped rather than breaking the pipeline run.
