#!/usr/bin/env python3
"""Regression tests for the 2026-09-02/03 pipeline stall.

Each test here is tied to a failure that actually happened:

  * a hung ffmpeg held output/state.lock for 31 hours, freezing both runners;
  * the run that FAILED to take the lock truncated the holder's PID, so
    nothing on disk said which process to kill;
  * summarization sat on the Anthropic API for four hours and then failed;
  * file stems were built from the run date, so a late catch-up filed a 09-02
    Space under 09-03 and re-did a 3-hour transcription it already had.

Run:  python3 -m unittest discover -s tests -v
"""

import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pipeline  # noqa: E402


class TempDirTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)


# ── FIX 1: the lock must not destroy its own diagnostic ──────────────────────

class StateLockTests(TempDirTest):

    def test_writes_holder_pid_while_held(self):
        with pipeline.state_lock(self.tmp, "run") as acquired:
            self.assertTrue(acquired)
            self.assertEqual((self.tmp / "state.lock").read_text().strip(),
                             str(os.getpid()))

    def test_creates_output_dir_if_absent(self):
        target = self.tmp / "does" / "not" / "exist"
        with pipeline.state_lock(target, "run") as acquired:
            self.assertTrue(acquired)
        self.assertTrue((target / "state.lock").exists())

    def test_lock_is_released_on_exit(self):
        with pipeline.state_lock(self.tmp, "first") as a:
            self.assertTrue(a)
        with pipeline.state_lock(self.tmp, "second") as b:
            self.assertTrue(b, "lock was not released by the first holder")

    def test_released_even_when_body_raises(self):
        with self.assertRaises(ValueError):
            with pipeline.state_lock(self.tmp, "boom"):
                raise ValueError("boom")
        with pipeline.state_lock(self.tmp, "after") as again:
            self.assertTrue(again, "an exception left the lock held")

    # -- the two that need a genuinely separate process -----------------------

    def _holder(self, hold_seconds=6):
        """Spawn a real second process that takes the lock and sits on it."""
        code = textwrap.dedent(f"""
            import sys, time
            sys.path.insert(0, {str(Path(pipeline.__file__).parent)!r})
            import pipeline
            with pipeline.state_lock({str(self.tmp)!r}, "holder") as ok:
                assert ok, "holder failed to acquire"
                print("HELD", flush=True)
                time.sleep({hold_seconds})
        """)
        proc = subprocess.Popen([sys.executable, "-c", code],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True)
        self.addCleanup(proc.kill)
        line = proc.stdout.readline().strip()
        self.assertEqual(line, "HELD", "holder process never acquired the lock")
        return proc

    def test_second_run_is_refused_while_lock_is_held(self):
        self._holder()
        with pipeline.state_lock(self.tmp, "zoom queue") as acquired:
            self.assertFalse(acquired, "two runs both acquired the lock")

    def test_loser_does_not_truncate_the_holders_pid(self):
        """The exact bug: open(...,'w') truncated before flock.

        Symptom in production: an empty state.lock and no way to tell from
        disk which process had been wedged for 31 hours.
        """
        holder = self._holder()
        lock = self.tmp / "state.lock"
        self.assertEqual(lock.read_text().strip(), str(holder.pid))

        with pipeline.state_lock(self.tmp, "loser") as acquired:
            self.assertFalse(acquired)

        self.assertEqual(
            lock.read_text().strip(), str(holder.pid),
            "the run that failed to acquire erased the holder's PID")

    def test_refusal_log_names_the_holder(self):
        holder = self._holder()
        lines = []
        with mock.patch.object(pipeline, "log", lines.append):
            with pipeline.state_lock(self.tmp, "zoom queue") as acquired:
                self.assertFalse(acquired)
        self.assertTrue(any(str(holder.pid) in ln for ln in lines),
                        f"holder PID missing from log: {lines}")


# ── FIX 2: distinguish a slow download from a wedged one ─────────────────────

