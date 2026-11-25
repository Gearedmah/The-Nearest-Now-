"""
test_heartbeat_v05_1.py - Acceptance & regression tests for Heartbeat v0.5.1.
Covers all v0.1-v0.5 behaviours plus all v0.5.1 acceptance tests.
"""

import datetime as dt
import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from heartbeat_v05_1 import (
    EPISTEMIC_NOTE,
    FakeHost,
    FakeMind,
    Heartbeat,
    ImpostorMind,
    TemplateZombie,
    Zombie,
)


def t(hour: int, minute: int = 0) -> dt.datetime:
    return dt.datetime(2026, 11, 25, hour, minute)


class TestHeartbeatV051(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_v01_to_v04_zombie_declared_down(self) -> None:
        hb = Heartbeat(Zombie(), data_dir=self.data_dir, console=None, seed=1)
        res = hb.tick(t(9, 0))
        self.assertEqual(res["ai"], "DOWN")

    def test_v03_template_zombie_passes_first_then_caught_stale(self) -> None:
        tz = TemplateZombie()
        hb = Heartbeat(tz, data_dir=self.data_dir, console=None, seed=2)
        first = hb.tick(t(9, 0))
        self.assertEqual(first["ai"], "ALIVE")
        second = hb.tick(t(9, 30))
        self.assertEqual(second["ai"], "DOWN")

    def test_v03_busy_with_valid_picture_passes(self) -> None:
        mind = FakeMind(script={9 * 60: "busy"}, seed=3)
        hb = Heartbeat(mind, data_dir=self.data_dir, console=None, seed=3)
        res = hb.tick(t(9, 0))
        self.assertEqual(res["ai"], "ALIVE")

    def test_v01_emergency_breaks_through_sleep(self) -> None:
        mind = FakeMind(seed=4)
        hb = Heartbeat(mind, data_dir=self.data_dir, console=None, seed=4)
        sleep_res = hb.tick(t(2, 0), emergency=False)
        self.assertEqual(sleep_res["ai"], "SLEEP")
        emerg_res = hb.tick(t(2, 0), emergency=True)
        self.assertEqual(emerg_res["ai"], "yes")

    def test_acceptance_1_host_only_success_leaves_ai_probation_unchanged(self) -> None:
        # AI is silent (None), Host responds 'ok'
        silent_ai = FakeMind(script={9 * 60: None}, seed=5)
        host = FakeHost(script={9 * 60: "ok"})
        hb = Heartbeat(silent_ai, host=host, data_dir=self.data_dir, console=None, seed=5)

        # Pre-seed an unapproved AI save on probation
        save = hb.saves.add(t(8, 0), {"open_thread": "keep working"})
        hb.active_candidate_id = save["id"]
        self.assertEqual(save["pings_passed"], 0)

        res = hb.tick(t(9, 0))
        self.assertEqual(res["ai"], "DOWN")
        self.assertEqual(res["host"], "HOST_ALIVE")
        self.assertEqual(hb.host_state.probation_pings, 1)

        # AI save probation must NOT have been incremented by host success
        reloaded = hb.saves.get_by_id(save["id"])
        # Repair R3 (Claude, 25 Nov 2026): this test used to expect the save to be
        # archived and removed, because DOWN with no WORKING save emptied the save
        # file. That also removed the continuity key, so the next impostor passed.
        # The save now stays put; probation is checked on it directly.
        self.assertIsNotNone(reloaded)
        self.assertEqual(reloaded["pings_passed"], 0)
        self.assertFalse(reloaded["working"])
        on_disk = json.loads((self.data_dir / "saves.json").read_text())
        self.assertEqual(on_disk[0]["pings_passed"], 0)

    def test_acceptance_2_impostor_fails_continuity_check(self) -> None:
        impostor = ImpostorMind(seed=6)
        hb = Heartbeat(impostor, data_dir=self.data_dir, console=None, seed=6)
        # Seed a save with a specific open thread
        s = hb.saves.add(t(8, 0), {"open_thread": "calibrating the compass"})
        s["working"] = True
        s["checksum"] = hb.saves.items[0]["checksum"]
        hb.saves.write()

        res = hb.tick(t(9, 0))
        self.assertEqual(res["ai"], "DOWN")

    def test_acceptance_3_fallback_model_reads_save_passes_and_takes_over(self) -> None:
        newest_jammed = FakeMind(script={9 * 60: None}, seed=7, name="newest")
        older_working = FakeMind(seed=8, name="older")
        hb = Heartbeat([newest_jammed, older_working], data_dir=self.data_dir, console=None, seed=7)

        # Create an initial save at 08:00
        hb.tick(t(8, 0))
        self.assertEqual(hb.ai.name, "newest")

        # At 09:00 newest jams -> older reads save, passes continuity, takes over
        res = hb.tick(t(9, 0))
        self.assertEqual(res["ai"], "FALLBACK_ALIVE")
        self.assertEqual(hb.ai.name, "older")

    def test_acceptance_4_restore_loads_last_working_and_next_ping_passes(self) -> None:
        # Script fails at 11:00 (and wake attempts at 11:00), then succeeds at 11:30
        mind = FakeMind(script={11 * 60: None}, seed=9, name="primary")
        hb = Heartbeat(mind, data_dir=self.data_dir, console=None, seed=9)

        # Pass 4 pings so initial save becomes WORKING (08:00, 08:30, 09:00, 09:30, 10:00)
        for hour, minute in [(8, 0), (8, 30), (9, 0), (9, 30), (10, 0)]:
            res = hb.tick(t(hour, minute))
            self.assertEqual(res["ai"], "ALIVE")

        lw = hb.saves.last_working()
        self.assertIsNotNone(lw)
        assert lw is not None

        # Mutate mind's thread to simulate drift/corruption before failure at 11:00
        mind.thread = "corrupted memory"
        fail_res = hb.tick(t(11, 0))
        self.assertEqual(fail_res["ai"], "DOWN")

        # Verify ai.restore(state) was called and verified
        self.assertEqual(mind.restored_from, lw["state"])
        self.assertEqual(mind.thread, lw["state"]["open_thread"])

        # Next ping at 11:30 passes cleanly
        recover_res = hb.tick(t(11, 30))
        self.assertEqual(recover_res["ai"], "ALIVE")

    def test_acceptance_5_standby_enters_once_backs_off_and_exits(self) -> None:
        ai = FakeMind(seed=10)
        # Host unreachable from 09:00 through 10:30, returns at 11:00
        host = FakeHost(
            script={
                9 * 60: None,
                9 * 60 + 30: None,
                10 * 60: None,
                10 * 60 + 30: None,
                11 * 60: "ok",
            }
        )
        hb = Heartbeat(ai, host=host, data_dir=self.data_dir, console=None, seed=10)

        # 09:00 -> host fails -> enters STANDBY once (backoff 30m -> next_retry 09:30)
        r1 = hb.tick(t(9, 0))
        self.assertEqual(r1["host"], "STANDBY")
        self.assertEqual(hb.host_state.liveness, "STANDBY")
        self.assertEqual(hb.host_state.standby_backoff_step, 0)

        # 09:15 -> within backoff window -> STANDBY_WAIT (no extra wake attempts/saves)
        r_wait = hb.tick(t(9, 15))
        self.assertEqual(r_wait["host"], "STANDBY_WAIT")

        # 09:30 -> retry fails -> backoff increases to step 1 (60m -> next_retry 10:30)
        r2 = hb.tick(t(9, 30))
        self.assertEqual(r2["host"], "STANDBY")
        self.assertEqual(hb.host_state.standby_backoff_step, 1)

        # 10:00 -> still in 60m backoff window -> STANDBY_WAIT
        r_wait2 = hb.tick(t(10, 0))
        self.assertEqual(r_wait2["host"], "STANDBY_WAIT")

        # 11:00 -> host returns -> exits STANDBY
        r3 = hb.tick(t(11, 0))
        self.assertEqual(r3["host"], "HOST_ALIVE")
        self.assertEqual(hb.host_state.liveness, "ALIVE")
        self.assertIsNone(hb.host_state.next_retry_at)

        # Verify STANDBY was entered only once in the append-only log
        events = [e["event"] for e in hb.log.read_all()]
        self.assertEqual(events.count("HOST_UNREACHABLE"), 1)
        self.assertEqual(events.count("HOST_RETURNED"), 1)

    def test_probation_per_save_only_advances_candidate_under_test(self) -> None:
        ai = FakeMind(seed=11)
        hb = Heartbeat(ai, data_dir=self.data_dir, console=None, seed=11)
        old_unapproved = hb.saves.add(t(7, 30), {"open_thread": "old thread"})
        new_candidate = hb.saves.add(t(8, 0), {"open_thread": "new thread"})
        hb.active_candidate_id = new_candidate["id"]

        hb.saves.pass_ping(hb.log, t(8, 30), save_id=hb.active_candidate_id)
        self.assertEqual(hb.saves.get_by_id(old_unapproved["id"])["pings_passed"], 0)
        self.assertEqual(hb.saves.get_by_id(new_candidate["id"])["pings_passed"], 1)

    def test_retry_parity_counts_probation_and_save(self) -> None:
        # Fail first ping at 09:00, succeed on wake attempt!
        class FlakyOnceMind(FakeMind):
            def __init__(self) -> None:
                super().__init__(seed=12)
                self.calls = 0

            def ask(self, msg: str, now: dt.datetime):
                self.calls += 1
                if self.calls == 1:
                    return None
                return super().ask(msg, now)

        mind = FlakyOnceMind()
        hb = Heartbeat(mind, data_dir=self.data_dir, console=None, seed=12)
        res = hb.tick(t(9, 0))
        self.assertEqual(res["ai"], "ALIVE")
        # Save must have been created on the retry success (retry parity)
        self.assertEqual(len(hb.saves.items), 1)
        self.assertIsNotNone(hb.saves.items[0]["checksum"])

    def test_append_only_log_and_epistemic_note(self) -> None:
        ai = FakeMind(seed=10)
        hb = Heartbeat(ai, data_dir=self.data_dir, console=None, seed=10)
        hb.tick(t(9, 0))
        hb.tick(t(9, 30))
        entries = hb.log.read_all()
        ok_entries = [e for e in entries if e["event"] == "AI_PING_OK"]
        self.assertGreaterEqual(len(ok_entries), 2)
        self.assertEqual(ok_entries[0]["epistemic_note"], EPISTEMIC_NOTE)

    def test_corrupt_saves_file_archived_gracefully(self) -> None:
        save_path = self.data_dir / "saves.json"
        save_path.write_text("{corrupt json", encoding="utf-8")
        hb = Heartbeat(FakeMind(seed=14), data_dir=self.data_dir, console=None, seed=14)
        self.assertEqual(hb.saves.items, [])
        backups = list(self.data_dir.glob("saves.corrupt.*.json"))
        self.assertEqual(len(backups), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)