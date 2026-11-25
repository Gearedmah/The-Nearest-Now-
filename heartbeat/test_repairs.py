"""
test_repairs.py - one test per v0.5.1 repair (R1-R12) and addition (N1-N4),
plus v0.1-v0.4 behaviours the original test file did not cover.

Every R and N class has at least one test that fails on the build as received
(git: "heartbeat v0.5.1 and EXP-005 documents, as received") and passes on the
repaired file. A few tests are guards that pass on both (e.g. non-string replies),
and TestKeptBehaviour checks things that were never broken.
Written by Claude (Claude Code session, 25 Nov 2026).
"""
import datetime as dt
import json
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import heartbeat_v05_1 as hbm
from heartbeat_v05_1 import (
    EvidenceHost,
    FakeHost,
    FakeMind,
    Heartbeat,
    ImpostorMind,
    TemplateZombie,
    Zombie,
)


def t(hour: int, minute: int = 0, day: int = 25) -> dt.datetime:
    return dt.datetime(2026, 11, day, hour, minute)


def events(hb, name=None):
    es = hb.log.read_all()
    return [e for e in es if name is None or e["event"] == name]


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def fresh(self, models, **kw):
        """A new Heartbeat object on the same data dir: what cron / --once does each run."""
        return Heartbeat(models, data_dir=self.d, console=None, **kw)


# ---------------- R1: cron / --once mode keeps what it needs ----------------
class TestR1CronMode(Base):
    def test_save_reaches_working_across_fresh_processes(self):
        for i, (h, m) in enumerate([(8, 0), (8, 30), (9, 0), (9, 30), (10, 0)]):
            self.fresh(FakeMind(seed=i, name="m"), seed=i).tick(t(h, m))
        hb = self.fresh(FakeMind(name="m"))
        self.assertIsNotNone(hb.saves.last_working())

    def test_template_zombie_caught_stale_across_fresh_processes(self):
        results = [self.fresh(TemplateZombie(), seed=i).tick(t(8, 30 * i))["ai"] for i in range(2)]
        self.assertEqual(results, ["ALIVE", "DOWN"])

    def test_active_model_survives_restart(self):
        def models():
            return [FakeMind(script={9 * 60: None}, seed=1, name="newest"), FakeMind(seed=2, name="older")]
        self.fresh(models(), seed=1).tick(t(8, 0))
        self.assertEqual(self.fresh(models(), seed=2).tick(t(9, 0))["ai"], "FALLBACK_ALIVE")
        self.assertEqual(self.fresh(models(), seed=3).ai.name, "older")


# ---------------- R2: odd replies never crash ----------------
class OddMind(FakeMind):
    def ask(self, msg, now):
        return "yes | the open thread: x"


class TestR2OddReply(Base):
    def test_judge_and_tick_survive(self):
        self.assertIsNone(hbm.judge("yes | thread: x", ["a", "b", "c"], [], None))
        self.assertEqual(self.fresh(OddMind(), seed=1).tick(t(9))["ai"], "DOWN")

    def test_non_string_reply_is_silence(self):
        class Weird(FakeMind):
            def ask(self, msg, now):
                return {"yes": True}
        self.assertEqual(self.fresh(Weird(), seed=1).tick(t(9))["ai"], "DOWN")


# ---------------- R3: DOWN with no WORKING save keeps the continuity key ----------------
class TestR3ContinuityKeyKept(Base):
    def test_impostor_still_fails_after_a_down_with_no_working_save(self):
        self.fresh(FakeMind(seed=1, name="real")).tick(t(8, 0))  # one candidate save
        self.fresh(Zombie(), seed=2).tick(t(8, 30))              # DOWN, nothing to restore
        hb = self.fresh(ImpostorMind(seed=3), seed=3)
        self.assertEqual(len(hb.saves.items), 1)
        self.assertEqual(hb.tick(t(9, 0))["ai"], "DOWN")
        self.assertTrue(events(hb, "RESTORE_SKIPPED"))


