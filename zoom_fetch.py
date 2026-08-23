#!/usr/bin/env python3
"""
zoom_fetch.py — Download the .vtt transcript behind a passcode-protected Zoom
share link.

Why a browser and not an HTTP client
------------------------------------
yt-dlp cannot do this, and neither can `requests`. Walking the share flow by
hand goes:

    GET /rec/share/<token>                     → SPA shell, window.__data__.meetingId
    GET /nws/recording/1.0/play/share-info/…   → {"componentName": "need-password",
                                                  "redirectUrl": "/rec/component-page"}
    GET /rec/component-page?…                  → the passcode gate

and stops there. The meetingId token is *rotated at every hop* — the share page,
share-info, and the component page each hand back a different one — and the
passcode POST sits behind OWASP CSRFGuard, whose token comes from a separate
POST /csrf_js round-trip on top of the _zm_ssid/cred session cookies. The old
/rec/validate_meet_passwd endpoint every gist uses now answers "This API has
been deprecated".

That plumbing is exactly what rotted yt-dlp's extractor. Driving the real page
sidesteps all of it: Zoom's own JavaScript mints the CSRF token, follows its own
redirects, and carries its own cookies. We only type the passcode and read the
result.

See zoom_failure_diagram.html for the long-form version.
"""

import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# Load .env
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

BASE_DIR = Path(__file__).parent
LOGS_DIR = BASE_DIR / "logs"
INBOX_DIR = BASE_DIR / "transcripts_in"

sys.path.insert(0, str(BASE_DIR))
from pipeline import log
from vtt_ingest import parse_cues, has_speaker_labels

# Zoom serves a degraded page to obvious automation.
USER_AGENT = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36")

# Zoom renames these between releases, so try a chain rather than one selector.
PASSCODE_SELECTORS = (
    "input#passcode",
    "input[name='passwd']",
    "input[type='password']",
    "#password",
)
SUBMIT_SELECTORS = (
    "button#passcode_btn",
    "button[type='submit']",
    "#btnSubmit",
)


class ZoomFetchError(Exception):
    """Base for everything this module raises."""
    retryable = False


class ZoomPasscodeRejected(ZoomFetchError):
    """Zoom said the passcode is wrong. Retrying will not help."""
    retryable = False


class ZoomCaptchaRequired(ZoomFetchError):
    """A captcha appeared — usually rate limiting, so worth trying later."""
    retryable = True


class ZoomTimeout(ZoomFetchError):
    """Page never reached the player. Could be a slow network or a redesign."""
    retryable = True


def slugify(text: str, fallback: str = "zoom-recording") -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]+", " ", text or "").strip()
    return re.sub(r"\s+", " ", cleaned)[:60] or fallback


def date_from_share_url(share_url: str) -> str:
    """Zoom's startTime query param is epoch milliseconds.

    Naming the file with this date lets zoom_ingest.episode_meta() derive the
    episode date from the filename exactly as it does for a hand-downloaded
    "GMT20260807-140233_Recording.transcript.vtt", so nothing downstream has to
    learn about share URLs.
    """
    try:
        raw = parse_qs(urlparse(share_url).query).get("startTime", [None])[0]
        if raw:
            ts = int(raw) / 1000
            return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")
    except (ValueError, TypeError, OSError):
        pass
    return datetime.now().strftime("%Y-%m-%d")


def share_token(share_url: str) -> str:
    """The stable per-recording id: /rec/share/<TOKEN>.<junk>?startTime=…

    The full URL is not a usable key because startTime varies between copies of
    the same link.
    """
    match = re.search(r"/rec/share/([^/?.]+)", share_url)
    return match.group(1) if match else share_url


def _dump_failure(page, tag: str) -> str:
    """Save the page and a screenshot so a 3am launchd failure is diagnosable.

    Without this, working out why Zoom's markup stopped matching means
    reproducing a failure that already happened.
    """
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = LOGS_DIR / f"zoom_fail_{tag}_{stamp}"
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        stem.with_suffix(".html").write_text(page.content(), encoding="utf-8")
        page.screenshot(path=str(stem.with_suffix(".png")), full_page=True)
        log(f"Saved failure dump: {stem}.html / .png")
    except Exception as e:
        log(f"Could not save failure dump: {e}")
    return str(stem)