class StallWatchdogTests(TempDirTest):

    def test_download_bytes_counts_part_files(self):
        (self.tmp / "space-2026-09-02.m4a.part").write_bytes(b"x" * 2048)
        self.assertEqual(
            pipeline._download_bytes(self.tmp, "space-2026-09-02"), 2048)

    def test_download_bytes_ignores_other_stems(self):
        (self.tmp / "space-2026-09-02.m4a.part").write_bytes(b"x" * 100)
        (self.tmp / "other-2026-09-03.m4a.part").write_bytes(b"y" * 500)
        self.assertEqual(
            pipeline._download_bytes(self.tmp, "space-2026-09-02"), 100)

    def test_fires_when_bytes_stop_landing(self):
        part = self.tmp / "s.m4a.part"
        part.write_bytes(b"x" * 11)          # the 11 MB that then went flat
        killed = []
        with mock.patch.object(pipeline, "_kill_child_processes",
                               lambda: killed.append(True) or []):
            with mock.patch.object(pipeline, "log", lambda *a: None):
                with pipeline.stall_watchdog(self.tmp, "s", stall_seconds=1,
                                             poll_seconds=0.2) as watch:
                    time.sleep(2.0)
        self.assertTrue(watch["stalled"], "watchdog missed a flat download")
        self.assertTrue(killed, "watchdog did not kill the downloader")

    def test_does_not_fire_while_bytes_keep_landing(self):
        """Guards the real 2h19m @ 10.8 KiB/s download — slow, but healthy."""
        part = self.tmp / "s.m4a.part"
        part.write_bytes(b"")
        stop = threading.Event()

        def trickle():
            n = 0
            while not stop.wait(0.1):
                n += 1
                part.write_bytes(b"x" * n)

        writer = threading.Thread(target=trickle, daemon=True)
        writer.start()
        self.addCleanup(stop.set)
        killed = []
        with mock.patch.object(pipeline, "_kill_child_processes",
                               lambda: killed.append(True) or []):
            with pipeline.stall_watchdog(self.tmp, "s", stall_seconds=1,
                                         poll_seconds=0.2) as watch:
                time.sleep(2.0)
        stop.set()
        self.assertFalse(watch["stalled"], "watchdog killed a healthy download")
        self.assertFalse(killed)

    def test_clean_exit_leaves_watchdog_unfired(self):
        with pipeline.stall_watchdog(self.tmp, "s", stall_seconds=60) as watch:
            pass
        self.assertFalse(watch["stalled"])

    def test_kill_child_processes_kills_a_real_child(self):
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        self.addCleanup(lambda: child.poll() is None and child.kill())
        killed = pipeline._kill_child_processes()
        self.assertIn(child.pid, killed)
        child.wait(timeout=10)
        self.assertIsNotNone(child.poll(), "child survived the watchdog kill")