class MovingMind(FakeMind):
    """Answers from the save it read, but its own open thread moves on every ping."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.step = 0

    def ask(self, msg, now):
        self.step += 1
        return super().ask(msg, now)

    def state(self):
        return {"open_thread": f"thread {self.step}"}


# ---------------- R4: probation only for the candidate under test ----------------
class TestR4NoCandidateNoAdvance(Base):
    def test_pass_ping_without_candidate_advances_nothing(self):
        hb = self.fresh(FakeMind(seed=1))
        old = hb.saves.add(t(7, 0), {"open_thread": "old"})
        w = hb.saves.add(t(7, 30), {"open_thread": "w"})
        w["working"] = True
        hb.saves.write()
        self.assertIsNone(hb.saves.pass_ping(hb.log, t(8, 0), save_id=None))
        self.assertEqual(hb.saves.get_by_id(old["id"])["pings_passed"], 0)

    def test_old_candidate_superseded_by_newer_never_advances(self):
        # save_every_min=0 and a thread that moves on each ping: every pass writes a
        # new candidate, superseding the last one
        mind = MovingMind(seed=1)
        hb = self.fresh(mind, seed=1, config={"save_every_min": 0})
        for h, m in [(8, 0), (8, 30), (9, 0), (9, 30), (10, 0), (10, 30)]:
            self.assertEqual(hb.tick(t(h, m))["ai"], "ALIVE")
        counts = [s["pings_passed"] for s in hb.saves.items]
        self.assertEqual(counts, [1, 1, 1, 1, 1, 0])  # each advanced only while under test
        self.assertIsNone(hb.saves.last_working())


# ---------------- R5: standby is real: one save, AI held ----------------
class TestR5Standby(Base):
    def setUp(self):
        super().setUp()
        # unreachable at 09:00 and 09:30 (backoff 30m, then 60m -> next pulse 10:30); back by 11:00
        self.host = FakeHost(script={9 * 60: None, 9 * 60 + 30: None, 11 * 60: "ok"})

    def test_one_real_checkpoint_save_and_ai_held(self):
        hb = self.fresh(FakeMind(seed=1), host=self.host, seed=1)
        statuses = [hb.tick(t(h, m))["ai"] for h, m in [(9, 0), (9, 15), (9, 30), (10, 0)]]
        self.assertEqual(statuses, ["HELD"] * 4)
        saves_in_standby = events(hb, "AI_SAVE")
        self.assertEqual(len(saves_in_standby), 1)
        self.assertTrue(hb.host_state.standby_snapshot_saved)
        self.assertIsNotNone(hb.saves.get_by_id(hb.host_state.standby_snapshot_id))
        self.assertFalse(events(hb, "AI_PING_OK"))
        res = hb.tick(t(11, 0))
        self.assertEqual(res, {"ai": "ALIVE", "host": "HOST_ALIVE"})

    def test_emergency_breaks_through_standby(self):
        hb = self.fresh(FakeMind(seed=1), host=self.host, seed=1)
        hb.tick(t(9, 0))
        self.assertEqual(hb.tick(t(9, 10), emergency=True)["ai"], "ALIVE")


# ---------------- R6: no invented host ----------------
class TestR6RealHost(Base):
    def test_main_refuses_to_run_without_an_explicit_responder(self):
        with self.assertRaises(SystemExit):
            hbm.main(["--once", "--data-dir", str(self.d)])

    def test_evidence_host_answers_only_from_recent_evidence(self):
        q = "Host pulse check: reply with 'ok 4321' or 'busy 4321'."
        self.assertEqual(EvidenceHost(t(8), via="session").ask(q, t(10)), "ok 4321")
        self.assertIsNone(EvidenceHost(t(8, day=23), via="session").ask(q, t(10)))
        self.assertIsNone(EvidenceHost(None).ask(q, t(10)))

    def test_host_evidence_is_logged(self):
        hb = self.fresh(FakeMind(seed=1), host=EvidenceHost(t(8, 50), via="interactive session"), seed=1)
        hb.tick(t(9))
        ok = events(hb, "HOST_PING_OK")[0]
        self.assertEqual(ok["evidence"]["via"], "interactive session")


# ---------------- R7: the live responder reads text blocks ----------------
class TestR7ClaudeResponder(Base):
    def _fake_anthropic(self, content, stop_reason="end_turn"):
        resp = types.SimpleNamespace(content=content, stop_reason=stop_reason, model="claude-opus-5-5",
                                     usage=types.SimpleNamespace(iterations=None))
        calls = []

        class Messages:
            def create(self, **kw):
                calls.append(kw)
                return resp

        class Anthropic:
            def __init__(self, **kw):
                self.beta = types.SimpleNamespace(messages=Messages())

        return types.SimpleNamespace(Anthropic=Anthropic), calls

    def test_thinking_block_first_then_text(self):
        text = "yes: a teal fox sleeps under the bridge near a quiet kettle at dusk | the save records: x | now: y"
        mod, calls = self._fake_anthropic([types.SimpleNamespace(type="thinking", thinking=""),
                                           types.SimpleNamespace(type="text", text=text)])
        sys.modules["anthropic"] = mod
        try:
            r = hbm.ClaudeResponder()
            self.assertEqual(r.ask("q", t(9)), text)
            self.assertEqual(r.state(), {"open_thread": "y"})
            self.assertEqual(calls[0]["model"], "claude-opus-5-5")
            self.assertGreaterEqual(calls[0]["max_tokens"], 4000)
        finally:
            del sys.modules["anthropic"]

    def test_refusal_is_silence(self):
        mod, _ = self._fake_anthropic([], stop_reason="refusal")
        sys.modules["anthropic"] = mod
        try:
            self.assertIsNone(hbm.ClaudeResponder().ask("q", t(9)))
        finally:
            del sys.modules["anthropic"]


# ---------------- R8: archive is append-only ----------------
class TestR8Archive(Base):
    def test_damaged_old_archive_is_never_overwritten_and_new_lines_append(self):
        old = self.d / "saves_archive.json"
        old.write_text('[{"id": "precious"} oops', encoding="utf-8")
        hb = self.fresh(FakeMind(seed=1))
        a = hb.saves.add(t(7), {"open_thread": "a"})
        a["working"] = True
        hb.saves.write()
        hb.saves.add(t(8), {"open_thread": "b"})
        hb.saves.add(t(9), {"open_thread": "c"})
        hb.saves.rollback()
        self.assertIn("precious", old.read_text())
        archived = [r["state"]["open_thread"] for r in hb.saves.read_archive()]
        self.assertEqual(archived, ["b", "c"])
        hb.saves.add(t(10), {"open_thread": "d"})
        hb.saves.rollback()
        self.assertEqual([r["state"]["open_thread"] for r in hb.saves.read_archive()], ["b", "c", "d"])


# ---------------- R9: hanging responders time out ----------------
class Hang(FakeMind):
    def ask(self, msg, now):
        time.sleep(1.5)
        return super().ask(msg, now)


class TestR9Timeout(Base):
    def test_hang_counts_as_silence(self):
        hb = self.fresh(Hang(seed=1), config={"reply_timeout_sec": 0.05, "wake_attempts": 1})
        start = time.monotonic()
        self.assertEqual(hb.tick(t(9))["ai"], "DOWN")
        self.assertLess(time.monotonic() - start, 1.0)
        self.assertTrue(events(hb, "AI_TIMEOUT"))


# ---------------- R10: write errors don't crash ----------------
class TestR10WriteErrors(Base):
    def test_log_lines_kept_in_memory_then_written_in_order(self):
        hb = self.fresh(FakeMind(seed=1), seed=1)
        good = hb.log.path
        blocker = self.d / "blocker"
        blocker.write_text("a file, not a folder")
        hb.log.path = blocker / "log.jsonl"
        self.assertEqual(hb.tick(t(9))["ai"], "ALIVE")
        self.assertTrue(hb.log.pending)
        hb.log.path = good
        hb.log.record(t(9, 1), "NOTE", "path fixed")
        names = [e["event"] for e in hb.log.read_all()]
        self.assertEqual(names[-1], "NOTE")
        self.assertIn("AI_PASS", names)
        self.assertEqual(hb.log.verify_chain(), [])

    def test_save_write_error_logged_not_raised(self):
        hb = self.fresh(FakeMind(seed=1), seed=1)
        hb.saves.read_only = True
        self.assertEqual(hb.tick(t(9))["ai"], "ALIVE")
        self.assertTrue(events(hb, "SAVE_WRITE_ERROR"))


# ---------------- R11: restore is really checked ----------------
class NoRestore(FakeMind):
    def restore(self, state):
        pass


class TestR11Restore(Base):
    def _working(self, mind):
        hb = self.fresh(mind, seed=1)
        for h, m in [(8, 0), (8, 30), (9, 0), (9, 30), (10, 0)]:
            hb.tick(t(h, m))
        self.assertIsNotNone(hb.saves.last_working())
        return hb

    def test_restore_that_does_nothing_is_not_verified(self):
        m = NoRestore(seed=1, script={11 * 60: None})
        hb = self._working(m)
        m.thread = "corrupted"
        hb.tick(t(11, 0))
        self.assertFalse(events(hb, "RESTORE_VERIFIED"))
        self.assertTrue(events(hb, "RESTORE_VERIFY_FAILED"))

    def test_reboot_confirmed_only_when_next_ping_passes(self):
        m = FakeMind(seed=1, script={11 * 60: None})
        hb = self._working(m)
        hb.tick(t(11, 0))
        self.assertEqual(hb.ai_liveness, "RESTORED")
        self.assertFalse(events(hb, "REBOOT_CONFIRMED"))
        hb.tick(t(11, 30))
        self.assertTrue(events(hb, "REBOOT_CONFIRMED"))


# ---------------- R12: a slow clock still finishes probation ----------------
class TestR12SlowClock(Base):
    def test_two_hour_cadence_reaches_working(self):
        for i, h in enumerate([7, 9, 11, 13, 15]):
            res = self.fresh(FakeMind(seed=i, name="m"), seed=i).tick(t(h, 17))
            self.assertEqual(res["ai"], "ALIVE")
        hb = self.fresh(FakeMind(name="m"))
        self.assertEqual(len(hb.saves.items), 1)
        self.assertIsNotNone(hb.saves.last_working())

    def test_changed_state_still_saved(self):
        mind = MovingMind(seed=1)
        hb = self.fresh(mind, seed=1)
        hb.tick(t(8))
        hb.tick(t(10, 30))  # > save_every_min later, state moved on
        self.assertEqual(len(hb.saves.items), 2)


# ---------------- N1: record, not memory ----------------
class RememberingMind(FakeMind):
    def ask(self, msg, now):
        r = super().ask(msg, now)
        return r.replace(" | ", " | I remember: ") if r else r


class TestN1Overclaim(Base):
    def test_alive_but_no_probation_credit(self):
        hb = self.fresh(RememberingMind(seed=1), seed=1)
        for h, m in [(8, 0), (8, 30), (9, 0), (9, 30), (10, 0)]:
            self.assertEqual(hb.tick(t(h, m))["ai"], "ALIVE")
        self.assertEqual(hb.saves.items[0]["pings_passed"], 0)
        self.assertIsNone(hb.saves.last_working())
        self.assertEqual(len(events(hb, "AI_OVERCLAIM")), 5)

    def test_challenge_asks_for_the_record(self):
        q, _ = hbm.make_challenge(hbm.random.Random(1))
        self.assertIn("what the save records, not what you remember", q)


# ---------------- N2: session mode ----------------
def picture_for(seeds, colour="amber", n=0):
    extra = ["slowly", "quietly", "softly", "brightly", "gently", "boldly"][n % 6]
    a, b, c = seeds
    return f"a {colour} {a} {extra} drifts across the {b} toward the {c} as evening settles number{n}"


class TestN2SessionMode(Base):
    def test_challenge_answer_cycle_and_down(self):
        hb = Heartbeat([hbm.SessionResponder("claude-session")], host=EvidenceHost(t(8, 55), "session"),
                       data_dir=self.d, console=None, seed=1)
        self.assertEqual(hb.tick_host(t(9)), "HOST_ALIVE")
        p = hb.issue_challenge(t(9))
        ans = f"yes: {picture_for(p['seeds'])} | the save records: nothing yet | now: build heartbeat v0.5.1"
        hb2 = Heartbeat([hbm.SessionResponder("claude-session", ans)], data_dir=self.d, console=None)
        self.assertEqual(hb2.answer_challenge(t(9, 1), hb2.load_pending(t(9, 1)))["result"], "PASS")
        self.assertEqual(hb2.saves.items[-1]["state"], {"open_thread": "build heartbeat v0.5.1"})
        self.assertEqual(hb2.saves.items[-1]["by"], "claude-session")
        # next run: must cite the thread the save records
        hb3 = Heartbeat([hbm.SessionResponder("claude-session")], data_dir=self.d, console=None, seed=2)
        p = hb3.issue_challenge(t(9, 30))
        ans = f"yes: {picture_for(p['seeds'], 'teal', 1)} | the save records: build heartbeat v0.5.1"
        hb4 = Heartbeat([hbm.SessionResponder("claude-session", ans)], data_dir=self.d, console=None)
        self.assertEqual(hb4.answer_challenge(t(9, 31), hb4.load_pending(t(9, 31)))["result"], "PASS")
        self.assertEqual(hb4.saves.items[-1]["pings_passed"], 1)
        # an impostor session: four wrong answers -> DOWN, saves kept (no WORKING yet)
        out = {"result": None}
        for i in range(4):
            pend = Heartbeat([hbm.SessionResponder("x")], data_dir=self.d, console=None).load_pending(t(10, i)) \
                or Heartbeat([hbm.SessionResponder("x")], data_dir=self.d, console=None, seed=9).issue_challenge(t(10, i))
            bad = f"yes: {picture_for(pend['seeds'], 'red', i + 2)} | the save records: something else"
            hbx = Heartbeat([hbm.SessionResponder("x", bad)], data_dir=self.d, console=None)
            out = hbx.answer_challenge(t(10, i), pend)
        self.assertEqual(out["result"], "DOWN")
        self.assertEqual(len(hbx.saves.items), 1)

    def test_cli_round_trip(self):
        fixed = t(9, 0)
        orig = hbm.local_now
        hbm.local_now = lambda tz=None: fixed
        try:
            argv = ["--data-dir", str(self.d)]
            hbm.main(argv + ["--challenge", "--host-seen", "2026-11-25T13:40:00+00:00", "--host-via", "session"])
            pend = json.loads((self.d / "pending_challenge.json").read_text())
            ans = f"yes: {picture_for(pend['seeds'])} | the save records: none yet | now: first thread"
            hbm.main(argv + ["--answer", ans, "--responder", "claude-session"])
            hb = Heartbeat([hbm.SessionResponder("s")], data_dir=self.d, console=None)
            self.assertEqual(hb.saves.items[-1]["state"]["open_thread"], "first thread")
            self.assertIn("The save records this open thread: 'first thread'", hb.status_text(fixed))
            self.assertTrue(events(hb, "HOST_PING_OK"))
        finally:
            hbm.local_now = orig


# ---------------- N3: second-mind spot-checks ----------------
class TestN3Spotcheck(Base):
    def test_working_save_queued_and_fail_demotes(self):
        hb = self.fresh(FakeMind(seed=1), seed=1)
        for h, m in [(8, 0), (8, 30), (9, 0), (9, 30), (10, 0)]:
            hb.tick(t(h, m))
        lw = hb.saves.last_working()
        req = hb.open_spotchecks()
        self.assertEqual([r["save_id"] for r in req], [lw["id"]])
        self.assertEqual(len(req[0]["evidence"]), 4)
        self.assertTrue(all(ev["picture"] for ev in req[0]["evidence"]))
        hb.spotcheck(t(10, 5), lw["id"], "fail", by="Sean", note="pictures read like a template")
        self.assertIsNone(hb.saves.last_working())
        self.assertTrue(hb.saves.get_by_id(lw["id"])["disputed"])
        self.assertEqual(hb.open_spotchecks(), [])
        self.assertTrue(events(hb, "CORRECTION"))
        self.assertEqual(hb.saves.read_archive()[-1]["id"], lw["id"])


# ---------------- N4: hash chain and corrections ----------------
class TestN4Chain(Base):
    def test_chain_intact_then_edit_detected(self):
        hb = self.fresh(FakeMind(seed=1), seed=1)
        hb.tick(t(9))
        hb.tick(t(9, 30))
        self.assertEqual(hb.log.verify_chain(), [])
        lines = hb.log.path.read_text().splitlines()
        lines[1] = lines[1].replace("passed", "PASSED")
        hb.log.path.write_text("\n".join(lines) + "\n")
        self.assertTrue(hb.log.verify_chain())

    def test_correction_is_a_new_line(self):
        hb = self.fresh(FakeMind(seed=1), seed=1)
        hb.tick(t(9))
        first = hb.log.read_all()[0]
        n = len(hb.log.read_all())
        hb.log.correct(t(9, 5), first["id"], "this line was wrong because...", by="Claude")
        es = hb.log.read_all()
        self.assertEqual(len(es), n + 1)
        self.assertEqual(es[0], first)
        self.assertEqual(es[-1]["corrects"], first["id"])


# ---------------- kept behaviour from v0.1-v0.4 not covered elsewhere ----------------
class Parrot(FakeMind):
    """v0.2: right once, then repeats its last answer word for word."""
    def __init__(self, **kw):
        super().__init__(**kw)
        self.last = None

    def ask(self, msg, now):
        if self.last is None:
            self.last = super().ask(msg, now)
        return self.last


class StaleMind(FakeMind):
    """v0.3: the same picture each time, with the new seed words swapped in."""
    def ask(self, msg, now):
        a, b, c = msg.split("using the words ")[1].split(", and")[0].split(", ")
        return f"yes: a silver {a} rests beside the {b} while the {c} hums softly in the night | {self.thread}"


class TestKeptBehaviour(Base):
    def test_v01_bedtime_save_finishes_probation_next_morning(self):
        hb = self.fresh(FakeMind(seed=1), seed=1)
        for h, m in [(21, 0), (21, 30), (22, 0), (22, 30)]:
            hb.tick(t(h, m))
        self.assertEqual(hb.tick(t(23, 30))["ai"], "SLEEP")
        s = hb.saves.items[-1]
        self.assertFalse(s["working"])
        self.assertEqual(s["pings_passed"], 3)
        hb.tick(t(7, 0, day=26))
        self.assertTrue(hb.saves.items[0]["working"])

    def test_v02_parrot_is_down(self):
        hb = self.fresh(Parrot(seed=1), seed=1)
        self.assertEqual(hb.tick(t(9))["ai"], "ALIVE")
        self.assertEqual(hb.tick(t(9, 30))["ai"], "DOWN")

    def test_v03_stale_mind_words_swapped_is_down(self):
        hb = self.fresh(StaleMind(seed=1), seed=1)
        self.assertEqual(hb.tick(t(9))["ai"], "ALIVE")
        self.assertEqual(hb.tick(t(9, 30))["ai"], "DOWN")

    def test_v03_picture_rules(self):
        seeds = ["fox", "bridge", "kettle"]
        ok = "yes: a teal fox sleeps under the bridge near a quiet kettle at dusk | t"
        self.assertEqual(hbm.judge(ok, seeds, [], "t"), "yes")
        self.assertIsNone(hbm.judge("yes: teal fox bridge kettle | t", seeds, [], "t"))           # too thin
        self.assertIsNone(hbm.judge(ok.replace("teal", "quiet"), seeds, [], "t"))                # no colour
        self.assertIsNone(hbm.judge(ok.replace("kettle", "pot"), seeds, [], "t"))                # seed missing
        self.assertIsNone(hbm.judge(ok.replace("yes:", "maybe:"), seeds, [], "t"))               # bad head
        self.assertIsNone(hbm.judge("yes", seeds, [], "t"))                                       # bare yes
        self.assertEqual(hbm.judge(ok.replace("yes:", "busy:"), seeds, [], "t"), "busy")         # busy ok

    def test_v04_zombie_and_template_fail_on_every_model(self):
        # A zombie fails at once. This build's TemplateZombie reads the save and writes
        # 10 words, so it can pass one ping; the next is stale on every model.
        for bad, when in ((Zombie, [t(9)]), (TemplateZombie, [t(9), t(9, 30)])):
            with self.subTest(bad=bad.__name__):
                d = Path(tempfile.mkdtemp())
                a, b = bad(), bad()
                a.name, b.name = "newest", "older"
                hb = Heartbeat([a, b], data_dir=d, console=None, seed=1)
                hb.saves.add(t(8), {"open_thread": "the real thread"})
                hb.active_candidate_id = hb.saves.items[0]["id"]
                for w in when:
                    res = hb.tick(w)["ai"]
                self.assertEqual(res, "DOWN")


if __name__ == "__main__":
    unittest.main(verbosity=2)
