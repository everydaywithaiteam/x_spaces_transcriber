# X Spaces Transcriber

Automatically downloads X (Twitter) Spaces, transcribes them with Whisper, and generates summaries of a specific speaker's contributions using Claude.

## What it does

1. **Detects** recent Spaces from a given X account (via Twitter API v2, Playwright, or yt-dlp)
2. **Downloads** the Space audio with yt-dlp
3. **Transcribes** audio using faster-whisper with optional speaker diarization (pyannote)
4. **Summarizes** the target speaker's contributions using Claude (Anthropic API)
5. **Emails** each summary as a formatted HTML message
6. **Alerts** you separately if a ticker on your watchlist gets mentioned (opt-in, off by default)
7. **Syncs** each summary into a Notion database for a searchable archive (optional)

Outputs per Space: `.m4a` audio, `.txt` transcript, `_summary.md` summary, `_run.json` metadata.

`check_and_run.py` tracks every successfully processed Space in `output/state.json`, so if a scheduled run is missed (e.g. the laptop was off), the next run catches up on every new Space since the last one processed — not just the latest — bounded by the ~10 most recent Spaces X exposes on the account's profile page.

## Pipeline

```text
                   +---------------------------------------------------------------------------------------------------------------------------------------+
                   |                        check_and_run.py -- scheduled catch-up; retries each stage independently, on a schedule                        |
                   +---------------------------------------------------------------------------------------------------------------------------------------+

                   +------------------+   +------------------+   +------------------+   +------------------+   +------------------+   +------------------+
New Space          |   1. Download    |-->|  2. Transcribe   |-->|   3. Summarize   |-->|    4. Deliver    |-->|  5. Alert  NEW   |-->|  6. Archive NEW  |
detected on        |      yt-dlp      |   |  faster-whisper  |   |    Claude API    |   | email_notify.py  |   | ticker_alerts.py |   |  notion_sync.py  |
@account   -->     +------------------+   +------------------+   +------------------+   +---------+--------+   +---------+--------+   +---------+--------+
                                                                                                    |                      |                      |
                                                                                               your inbox            priority ping         Notion database

                   NEW = shipped in v2.0 (ticker watchlist alerts + Notion sync)
```

A polished, interactive (light/dark) version of this same diagram is at [pipeline_diagram.html](pipeline_diagram.html).

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
EMAIL_TO=pkamela@gmail.com           # Where summaries get sent (comma-separate for multiple recipients)

WATCHLIST_ALERTS=false               # Optional: 1/true/yes/on to enable ticker alert emails (off by default)
WATCHLIST_FILE=watchlist.txt         # Optional: path to a personal ticker watchlist (see below)

NOTION_TOKEN=secret_...              # Optional: Notion integration token (see below)
NOTION_DATABASE_ID=...               # Optional: Notion database ID (see below)
```

Get a free Twitter Bearer Token at [developer.twitter.com](https://developer.twitter.com/en/portal/dashboard) — create a project → app → Keys and Tokens → Bearer Token.

For email delivery, generate a Gmail **App Password** at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords) (requires 2-factor authentication enabled on the Google account) and use it as `SMTP_APP_PASSWORD` — not your regular Gmail password. Without SMTP configured, the pipeline still runs normally; email sending is skipped and logged rather than failing the run.

### Ticker watchlist alerts (opt-in, off by default)

Alert emails are disabled unless you set `WATCHLIST_ALERTS=true`. With them off you still get the full summary email for every Space, and tickers are still extracted for the Notion `Tickers` property — only the extra alert email is suppressed.

If you only care when a specific stock comes up, copy `watchlist.txt.example` to `watchlist.txt` and list your tickers, one per line (lines starting with `#` are ignored):

```
AAPL
NVDA
TSLA
```

After each Space is summarized, its "Stocks & Tickers Mentioned" section is checked against `watchlist.txt`. When `WATCHLIST_ALERTS` is on, any match triggers a short, separate priority email (e.g. "🔔 $NVDA mentioned — StocksOnSpaces 2026-07-21") in addition to the full summary email — so a match doesn't get buried in a long read. `watchlist.txt` is gitignored (personal data) and re-read fresh on every run, so editing it takes effect on the next scheduled run. Without it configured (or if it's empty/missing), watchlist alerts are skipped and logged, same as unconfigured SMTP.

### Zoom episodes from .vtt transcripts (optional)

Zoom cloud recordings publish an **Audio Transcript** (`.vtt`) alongside the video. `zoom_ingest.py` summarizes an episode straight from that file, which skips the two most expensive stages of the pipeline entirely — there is no multi-GB video to download and no Whisper pass to run — and the transcript arrives with **speaker names already attached**, so Claude works from real attribution instead of inferring who the host is.

1. Download an episode's `.vtt` from its Zoom recording page.
2. Drop it into `transcripts_in/` (created on first run).
3. Run it:

```bash
python zoom_ingest.py
```