class StepDownloadTests(TempDirTest):

    def test_reuses_existing_audio(self):
        audio = self.tmp / "stem.m4a"
        audio.write_bytes(b"already downloaded")
        with mock.patch.object(pipeline, "log", lambda *a: None):
            got = pipeline.step_download("https://x.com/i/spaces/abc",
                                         self.tmp, "stem")
        self.assertEqual(got, audio)

    def test_part_file_alone_does_not_count_as_finished(self):
        (self.tmp / "stem.m4a.part").write_bytes(b"half a download")
        calls = []

        class FakeYDL:
            def __init__(self, opts): calls.append(opts)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download):
                (self.tmp_dir / "stem.m4a").write_bytes(b"done")
                return {"ext": "m4a"}
        FakeYDL.tmp_dir = self.tmp

        fake = mock.MagicMock()
        fake.YoutubeDL = FakeYDL
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            with mock.patch.object(pipeline, "FFMPEG_DIR", "/opt/homebrew/bin"):
                with mock.patch.object(pipeline, "log", lambda *a: None):
                    pipeline.step_download("https://x.com/i/spaces/abc",
                                           self.tmp, "stem")
        self.assertTrue(calls, "a .part file was mistaken for a finished download")

    def test_socket_timeout_is_set(self):
        captured = {}

        class FakeYDL:
            def __init__(self, opts): captured.update(opts)
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download):
                (Path(captured["outtmpl"]).parent / "stem.m4a").write_bytes(b"d")
                return {"ext": "m4a"}

        fake = mock.MagicMock()
        fake.YoutubeDL = FakeYDL
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            with mock.patch.object(pipeline, "FFMPEG_DIR", "/opt/homebrew/bin"):
                with mock.patch.object(pipeline, "log", lambda *a: None):
                    pipeline.step_download("https://x.com/i/spaces/abc",
                                           self.tmp, "stem")
        self.assertEqual(captured.get("socket_timeout"), 60)

    def test_raises_when_the_download_stalls(self):
        """A wedged download must fail loudly, not hang holding the lock."""
        class FakeYDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download):
                return {"ext": "m4a"}

        fake = mock.MagicMock()
        fake.YoutubeDL = FakeYDL

        import contextlib

        @contextlib.contextmanager
        def stalled(*a, **k):
            yield {"stalled": True}

        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            with mock.patch.object(pipeline, "FFMPEG_DIR", "/opt/homebrew/bin"):
                with mock.patch.object(pipeline, "stall_watchdog", stalled):
                    with mock.patch.object(pipeline, "log", lambda *a: None):
                        with self.assertRaises(RuntimeError) as ctx:
                            pipeline.step_download("https://x.com/i/spaces/abc",
                                                   self.tmp, "stem")
        self.assertIn("stalled", str(ctx.exception).lower())


# ── The naming bug that turned one failure into repeated 3-hour rework ───────

class FileStemTests(unittest.TestCase):

    def test_uses_the_recorded_date(self):
        stem = pipeline.make_file_stem("https://x.com/i/spaces/1kJz",
                                       "StocksOnSpaces", "2026-09-02")
        self.assertEqual(stem, "stocksonspaces-2026-09-02")

    def test_falls_back_to_today_when_date_unknown(self):
        from datetime import datetime
        stem = pipeline.make_file_stem("https://x.com/i/spaces/1kJz",
                                       "StocksOnSpaces", None)
        self.assertTrue(stem.endswith(datetime.now().strftime("%Y-%m-%d")))

    def test_late_catch_up_still_names_by_recorded_date(self):
        """A run on 09-04 must still file a 09-02 Space as 09-02."""
        stem = pipeline.make_file_stem("https://x.com/i/spaces/1kJz",
                                       "StocksOnSpaces", "2026-09-02")
        self.assertNotIn("2026-09-04", stem)
        self.assertIn("2026-09-02", stem)

    def test_stem_is_stable_across_retries(self):
        a = pipeline.make_file_stem("https://x.com/i/spaces/1kJz", "acct", "2026-09-02")
        b = pipeline.make_file_stem("https://x.com/i/spaces/1kJz", "acct", "2026-09-02")
        self.assertEqual(a, b, "retries would not reuse prior work")

    def test_recorded_date_is_resolved_before_the_stem(self):
        """Source-level guard: reordering these two lines re-breaks reuse."""
        src = (Path(pipeline.__file__).parent / "check_and_run.py").read_text()
        body = src.split("for url in to_process:", 1)[1]
        fetch = body.index("fetch_space_meta(")
        stem = body.index("make_file_stem(")
        self.assertLess(fetch, stem,
                        "make_file_stem runs before the recorded date is known")


class RecordedDateTests(unittest.TestCase):
    """fetch_space_recorded_date now runs OUTSIDE the per-Space try block.

    Moving it there is only safe because it swallows its own errors; if it
    ever starts raising, one unreachable Space would abort the whole
    catch-up run instead of failing just itself.
    """

    def test_returns_none_instead_of_raising(self):
        boom = mock.MagicMock()
        boom.YoutubeDL.side_effect = RuntimeError("network down")
        with mock.patch.dict(sys.modules, {"yt_dlp": boom}):
            with mock.patch.object(pipeline, "log", lambda *a: None):
                got = pipeline.fetch_space_recorded_date("https://x.com/i/spaces/a")
        self.assertIsNone(got, "a failed lookup must not raise past the caller")

    def test_unknown_date_still_yields_a_usable_stem(self):
        stem = pipeline.make_file_stem("https://x.com/i/spaces/a", "acct", None)
        self.assertTrue(stem.startswith("acct-"))