def _looks_like_vtt(text: str) -> bool:
    return bool(text) and text.lstrip().upper().startswith("WEBVTT")


def _has_captcha(page) -> bool:
    try:
        if page.evaluate("() => !!(window.__data__ && window.__data__.needRecaptcha)"):
            return True
    except Exception:
        pass
    for sel in ("iframe[src*='recaptcha']", "iframe[src*='hcaptcha']", ".captcha-container"):
        try:
            if page.locator(sel).count() > 0 and page.locator(sel).first.is_visible():
                return True
        except Exception:
            continue
    return False


def _fill_passcode(page, passcode: str) -> bool:
    """Type the passcode into whichever field this Zoom build is using."""
    field = None
    for sel in PASSCODE_SELECTORS:
        try:
            loc = page.locator(sel).first
            if loc.count() > 0:
                loc.wait_for(state="visible", timeout=5000)
                field = loc
                break
        except Exception:
            continue
    if field is None:
        return False

    field.fill(passcode)
    for sel in SUBMIT_SELECTORS:
        try:
            btn = page.locator(sel).first
            if btn.count() > 0 and btn.is_visible():
                btn.click()
                return True
        except Exception:
            continue
    field.press("Enter")
    return True


def _play_ids(page) -> list:
    """Candidate fid values for the transcript endpoint.

    The player calls /rec/play/vtt?fid=<playId>&type=cc. playId lives in the
    Vuex store, but window.__data__ and the post-auth URL both carry usable ids
    too, so collect every candidate and let the caller try them in turn.
    """
    ids = []
    try:
        found = page.evaluate("""() => {
            const out = [];
            const d = window.__data__ || {};
            for (const k of ['fileId', 'playId', 'meetingId', 'meeting_id']) {
                if (d[k]) out.push(d[k]);
            }
            const app = document.querySelector('#app');
            const store = app && app.__vue__ && app.__vue__.$store;
            if (store && store.state) {
                for (const k of ['playId', 'playCheckId', 'fileId']) {
                    if (store.state[k]) out.push(store.state[k]);
                }
            }
            return out;
        }""") or []
        ids.extend(found)
    except Exception:
        pass

    match = re.search(r"/rec/play/([^/?#]+)", page.url)
    if match:
        ids.append(match.group(1))

    seen, unique = set(), []
    for value in ids:
        if isinstance(value, str) and value and value not in seen:
            seen.add(value)
            unique.append(value)
    return unique


def _try_endpoints(page, captured: dict) -> str:
    """Ask for the transcript directly, then fall back to whatever the player fetched.

    type=transcript is the Audio Transcript, which carries speaker names;
    type=cc is the caption track, which may not. Speaker attribution is the
    entire reason this project prefers VTT over re-transcribing audio, so the
    transcript flavour is always tried first.
    """
    origin = f"{urlparse(page.url).scheme}://{urlparse(page.url).netloc}"
    for fid in _play_ids(page):
        for kind in ("transcript", "cc"):
            for base in (f"{origin}/rec/play/vtt",
                         f"{origin}/nws/recording/1.0/play/vtt"):
                url = f"{base}?fid={fid}&type={kind}"
                try:
                    resp = page.request.get(url, timeout=30000)
                    if not resp.ok:
                        continue
                    body = resp.text()
                    if _looks_like_vtt(body):
                        log(f"Transcript via {base.rsplit('/', 2)[-2]}/vtt (type={kind})")
                        return body
                except Exception:
                    continue

    if captured.get("body") and _looks_like_vtt(captured["body"]):
        log("Transcript via intercepted player request")
        return captured["body"]
    return ""