Each file becomes `output/zoom-<date>-<title>.txt` (a merged, speaker-labelled transcript) and `output/zoom-<date>-<title>_summary.md`, then flows through the same email → watchlist alert → Notion delivery as a Space. Processed `.vtt` files move to `transcripts_in/processed/`; pass `--keep` to leave them where they are.

The episode date comes from a `YYYY-MM-DD` (or `YYYYMMDD`) in the filename when present, otherwise the file's modification time — so both Zoom's own `GMT20260731-140233_Recording.transcript.vtt` and a hand-renamed `2026-08-07 Episode 36.vtt` work, as does a browser's `… (1).vtt` repeat download (which resolves to the same episode, so it can't be processed twice). Zoom's own names produce a title of just "Recording"; rename the file if you want the episode number in the summary. The focus speaker defaults to whoever has the most airtime, which for a hosted show is the host; override with `--speaker "Name"`.

Zoom emits one cue per breath, so consecutive cues from the same speaker are merged into turns of up to a minute. On a **solo** broadcast — one presenter, questions taken from a text channel rather than out loud — the speaker never changes and rarely pauses, so that one-minute cap is what keeps a usable timestamp on each turn for the summary to cite. Single-speaker episodes are detected automatically and summarized with the "Guest Highlights" section dropped.

```bash
python zoom_ingest.py --dry-run                    # show what would be processed
python vtt_ingest.py <file.vtt> --speakers         # speaking-time breakdown, no API calls
```

Because the transcript is machine-generated, ticker symbols are sometimes mis-transcribed ("in video" for NVDA). The summary prompt used for labelled transcripts tells Claude to correct those where context makes the intended ticker unambiguous, and to flag rather than guess where it doesn't. `transcripts_in/` is gitignored — it holds third-party show content.

### Notion sync (optional)

Every generated summary can also be pushed into a Notion database as its own page — a searchable, filterable archive instead of markdown files sitting in `output/`. One-time setup:

1. Create an integration at [notion.so/my-integrations](https://www.notion.so/my-integrations) and copy its **Internal Integration Token** into `NOTION_TOKEN`.
2. Create a Notion database with these properties, named exactly:
   - `Name` (title — the default title property)
   - `Date` (date)
   - `Account` (text)
   - `Speaker` (text)
   - `URL` (url)
   - `Tickers` (multi-select)
3. Share the database with your integration (`•••` menu on the database → Connections → add your integration).
4. Copy the database ID out of the database's URL (the 32-character ID segment) into `NOTION_DATABASE_ID`.

Without `NOTION_TOKEN`/`NOTION_DATABASE_ID` configured, Notion sync is skipped and logged rather than failing the run — same behavior as unconfigured SMTP or an unconfigured watchlist.

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
  state.json               # tracks processed Space IDs and per-space delivery/sync status
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
      "email_last_error": null,
      "tickers_mentioned": ["AAPL", "NVDA"],
      "watchlist_matches": ["NVDA"],
      "watchlist_alert_sent": true,
      "watchlist_alert_sent_at": "2026-07-18T12:41:05+00:00",
      "watchlist_alert_attempts": 1,
      "watchlist_alert_last_error": null,
      "notion_synced": true,
      "notion_synced_at": "2026-07-18T12:41:07+00:00",
      "notion_page_id": "1a2b3c4d-...",
      "notion_sync_attempts": 1,
      "notion_sync_last_error": null
    }
  }
}
```

A Space is added to `processed` only once download, transcription, and summarization all succeed — a failed pipeline run is retried next time automatically. Email delivery, watchlist alerts, and Notion sync status are each tracked separately per Space, so a bounced/failed send or sync is retried on the next run without redoing any of the pipeline work. Spaces processed before these two features existed are marked as already-done (`watchlist_alert_sent`/`notion_synced: true`) on migration, so upgrading doesn't trigger a burst of retroactive alerts or Notion pages. The same applies while `WATCHLIST_ALERTS` is off: matches are still recorded in `watchlist_matches`, but entries are written with `watchlist_alert_sent: true`, so turning alerts on later doesn't replay everything that came before.

## Notes

- Speaker diarization requires accepting [pyannote's terms](https://huggingface.co/pyannote/speaker-diarization-3.1) on Hugging Face and providing `HF_TOKEN`.
- Without `HF_TOKEN`, the pipeline still transcribes but cannot attribute segments by speaker.
- If the Twitter API is unavailable, the pipeline falls back to Playwright (using `cookies.txt` if present, otherwise local Chrome cookies) and then yt-dlp for Space discovery.
- Email delivery (`email_notify.py`) renders each `_summary.md` to HTML via the `markdown` package and sends it over SMTP with STARTTLS; it never raises — a misconfigured or failing SMTP setup is logged and skipped rather than breaking the pipeline run.
- Ticker extraction (`ticker_alerts.py`) depends on Claude following the `- **$TICKER** — stance — context` bullet format requested in `summarize.py`'s prompts; summaries generated before this format was introduced won't parse and simply yield no tickers.
- Notion sync (`notion_sync.py`) talks to the Notion REST API directly via `urllib` (no extra dependency) and converts each summary's headings/bullets into Notion blocks, paginating in batches of 100 for longer summaries.