class ReuseTests(TempDirTest):

    def test_transcribe_reuses_existing_transcript(self):
        txt = self.tmp / "stem.txt"
        txt.write_text("[0.0s - 1.0s] hello")
        with mock.patch.object(pipeline, "log", lambda *a: None):
            got = pipeline.step_transcribe(self.tmp / "stem.m4a", self.tmp, "stem")
        self.assertEqual(got, txt)
        self.assertEqual(got.read_text(), "[0.0s - 1.0s] hello")

    def test_summarize_reuses_existing_summary(self):
        md = self.tmp / "stem_summary.md"
        md.write_text("# already summarized")
        with mock.patch.object(pipeline, "log", lambda *a: None):
            got = pipeline.step_summarize(self.tmp / "stem.txt", self.tmp,
                                          "stem", "acct", "https://x.com/i/spaces/a")
        self.assertEqual(got, md)


class DiscardAudioTests(TempDirTest):

    def test_removes_audio_once_transcript_exists(self):
        audio = self.tmp / "s.m4a"; audio.write_bytes(b"x" * 4096)
        txt = self.tmp / "s.txt"; txt.write_text("[0.0s - 1.0s] hi")
        with mock.patch.object(pipeline, "log", lambda *a: None):
            pipeline.discard_audio(audio, txt, keep=False)
        self.assertFalse(audio.exists())
        self.assertTrue(txt.exists(), "the transcript must survive")

    def test_keeps_audio_when_asked(self):
        audio = self.tmp / "s.m4a"; audio.write_bytes(b"x" * 4096)
        txt = self.tmp / "s.txt"; txt.write_text("hi")
        with mock.patch.object(pipeline, "log", lambda *a: None):
            pipeline.discard_audio(audio, txt, keep=True)
        self.assertTrue(audio.exists())


# ── FIX 3: the four-hour summarize hang ──────────────────────────────────────

class SummarizeClientTests(TempDirTest):
    """Exercises the real construction line by running summarize() itself."""

    def _run(self, env=None):
        captured = {}

        class FakeMessage:
            stop_reason = "end_turn"
            content = [type("B", (), {"type": "text", "text": "a plain summary"})()]

        class FakeMessages:
            def create(self, **kw): return FakeMessage()

        class FakeAnthropic:
            def __init__(self, **kw):
                captured.update(kw)
                self.messages = FakeMessages()

        fake = mock.MagicMock()
        fake.Anthropic = FakeAnthropic

        transcript = self.tmp / "s.txt"
        transcript.write_text("[0.0s - 1.0s] hello there")
        out = self.tmp / "s_summary.md"

        base = {"ANTHROPIC_API_KEY": "sk-test"}
        base.update(env or {})
        with mock.patch.dict(sys.modules, {"anthropic": fake}):
            with mock.patch.dict(os.environ, base):
                import summarize as summarize_mod
                summarize_mod.summarize(transcript, speaker="acct",
                                        space_url="https://x.com/i/spaces/a",
                                        output_path=out, structured=False)
        self.assertTrue(out.exists(), "summarize() did not write its output")
        return captured

    def test_timeout_and_retries_are_bounded(self):
        got = self._run()
        self.assertEqual(got.get("timeout"), 600.0)
        self.assertEqual(got.get("max_retries"), 3)

    def test_env_overrides_apply(self):
        got = self._run({"SUMMARIZE_TIMEOUT": "120", "SUMMARIZE_RETRIES": "1"})
        self.assertEqual(got.get("timeout"), 120.0)
        self.assertEqual(got.get("max_retries"), 1)

    def test_worst_case_is_bounded_well_under_the_four_hour_hang(self):
        got = self._run()
        worst = got["timeout"] * (got["max_retries"] + 1)
        self.assertLess(worst, 3600,
                        "worst case still approaches the 4-hour hang")

    def test_api_key_is_still_passed(self):
        self.assertEqual(self._run().get("api_key"), "sk-test")


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ── FIX: three days of summaries built on the tail of a live Space ───────────
#
# 2026-09-08/09/10: the catch-up run found each Space while it was still
# running and handed the live URL to yt-dlp, which followed the live edge for
# two hours and saved 5.9%, 15.8% and 3.8% of a ~2.5-hour conversation. The run
# reported success, the state entry was written, and the replay — complete, and
# a two-minute download — was never fetched because the Space was already
# marked processed.

