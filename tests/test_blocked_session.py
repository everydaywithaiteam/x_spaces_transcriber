#!/usr/bin/env python3
"""Regression tests for the 2026-10-19 silent discovery outage.

From 2026-10-19 X redirected the scraper's cookie session to /account/access
behind a Cloudflare "verify you are human" page. Playwright saw no Space calls,
logged "no recent Spaces in timeline", and every run reported 0 new Spaces for
four days with no alert. A block must now be named in the log and emailed once.

Run:  python3 -m unittest discover -s tests -v
"""

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_and_run  # noqa: E402
import pipeline  # noqa: E402


class BlockReasonTest(unittest.TestCase):
    def test_account_access_redirect(self):
        self.assertIn("/account/access",
                      pipeline.x_block_reason("https://x.com/account/access"))

    def test_login_redirect(self):
        self.assertIn("login", pipeline.x_block_reason(
            "https://x.com/i/flow/login?redirect_after_login=%2FStocksOnSpaces"))

    def test_cloudflare_challenge_on_the_profile_url(self):
        body = "x.com\nPerforming security verification\nThis website uses a security service"
        self.assertIn("Cloudflare", pipeline.x_block_reason("https://x.com/StocksOnSpaces", body))

    def test_ordinary_profile_is_not_blocked(self):
        self.assertIsNone(pipeline.x_block_reason("https://x.com/StocksOnSpaces",
                                                  "StocksOnSpaces\nPosts\nReplies"))


class FetchRecentTest(unittest.TestCase):
    def _patch(self, playwright, ydlp):
        return mock.patch.multiple(
            pipeline,
            _find_spaces_via_twitter_api=mock.Mock(return_value=[]),
            _find_spaces_via_playwright=playwright,
            _find_spaces_via_ydlp=mock.Mock(return_value=ydlp),
        )

    def test_block_is_raised_when_nothing_else_finds_spaces(self):
        blocked = mock.Mock(side_effect=pipeline.XSessionBlocked("account check"))
        with self._patch(blocked, []), self.assertRaises(pipeline.XSessionBlocked):
            pipeline.fetch_recent_space_urls("StocksOnSpaces")

    def test_ydlp_result_still_wins_over_a_block(self):
        blocked = mock.Mock(side_effect=pipeline.XSessionBlocked("account check"))
        with self._patch(blocked, ["https://x.com/i/spaces/abc"]):
            self.assertEqual(pipeline.fetch_recent_space_urls("StocksOnSpaces"),
                             ["https://x.com/i/spaces/abc"])

    def test_empty_timeline_is_still_just_empty(self):
        with self._patch(mock.Mock(return_value=[]), []):
            self.assertEqual(pipeline.fetch_recent_space_urls("StocksOnSpaces"), [])


class DiscoverTest(unittest.TestCase):
    args = SimpleNamespace(account="StocksOnSpaces", cookies_file=None, dry_run=False)

    def run_discover(self, state, result):
        fetch = (mock.Mock(side_effect=result) if isinstance(result, Exception)
                 else mock.Mock(return_value=result))
        with mock.patch.object(check_and_run, "fetch_recent_space_urls", fetch), \
             mock.patch.object(check_and_run, "send_email", return_value=True) as send:
            urls = check_and_run.discover(self.args, state)
        return urls, send

    def test_emails_once_after_three_blocked_runs(self):
        state = {}
        sends = []
        for _ in range(6):
            urls, send = self.run_discover(state, pipeline.XSessionBlocked("account check"))
            self.assertEqual(urls, [])
            sends.append(send.call_count)
        self.assertEqual(sends, [0, 0, 1, 0, 0, 0])
        self.assertTrue(state["x_blocked"]["notified"])
        self.assertEqual(state["x_blocked"]["runs"], 6)

    def test_failed_send_is_retried_next_run(self):
        state = {"x_blocked": {"since": "t", "runs": 5, "notified": False}}
        with mock.patch.object(check_and_run, "fetch_recent_space_urls",
                               side_effect=pipeline.XSessionBlocked("x")), \
             mock.patch.object(check_and_run, "send_email", return_value=False):
            check_and_run.discover(self.args, state)
        self.assertFalse(state["x_blocked"]["notified"])
        _, send = self.run_discover(state, pipeline.XSessionBlocked("x"))
        self.assertEqual(send.call_count, 1)

    def test_recovery_clears_the_flag_so_a_new_block_alerts_again(self):
        state = {"x_blocked": {"since": "t", "runs": 9, "notified": True}}
        urls, _ = self.run_discover(state, ["https://x.com/i/spaces/abc"])
        self.assertEqual(urls, ["https://x.com/i/spaces/abc"])
        self.assertNotIn("x_blocked", state)

    def test_dry_run_never_emails(self):
        state = {"x_blocked": {"since": "t", "runs": 5, "notified": False}}
        with mock.patch.object(check_and_run, "fetch_recent_space_urls",
                               side_effect=pipeline.XSessionBlocked("x")), \
             mock.patch.object(check_and_run, "send_email") as send:
            check_and_run.discover(SimpleNamespace(**{**vars(self.args), "dry_run": True}), state)
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main()
