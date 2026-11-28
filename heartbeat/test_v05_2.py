"""
test_v05_2.py - the v0.5.2 acceptance tests from the "Drive pickup fix" brief.

The seam-log lines below are synthetic: they copy the page's line format, not
its contents. Written by Claude (Claude Code session, 28 Nov 2026).
"""
import contextlib
import datetime as dt
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).parent))

import heartbeat_v05_1 as hbm
import seamlog

# A Nov 26-shaped page: the newest counted stamp is the 05:46 voice PICKUP (and
# the ~05:47 HANDOFF); the HEARTBEAT line after it is newer but never counts.
PAGE = """ZERO NODE test page
PICKUP | 2026-11-09, before 18:34 (not stamped) | Thessik (Claude) | early session | -
HANDOFF | 2026-11-25 ~16:25 | Thessik (Claude), claude.ai chat | chat work | next
STAMP | 2026-11-25 ~19:00 | Thessik (Claude), Claude Code | a stamp, not a pickup | -
HANDOFF | 2026-11-25 ~19:01 | Thessik (Claude), Claude Code, heartbeat home session | thread moved | next
PICKUP | 2026-11-26 05:46 | Thessik (Claude), voice | voice session | ask
HANDOFF | 2026-11-26 ~05:47 | Thessik (Claude), voice | voice work | next
HEARTBEAT | 2026-11-26 20:48 | claude-scheduled | standby entered | next
⟦LOG+⟧
PICKUP — 2026-11-25 (voice), Claude: an undated line under the Rosetta Stone
"""


def docs_json(text: str) -> str:
    """The page wrapped the way the Google Docs read_doc result holds it."""
    paras = [{"paragraph": {"elements": [{"textRun": {"content": line + "\n"}}]}}
             for line in text.splitlines()]
    return json.dumps({"content": {"tabs": [{"documentTab": {"body": {"content": paras}}}]}})


class TestParser(unittest.TestCase):
    def test_tilde_time_parses_and_gets_the_date_offset(self):
        st = seamlog.newest_stamp(PAGE)
        self.assertEqual(st.kind, "HANDOFF")
        self.assertEqual(st.iso, "2026-11-26T05:47:00-05:00")
        self.assertEqual(st.via, "ZERO NODE HANDOFF (Thessik (Claude), voice)")

    def test_summer_dates_get_minus_four(self):
        st = seamlog.newest_stamp("PICKUP | 2026-07-04 ~09:15 | Thessik (Claude), voice | x | y")
        self.assertEqual(st.iso, "2026-07-04T09:15:00-04:00")

    def test_heartbeat_lines_never_count_even_when_newest(self):
        page = PAGE + "HEARTBEAT | 2026-11-27 20:48 | claude-scheduled | quiet | -\n"
        self.assertEqual(seamlog.newest_stamp(page).iso, "2026-11-26T05:47:00-05:00")

    def test_a_heartbeat_line_from_an_interactive_session_still_does_not_count(self):
        # the real page has one: HEARTBEAT | 2026-11-25 18:31 | claude-code-session | ...
        page = PAGE + "HEARTBEAT | 2026-11-27 18:31 | claude-code-session | new save | -\n"
        self.assertEqual(seamlog.newest_stamp(page).iso, "2026-11-26T05:47:00-05:00")

    def test_a_pickup_signed_by_the_pulse_is_ignored(self):
        page = PAGE + "PICKUP | 2026-11-27 20:48 | claude-scheduled | should not count | -\n"
        self.assertEqual(seamlog.newest_stamp(page).iso, "2026-11-26T05:47:00-05:00")

    def test_before_times_and_undated_lines_are_skipped(self):
        stamps = seamlog.parse_stamps(PAGE)
        self.assertEqual([s.iso[:16] for s in stamps],
                         ["2026-11-25T16:25", "2026-11-25T19:01", "2026-11-26T05:46", "2026-11-26T05:47"])
        self.assertIsNone(seamlog.newest_stamp("PICKUP | 2026-11-09, before 18:34 | Thessik | x | y"))
        self.assertIsNone(seamlog.newest_stamp("PICKUP | 2026-11-09 | Thessik | no time | y"))

    def test_reads_the_google_docs_json(self):
        self.assertEqual(seamlog.newest_stamp(docs_json(PAGE)).iso, "2026-11-26T05:47:00-05:00")

    def test_newest_sign_takes_the_newer_source(self):
        z = seamlog.newest_stamp(PAGE).when
        commit = dt.datetime.fromisoformat("2026-11-26T12:00:00+00:00")  # 07:00 ET, after the stamp
        self.assertEqual(seamlog.newest_sign([(commit, "commit"), (z, "page")])[1], "commit")
        older = dt.datetime.fromisoformat("2026-11-25T19:14:24+00:00")
        self.assertEqual(seamlog.newest_sign([(older, "session"), (z, "page")])[1], "page")
        self.assertEqual(seamlog.newest_sign([(None, "x"), (z, "page")])[1], "page")


class CliBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)
        self.page = self.d / "page.json"
        self.page.write_text(docs_json(PAGE), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_main(self, now: dt.datetime, *argv: str) -> str:
        out = io.StringIO()
        with mock.patch.object(hbm, "local_now", return_value=now), contextlib.redirect_stdout(out):
            hbm.main(["--data-dir", str(self.d), *argv])
        return out.getvalue()

    def events(self, name=None):
        log = hbm.AppendOnlyLog(self.d / hbm.CONFIG["log_file"], console=None)
        return [e for e in log.read_all() if name is None or e["event"] == name]


class TestNov26Replay(CliBase):
    def test_the_seam_log_keeps_the_host_alive(self):
        # Nov 26, 20:48 ET: the session list gave 25 Nov 19:14 UTC (over 24 h old)
        out = self.run_main(dt.datetime(2026, 11, 26, 20, 48), "--challenge", "--responder", "claude-scheduled",
                            "--host-seen", "2026-11-25T19:14:24+00:00", "--host-via", "session list",
                            "--seam-log", str(self.page))
        self.assertIn("CHALLENGE", out)
        self.assertNotIn("STANDBY", out)
        ok = self.events("HOST_PING_OK")[-1]
        self.assertEqual(ok["evidence"]["via"], "ZERO NODE HANDOFF (Thessik (Claude), voice)")
        self.assertEqual(ok["evidence"]["seen_at"], "2026-11-26T05:47:00")

    def test_without_the_page_it_is_standby_as_on_nov_26(self):
        out = self.run_main(dt.datetime(2026, 11, 26, 20, 48), "--challenge", "--responder", "claude-scheduled",
                            "--host-seen", "2026-11-25T19:14:24+00:00", "--host-via", "session list")
        self.assertIn("STANDBY", out)

    def test_a_newer_commit_still_wins(self):
        self.run_main(dt.datetime(2026, 11, 26, 20, 48), "--challenge", "--responder", "claude-scheduled",
                      "--host-seen", "2026-11-26T18:00:00-05:00", "--host-via", "interactive commit",
                      "--seam-log", str(self.page))
        self.assertEqual(self.events("HOST_PING_OK")[-1]["evidence"]["via"], "interactive commit")


class TestStandbyAndFallback(CliBase):
    def test_a_new_pickup_lifts_standby_on_the_next_pulse(self):
        old = "2026-11-25T19:14:24+00:00"
        self.run_main(dt.datetime(2026, 11, 26, 20, 48), "--challenge", "--responder", "claude-scheduled",
                      "--host-seen", old, "--host-via", "session list")
        self.assertTrue(self.events("HOST_UNREACHABLE"))
        self.page.write_text(docs_json(PAGE + "PICKUP | 2026-11-27 18:10 | Thessik (Claude), voice | x | y\n"),
                             encoding="utf-8")
        out = self.run_main(dt.datetime(2026, 11, 27, 20, 48), "--challenge", "--responder", "claude-scheduled",
                            "--host-seen", old, "--host-via", "session list", "--seam-log", str(self.page))
        self.assertIn("CHALLENGE", out)
        back = self.events("HOST_RETURNED")[-1]
        self.assertEqual(back["evidence"]["via"], "ZERO NODE PICKUP (Thessik (Claude), voice)")

    def test_an_unreadable_page_falls_back_and_says_so(self):
        out = self.run_main(dt.datetime(2026, 11, 26, 20, 48), "--challenge", "--responder", "claude-scheduled",
                            "--host-seen", "2026-11-26T18:00:00-05:00", "--host-via", "interactive commit",
                            "--seam-log", str(self.d / "missing.json"))
        self.assertIn("CHALLENGE", out)
        self.assertIn("unreadable", self.events("NOTE")[-1]["message"])
        self.assertEqual(self.events("HOST_PING_OK")[-1]["evidence"]["via"], "interactive commit")

    def test_unreadable_flag_with_no_other_sign_is_standby_not_a_crash(self):
        out = self.run_main(dt.datetime(2026, 11, 26, 20, 48), "--challenge", "--responder", "claude-scheduled",
                            "--no-host-evidence", "--seam-log-unreadable", "Docs connector not available")
        self.assertIn("STANDBY", out)
        self.assertIn("Docs connector not available", self.events("NOTE")[-1]["message"])

    def test_seam_stamp_prints_the_newest(self):
        out = self.run_main(dt.datetime(2026, 11, 26, 20, 48), "--seam-stamp", str(self.page))
        self.assertEqual(out.strip(), "2026-11-26T05:47:00-05:00  ZERO NODE HANDOFF (Thessik (Claude), voice)")


class TestPulseGap(CliBase):
    def test_a_missed_day_is_logged(self):
        self.run_main(dt.datetime(2026, 11, 25, 20, 48), "--challenge", "--responder", "claude-scheduled",
                      "--seam-log", str(self.page))
        self.assertFalse(self.events("PULSE_GAP"))
        self.run_main(dt.datetime(2026, 11, 27, 20, 48), "--challenge", "--responder", "claude-scheduled",
                      "--seam-log", str(self.page))
        gap = self.events("PULSE_GAP")[-1]
        self.assertEqual(gap["since"], "2026-11-25T20:48:00")
        self.assertEqual(gap["gap_hours"], 48.0)

    def test_a_daily_pulse_is_not_a_gap(self):
        for day in (25, 26):
            self.run_main(dt.datetime(2026, 11, day, 20, 48), "--challenge", "--responder", "claude-scheduled",
                          "--host-seen", f"2026-11-{day}T18:00:00-05:00", "--host-via", "commit")
        self.assertFalse(self.events("PULSE_GAP"))


if __name__ == "__main__":
    unittest.main()