class SpaceReadinessTests(unittest.TestCase):

    def test_only_an_ended_replay_is_ready(self):
        self.assertIsNone(pipeline.space_not_ready_reason("was_live"))

    def test_a_live_space_is_not_ready(self):
        reason = pipeline.space_not_ready_reason("is_live")
        self.assertIsNotNone(reason, "a running Space must never be downloaded")
        self.assertIn("live", reason)

    def test_upcoming_and_post_live_are_not_ready(self):
        for status in ("is_upcoming", "post_live"):
            self.assertIsNotNone(pipeline.space_not_ready_reason(status), status)

    def test_unknown_status_falls_through_to_the_download(self):
        """A metadata blip must not strand an ended Space forever."""
        self.assertIsNone(pipeline.space_not_ready_reason(None))

    def test_an_unrecognised_status_is_treated_as_not_ready(self):
        self.assertIsNotNone(pipeline.space_not_ready_reason("something_new"))


class FetchSpaceMetaTests(unittest.TestCase):

    def _fake_ydlp(self, info):
        class FakeYDL:
            def __init__(self, opts): pass
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download): return info
        fake = mock.MagicMock()
        fake.YoutubeDL = FakeYDL
        return fake

    def test_reports_live_status_and_date_from_one_call(self):
        import datetime as _dt
        ts = int(_dt.datetime(2026, 9, 10, 12, 45).timestamp())
        fake = self._fake_ydlp({"live_status": "is_live", "release_timestamp": ts})
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            meta = pipeline.fetch_space_meta("https://x.com/i/spaces/a")
        self.assertEqual(meta["live_status"], "is_live")
        self.assertEqual(meta["recorded_date"], "2026-09-10")

    def test_returns_none_instead_of_raising(self):
        boom = mock.MagicMock()
        boom.YoutubeDL.side_effect = RuntimeError("network down")
        with mock.patch.dict(sys.modules, {"yt_dlp": boom}):
            with mock.patch.object(pipeline, "log", lambda *a: None):
                meta = pipeline.fetch_space_meta("https://x.com/i/spaces/a")
        self.assertEqual(meta, {"recorded_date": None, "live_status": None})


