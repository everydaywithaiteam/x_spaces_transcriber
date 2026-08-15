#!/usr/bin/env python3
"""
Email delivery for X Space summaries.

Sends the contents of a `_summary.md` file as a nicely formatted HTML email
(with a plain-text fallback) via SMTP.

Environment:
    SMTP_HOST           SMTP server (default: smtp.gmail.com)
    SMTP_PORT           SMTP port (default: 587)
    SMTP_USER           SMTP login / From address
    SMTP_APP_PASSWORD   Gmail App Password (myaccount.google.com/apppasswords)
    EMAIL_TO            Recipient address(es), comma-separated (default: same as SMTP_USER)
"""

import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

# Load .env file if present
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        if _line.strip() and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_APP_PASSWORD = os.environ.get("SMTP_APP_PASSWORD")
EMAIL_TO = os.environ.get("EMAIL_TO") or SMTP_USER
EMAIL_TO_LIST = [addr.strip() for addr in EMAIL_TO.split(",")] if EMAIL_TO else []


def send_email(subject: str, plain_text: str, html: str, to_list: list = None) -> bool:
    """Send an arbitrary email via SMTP. Never raises — returns True/False (logged)."""
    to_list = to_list or EMAIL_TO_LIST
    if not SMTP_USER or not SMTP_APP_PASSWORD or not to_list:
        print("email_notify: SMTP not configured (SMTP_USER/SMTP_APP_PASSWORD/EMAIL_TO) — skipping")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = SMTP_USER
        msg["To"] = ", ".join(to_list)
        msg.attach(MIMEText(plain_text, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=30) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_APP_PASSWORD)
            server.sendmail(SMTP_USER, to_list, msg.as_string())

        print(f"email_notify: sent '{subject}' to {', '.join(to_list)}")
        return True

    except Exception as e:
        print(f"email_notify: send failed — {e}")
        return False


# Per-source labelling, so a glance at the inbox says which show an email is
# from. Entries created before Zoom ingest existed carry no "source" and are
# X Spaces, which keeps old subject lines unchanged.
_SOURCES = {
    "zoom-vtt": ("Zoom Summary", "#2d8cff"),   # Zoom blue
}
_DEFAULT_SOURCE = ("X Space Summary", "#1da1f2")  # Twitter blue


def source_label(space_meta: dict) -> tuple:
    """(subject prefix, accent colour) for the episode's source."""
    return _SOURCES.get(space_meta.get("source"), _DEFAULT_SOURCE)


def send_summary_email(summary_path: Path, space_meta: dict) -> bool:
    """Email the contents of a summary .md file.

    space_meta: {"account", "speaker", "url", "space_id", "date", "source"}
    "date" is the date the episode was recorded (not when it was processed).
    "source" selects the subject prefix — "zoom-vtt" or absent (X Space).
    Never raises — returns True on success, False on any failure (logged).
    """
    try:
        md_text = summary_path.read_text(encoding="utf-8")

        import markdown
        html_body = markdown.markdown(md_text, extensions=["extra", "sane_lists"])

        account = space_meta.get("account", "")
        date = space_meta.get("date", "")
        url = space_meta.get("url", "")
        label, accent = source_label(space_meta)

        html = f"""\
<html>
<body style="font-family:-apple-system,Helvetica,Arial,sans-serif;max-width:700px;
             margin:0 auto;padding:20px;color:#1a1a1a;line-height:1.55;">
  <div style="border-bottom:2px solid {accent};padding-bottom:12px;margin-bottom:20px;">
    <h1 style="font-size:18px;margin:0 0 4px 0;">{label} — {account}</h1>
    <div style="font-size:13px;color:#666;">
      {f"Recorded {date}" if date else ""}{" &middot; " if date and url else ""}{f'<a href="{url}" style="color:{accent};">{url}</a>' if url else ""}
    </div>
  </div>
  {html_body}
</body>
</html>"""

        subject = f"{label} — {account} — {date}".strip(" —")
        return send_email(subject, md_text, html)

    except Exception as e:
        print(f"email_notify: send failed for {space_meta.get('space_id')} — {e}")
        return False
