#!/usr/bin/env python3
"""Regression tests for the 2026-11-02 silent download outage.

From 2026-11-02 X switched to 14-character Space IDs, yt-dlp truncated them,
and every Space download failed with KeyError('metadata') about 2,900 times in
a row over three days. Each failure was logged and written to
output/<space_id>_run.json, but nothing emailed, so summaries just stopped.
A Space that keeps failing must now be emailed once per streak, and once more
when it is processed again.

Run:  python3 -m unittest discover -s tests -v
"""

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_and_run  # noqa: E402

THRESHOLD = check_and_run.SPACE_FAILURE_RUNS_BEFORE_EMAIL
URL = "https://x.com/i/spaces/1aBcDeFgHiJkLm"
SID = "1aBcDeFgHiJkLm"


class RunHarness(unittest.TestCase):
    """Drives check_and_run.run() with the network/pipeline steps mocked out."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        out = Path(tmp.name) / "output"
        out.mkdir()
        self.state = {"processed": {}}
        self.urls = [URL]
        self.download_error = KeyError("metadata")
        self.args = SimpleNamespace(account="StocksOnSpaces", speaker=None, cookies_file=None,
                                    dry_run=False, no_deliver=True, model="turbo",
                                    claude_model="m", keep_audio=False)

        def download(*a, **kw):
            if self.download_error:
                raise self.download_error
            return out / "a.m4a"

        patches = [
            mock.patch.object(check_and_run, "OUTPUT_DIR", out),
            mock.patch.object(check_and_run, "LOG_DIR", Path(tmp.name) / "logs"),
            mock.patch.object(check_and_run, "load_state", lambda: self.state),
            mock.patch.object(check_and_run, "save_state", lambda s: None),
            mock.patch.object(check_and_run, "backfill_recorded_dates", lambda *a, **k: 0),
            mock.patch.object(check_and_run, "fetch_recent_space_urls",
                              lambda *a, **k: list(self.urls)),
            mock.patch.object(check_and_run, "fetch_space_meta",
                              lambda *a, **k: {"recorded_date": "2026-11-02", "live_status": "was_live"}),
            mock.patch.object(check_and_run, "step_download", download),
            mock.patch.object(check_and_run, "step_transcribe", lambda *a, **k: out / "t.txt"),
            mock.patch.object(check_and_run, "step_summarize", lambda *a, **k: out / "s.md"),
            mock.patch.object(check_and_run, "discard_audio", lambda *a, **k: None),
            mock.patch.object(check_and_run, "tickers_for_summary", lambda p: []),
            mock.patch.object(check_and_run, "match_watchlist", lambda t: []),
            mock.patch.object(check_and_run, "log", lambda m: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.send = mock.Mock(return_value=True)
        p = mock.patch.object(check_and_run, "send_email", self.send)
        p.start()
        self.addCleanup(p.stop)

    def run_once(self):
        before = self.send.call_count
        check_and_run.run(self.args)
        return [c.args[0] for c in self.send.call_args_list[before:]]


class SpaceFailureAlertTest(RunHarness):
    def test_emails_once_per_streak(self):
        subjects = [self.run_once() for _ in range(THRESHOLD + 5)]
        sent = [i for i, s in enumerate(subjects) if s]
        self.assertEqual(sent, [THRESHOLD - 1])
        self.assertIn("failing", subjects[THRESHOLD - 1][0])
        record = self.state["space_failures"][SID]
        self.assertTrue(record["notified"])
        self.assertEqual(record["runs"], THRESHOLD + 5)
        self.assertIn("metadata", record["error"])

    def test_email_names_the_space_and_error(self):
        for _ in range(THRESHOLD):
            self.run_once()
        _, plain, html = self.send.call_args.args
        self.assertIn(SID, plain)
        self.assertIn(URL, plain)
        self.assertIn("metadata", plain)
        self.assertIn(SID, html)

    def test_recovery_after_alert_sends_working_again_and_clears(self):
        for _ in range(THRESHOLD):
            self.run_once()
        self.download_error = None
        subjects = self.run_once()
        self.assertEqual(len(subjects), 1)
        self.assertIn("working again", subjects[0])
        self.assertNotIn("space_failures", self.state)
        self.assertIn(SID, self.state["processed"])

    def test_recovery_before_alert_is_silent(self):
        for _ in range(THRESHOLD - 1):
            self.run_once()
        self.download_error = None
        self.assertEqual(self.run_once(), [])
        self.assertNotIn("space_failures", self.state)

    def test_failed_send_is_retried_next_run(self):
        for _ in range(THRESHOLD - 1):
            self.run_once()
        self.send.return_value = False
        self.run_once()
        self.assertFalse(self.state["space_failures"][SID]["notified"])
        self.send.return_value = True
        self.assertEqual(len(self.run_once()), 1)
        self.assertTrue(self.state["space_failures"][SID]["notified"])

    def test_many_failing_spaces_share_one_email(self):
        self.urls = [f"https://x.com/i/spaces/1Space{i:08d}" for i in range(4)]
        sent = [self.run_once() for _ in range(THRESHOLD)]
        self.assertEqual([len(s) for s in sent], [0] * (THRESHOLD - 1) + [1])
        self.assertIn("4 Spaces", sent[-1][0])

    def test_space_leaving_the_profile_drops_its_streak(self):
        for _ in range(3):
            self.run_once()
        self.urls = ["https://x.com/i/spaces/1OtherSpace0000"]
        self.download_error = None
        self.run_once()
        self.assertNotIn("space_failures", self.state)

    def test_empty_discovery_keeps_the_streak(self):
        for _ in range(3):
            self.run_once()
        self.urls = []
        self.run_once()
        self.assertEqual(self.state["space_failures"][SID]["runs"], 3)

    def test_dry_run_never_emails_or_counts(self):
        self.args.dry_run = True
        for _ in range(THRESHOLD + 1):
            self.assertEqual(self.run_once(), [])
        self.assertNotIn("space_failures", self.state)


if __name__ == "__main__":
    unittest.main()