def fetch_vtt(share_url: str, passcode: str, out_dir: Path = None,
              timeout_ms: int = 60000, headful: bool = None) -> Path:
    """Download the transcript for one share link. Returns the saved .vtt path."""
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    out_dir = Path(out_dir) if out_dir else INBOX_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    if headful is None:
        headful = os.environ.get("ZOOM_FETCH_HEADFUL", "").lower() in ("1", "true", "yes", "on")

    captured = {}
    log(f"Fetching Zoom transcript ({share_token(share_url)[:16]}…)")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=not headful)
        context = browser.new_context(user_agent=USER_AGENT,
                                      viewport={"width": 1280, "height": 800})
        page = context.new_page()
        page.set_default_timeout(timeout_ms)

        def on_response(response):
            # The player fetches this itself to render the transcript panel;
            # grabbing it here covers builds where the direct call 404s.
            if "/play/vtt" in response.url and "body" not in captured:
                try:
                    body = response.text()
                    if _looks_like_vtt(body):
                        captured["body"] = body
                except Exception:
                    pass

        page.on("response", on_response)

        try:
            page.goto(share_url, wait_until="domcontentloaded", timeout=timeout_ms)
            page.wait_for_timeout(2000)

            if _has_captcha(page):
                _dump_failure(page, "captcha")
                raise ZoomCaptchaRequired(
                    "Zoom presented a captcha — usually rate limiting. Will retry later.")

            if not _fill_passcode(page, passcode):
                # No passcode field at all: either the link is open, or the page
                # changed shape. Only the second case is a problem, and the
                # transcript fetch below will tell us which it was.
                log("No passcode field found — continuing (link may not need one)")
            else:
                page.wait_for_timeout(4000)
                if _has_captcha(page):
                    _dump_failure(page, "captcha")
                    raise ZoomCaptchaRequired("Captcha appeared after passcode entry.")
                body_text = ""
                try:
                    body_text = page.inner_text("body", timeout=5000).lower()
                except Exception:
                    pass
                if "passcode is incorrect" in body_text or "wrong passcode" in body_text:
                    _dump_failure(page, "passcode")
                    raise ZoomPasscodeRejected("Zoom rejected the passcode.")

            try:
                page.wait_for_load_state("networkidle", timeout=20000)
            except PWTimeout:
                pass

            vtt_text = _try_endpoints(page, captured)
            if not vtt_text:
                _dump_failure(page, "novtt")
                raise ZoomTimeout(
                    "Authenticated, but no .vtt came back — the recording may have no "
                    "Audio Transcript, or Zoom changed the player endpoint.")

            # Validate with the same parser the summarizer uses, so a Zoom error
            # page served as HTTP 200 fails here instead of landing in the inbox
            # as a silently broken transcript.
            cues = parse_cues(vtt_text)
            if not cues:
                _dump_failure(page, "nocues")
                raise ZoomFetchError("Downloaded .vtt parsed to zero cues.")
            if not has_speaker_labels(cues):
                log("WARNING: transcript has few speaker labels — "
                    "summary will lack attribution")

            topic = ""
            try:
                topic = page.title().split("|")[0].strip()
            except Exception:
                pass
            if not topic or "zoom" in topic.lower():
                topic = os.environ.get("ZOOM_SHOW_NAME", "Stock Talk Weekly")

            date = date_from_share_url(share_url)
            out_path = out_dir / f"{date} {slugify(topic)}.vtt"
            out_path.write_text(vtt_text, encoding="utf-8")
            log(f"Saved {out_path.name} — {len(cues)} cues")
            return out_path

        except ZoomFetchError:
            raise
        except PWTimeout as e:
            _dump_failure(page, "timeout")
            raise ZoomTimeout(f"Timed out loading the recording: {e}") from e
        except Exception as e:
            _dump_failure(page, "error")
            raise ZoomFetchError(f"{type(e).__name__}: {e}") from e
        finally:
            context.close()
            browser.close()


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Download a Zoom recording's .vtt transcript")
    parser.add_argument("url", help="Zoom /rec/share/ URL")
    parser.add_argument("passcode", help="Recording passcode")
    parser.add_argument("--out-dir", default=str(INBOX_DIR))
    parser.add_argument("--headful", action="store_true", help="Show the browser window")
    args = parser.parse_args()

    try:
        path = fetch_vtt(args.url, args.passcode, Path(args.out_dir), headful=args.headful)
    except ZoomFetchError as e:
        log(f"ERROR: {e}")
        raise SystemExit(1)
    print(path)


if __name__ == "__main__":
    main()