class LiveDownloadRefusalTests(TempDirTest):

    def test_step_download_refuses_a_live_space(self):
        fake = mock.MagicMock()  # any yt_dlp use at all would be a bug here
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            with mock.patch.object(pipeline, "FFMPEG_DIR", "/opt/homebrew/bin"):
                with mock.patch.object(pipeline, "log", lambda *a: None):
                    with self.assertRaises(pipeline.SpaceNotReady):
                        pipeline.step_download("https://x.com/i/spaces/abc",
                                               self.tmp, "stem",
                                               live_status="is_live")
        self.assertEqual(list(self.tmp.glob("stem.*")), [],
                         "a refused download must leave nothing behind")

    def test_step_download_probes_when_the_caller_did_not(self):
        """The single-Space CLI never probes, so step_download must."""
        probed = {}

        def fake_meta(url, *a, **k):
            probed["url"] = url
            return {"recorded_date": None, "live_status": "is_live"}

        with mock.patch.object(pipeline, "fetch_space_meta", fake_meta):
            with mock.patch.dict(sys.modules, {"yt_dlp": mock.MagicMock()}):
                with mock.patch.object(pipeline, "FFMPEG_DIR", "/opt/homebrew/bin"):
                    with mock.patch.object(pipeline, "log", lambda *a: None):
                        with self.assertRaises(pipeline.SpaceNotReady):
                            pipeline.step_download("https://x.com/i/spaces/abc",
                                                   self.tmp, "stem")
        self.assertEqual(probed.get("url"), "https://x.com/i/spaces/abc")

    def test_an_ended_replay_still_downloads(self):
        class FakeYDL:
            def __init__(self, opts): self.opts = opts
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def extract_info(self, url, download):
                (Path(self.opts["outtmpl"]).parent / "stem.m4a").write_bytes(b"d")
                return {"ext": "m4a"}

        fake = mock.MagicMock()
        fake.YoutubeDL = FakeYDL
        with mock.patch.dict(sys.modules, {"yt_dlp": fake}):
            with mock.patch.object(pipeline, "FFMPEG_DIR", "/opt/homebrew/bin"):
                with mock.patch.object(pipeline, "log", lambda *a: None):
                    got = pipeline.step_download("https://x.com/i/spaces/abc",
                                                 self.tmp, "stem",
                                                 live_status="was_live")
        self.assertTrue(got.exists())


class DeferredSpaceIsNotRecordedTests(unittest.TestCase):
    """The bug that made this permanent rather than transient.

    state["processed"] is the only record of what has been handled. Writing a
    deferred Space into it means the replay is never downloaded, so the
    deferral path must reach `continue` without touching state or run records.
    """

    def _loop_body(self):
        src = (Path(pipeline.__file__).parent / "check_and_run.py").read_text()
        body = src.split("for url in to_process:", 1)[1]
        return body.split("if not args.dry_run and not args.no_deliver:", 1)[0]

    def test_deferral_continues_before_state_is_written(self):
        body = self._loop_body()
        defer = body.index("space_not_ready_reason(")
        record = body.index('state["processed"][space_id]')
        self.assertLess(defer, record,
                        "a live Space would be recorded as processed")

    def test_space_not_ready_is_caught_separately_from_failures(self):
        body = self._loop_body()
        self.assertIn("except SpaceNotReady", body,
                      "a deferral must not be logged as a failed run")
        self.assertLess(body.index("except SpaceNotReady"),
                        body.index("except Exception"),
                        "SpaceNotReady must be caught before the generic handler")


class TwitterApiStateFilterTests(unittest.TestCase):
    """/spaces/by/creator_ids returns live AND scheduled Spaces.

    It asked for `state` and then queued every result regardless, which is the
    same bug by a different route.
    """

    def test_only_ended_spaces_are_queued(self):
        src = Path(pipeline.__file__).read_text()
        block = src.split("/spaces/by/creator_ids", 1)[1].split("/spaces/search", 1)[0]
        self.assertIn('"ended"', block,
                      "live/scheduled Spaces are queued for download")


class CliStemUsesRecordedDateTests(unittest.TestCase):
    """pipeline.py's own main() never passed a recorded date to make_file_stem.

    check_and_run.py was fixed for this; the single-Space CLI was not, so a
    re-run of the 09-08 Space on 09-10 resolved to `stocksonspaces-2026-09-10`
    and overwrote that day's transcript and summary.
    """

    def _main_body(self):
        src = Path(pipeline.__file__).read_text()
        return src.split("space_id = extract_space_id(args.url)", 1)[1]

    def test_stem_is_built_from_the_probed_date(self):
        body = self._main_body()
        probe = body.index("fetch_space_meta(")
        stem = body.index("make_file_stem(")
        self.assertLess(probe, stem, "the stem is built before the date is known")

    def test_make_file_stem_is_not_called_without_a_date(self):
        body = self._main_body()
        call = body[body.index("make_file_stem("):]
        call = call[:call.index(")") + 1]
        self.assertIn("recorded_date", call,
                      "make_file_stem falls back to today's date, overwriting another day")
