#!/usr/bin/env python3
"""Regression tests for the 2026-10-08 stranded Zoom summary.

The Stock Talk Weekly link was downloaded, but summarizing it hit the
max_tokens ceiling. The queue had already marked the download "success" and
only ran zoom_ingest after a new download, so the .vtt sat in transcripts_in/
and nothing retried it or said it had failed.

Run:  python3 -m unittest discover -s tests -v
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import zoom_queue  # noqa: E402

URL = "https://us06web.zoom.us/rec/share/TOKEN.abc?startTime=1"


class StrandedIngestTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        tmp = Path(self._tmp.name)
        self.output = tmp / "output"
        self.output.mkdir()
        self.inbox = tmp / "transcripts_in"
        self.inbox.mkdir()
        self.vtt = self.inbox / "2026-10-07 Stock Talk Weekly.vtt"
        self.vtt.write_text("WEBVTT\n")
        self.input = tmp / "zoom_calls_input.txt"
        self.input.write_text(f"{URL}\nPasscode: x\n")
        self.state_file = self.output / "state.json"
        self.state_file.write_text(json.dumps({"processed": {}, "zoom_queue": {
            "TOKEN": {"url": URL, "status": "success", "attempts": 1,
                      "vtt_path": str(self.vtt), "notified": False}}}))

        for name, value in {"OUTPUT_DIR": self.output, "STATE_FILE": self.state_file,
                            "INBOX_DIR": self.inbox}.items():
            patcher = mock.patch.object(zoom_queue, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.send_email = mock.patch.object(zoom_queue, "send_email", return_value=True).start()
        self.addCleanup(mock.patch.stopall)

    def run_queue(self, ingest_succeeds: bool):
        def fake_ingest(no_deliver):
            if ingest_succeeds:
                self.vtt.unlink()
            return ingest_succeeds
        with mock.patch.object(zoom_queue, "run_ingest", side_effect=fake_ingest) as ingest, \
             mock.patch.object(sys, "argv", ["zoom_queue.py", "--input", str(self.input)]):
            zoom_queue.main()
        return ingest

    def record(self):
        return json.loads(self.state_file.read_text())["zoom_queue"]["TOKEN"]

    def test_leftover_vtt_is_retried_without_a_new_download(self):
        ingest = self.run_queue(ingest_succeeds=True)
        ingest.assert_called_once()
        self.assertNotIn("ingest_attempts", self.record())

    def test_failed_ingest_is_counted_and_retried(self):
        self.run_queue(ingest_succeeds=False)
        self.assertEqual(self.record()["ingest_attempts"], 1)
        self.send_email.assert_not_called()
        self.run_queue(ingest_succeeds=False)
        self.assertEqual(self.record()["ingest_attempts"], 2)

    def test_gives_up_and_emails_once_after_max_attempts(self):
        for _ in range(zoom_queue.MAX_INGEST_ATTEMPTS):
            self.run_queue(ingest_succeeds=False)
        self.send_email.assert_called_once()
        self.assertTrue(self.record()["ingest_notified"])
        # Exhausted: later runs leave it alone instead of re-billing every 5 minutes.
        ingest = self.run_queue(ingest_succeeds=False)
        ingest.assert_not_called()
        self.send_email.assert_called_once()

    def test_already_summarized_link_does_not_trigger_ingest(self):
        self.vtt.unlink()
        ingest = self.run_queue(ingest_succeeds=True)
        ingest.assert_not_called()


if __name__ == "__main__":
    unittest.main()
