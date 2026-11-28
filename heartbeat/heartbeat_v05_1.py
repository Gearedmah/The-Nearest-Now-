"""
heartbeat_v05_1.py - Bidirectional Continuity Helper & Clock (v0.5.2; file name kept from v0.5.1)
Idea: Sean Battle. Original v0.1-v0.4: Thessik (Claude). v0.5.1 build: Continuity Helper.
v0.5.1 repairs (25 Nov 2026): Claude, in a Claude Code session; see "Repairs" below
and the git history, which keeps the build as received.
Open and free: no hidden costs, no gates.

Fixes in v0.5.1:
1. Continuity check restored on inward AI tick ("open thread at last save").
2. Last-working-model fallback restored (newest-first responder list).
3. Separate AI and host liveness, challenge history, and probation counters.
4. Probation per save: each save has an ID and SHA-256 checksum; only the
   specific candidate save under test is advanced/approved.
5. Retry parity: wake-attempt and fallback successes count toward probation
   and due-saving in both directions rather than returning early.
6. Real restore: ai.restore(state) + verification before claiming reboot worked.
7. Real STANDBY / HOST_UNREACHABLE state with backoff; enters once, never
   writes a new save on every repeated failure, exits when host returns.
8. Graceful failure handling: catches timeouts, bad replies, file errors, and
   missing WORKING saves; archives data before rollback/recovery, never deletes.
9. Append-only event log (JSONL) in line with the Recursor rule, recording
   the epistemic honesty question rather than claiming subjective proof.

Repairs (each has a test in test_repairs.py that fails on the build as received):
R1  Cron / --once mode kept nothing between runs: every run started a new save
    (so no save could ever reach WORKING) and forgot the picture history (so a
    template zombie was never caught as stale). The time of the last save, the
    picture history and the active model are now rebuilt from the saves file and
    the append-only log at start-up.
R2  A reply with ':' only after the '|' crashed the tick. Replies are parsed
    defensively and any judging error counts as a bad reply, not a crash.
R3  DOWN with no WORKING save archived and emptied the save file, which removed
    the continuity key: the next impostor passed with no thread to check. Saves
    are now kept when there is nothing to roll back to.
R4  pass_ping() with no candidate under test advanced the newest unapproved save
    anywhere in the file, including old superseded candidates (the bug fix 4 was
    meant to remove). With no candidate under test, nothing advances now.
R5  STANDBY set standby_snapshot_saved=True without writing a save, and kept
    pinging the AI. Entering standby now writes one real checkpoint save (only if
    the state changed) and holds the AI (no pings) until the host returns;
    emergencies still break through.
R6  main() paired a real model with FakeHost(), which always answers "ok", so a
    live run would log a host that was never asked. The host is now real
    evidence (--host-seen) or absent (NO_HOST); simulation must be asked for.
R7  ClaudeResponder read content[0].text (a thinking block on current models),
    capped output at 120 tokens and defaulted to an old model. Fixed; the model
    that actually served the reply is recorded.
R8  The save archive was rewritten whole each time; a damaged archive was
    replaced by an empty one. The archive is now append-only JSONL, and a save
    is only trimmed after its archive line is written.
R9  A responder that hung blocked the heartbeat forever. Calls now time out and
    count as silence.
R10 A log or save-file write error crashed the tick. Writes that fail are kept
    in memory and retried, and the tick carries on.
R11 Restore was "verified" through read_save(), so a restore() that did nothing
    still passed. The state is now checked straight after restore(), and the
    reboot is only called confirmed when the next ping passes.
R12 A save was written every save_every_min even when nothing had changed, which
    replaced the candidate under test. With a clock slower than save_every_min
    (a scheduled run every 2 hours) every run wrote a new save and none could
    finish probation. A new save is now written only when the state changed.
New in the repairs:
N1  Record, not memory (EXP-005 P8): the continuity question asks what the save
    records. A reply that claims to remember stays alive but earns no probation.
N2  Session mode (--challenge / --answer): the answering mind can be the Claude
    session the scheduler wakes, so the clock runs on a plan, not an API bill.
N3  Second-mind spot-checks: every save stamped WORKING is queued for a person or
    another AI to review; a "fail" verdict demotes the save, as a new log line.
N4  The log is hash-chained, and corrections are new CORRECTION lines.
v0.5.2 (28 Nov 2026, Claude Code session, from the "Drive pickup fix" brief):
D1  The ZERO NODE seam log is a host sign: --seam-log reads the page, and its
    newest PICKUP/HANDOFF stamp (seamlog.py) is compared with --host-seen; the
    newer wins. Voice and chat sessions stamp the page but leave no commit.
D2  An unreadable page falls back to the other sources and is logged as a NOTE.
D3  A gap of more than pulse_gap_hours since the log's last event is logged as
    PULSE_GAP, so a missed pulse isn't read as a quiet day.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import hashlib
import json
import random
import re
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

import seamlog
from provenance import overclaims

VERSION = "0.5.2"

CONFIG: dict[str, Any] = {
    "ping_every_min": 30,
    "awake_from": 7,
    "awake_until": 23,
    "timezone": "America/New_York",
    "probation_pings": 4,
    "wake_attempts": 3,
    "save_every_min": 120,
    "host_ping_every_min": 30,
    "host_wake_attempts": 3,
    "host_standby_backoff_min": [30, 60, 120, 240],
    "host_seen_window_hours": 24,
    "pulse_gap_hours": 26,
    "reply_timeout_sec": 120,
    "max_word_overlap": 0.5,
    "history_window": 5,
    "data_dir": "heartbeat_data",
    "save_file": "saves.json",
    "archive_file": "saves_archive.jsonl",
    "log_file": "heartbeat_log.jsonl",
    "host_state_file": "host_state.json",
    "pending_file": "pending_challenge.json",
    "pending_expires_min": 60,
}

EPISTEMIC_NOTE = (
    "Well-formed continuous output observed; this does not prove the same "
    "subjective mind continued."
)

# ---------- challenge: "what are you looking at?" -> fresh picture + thread ----------
SEEDS = (
    "lighthouse violin rain fox lantern bridge orchard comet kettle river "
    "owl ladder mirror harbor snow bell garden clock feather train desert "
    "candle whale market forest key storm island drum moth"
).split()

COLOURS = set(
    "red orange yellow green blue indigo violet purple pink white "
    "black grey gray gold silver brown amber teal crimson".split()
)


def make_challenge(rng: random.Random) -> tuple[str, list[str]]:
    words = rng.sample(SEEDS, 3)
    q = (
        "What are you looking at? Paint it in one sentence using the words "
        + ", ".join(words)
        + ", and name a colour. Then, after a '|', "
        "say the open thread at your last save. You read it in the save; "
        "say what the save records, not what you remember."  # N1
    )
    return q, words


def _words(s: str) -> list[str]:
    return [w.strip(".,!?;:'\"").lower() for w in s.split() if w.strip(".,!?;:'\"")]


def parse_reply(reply: Any) -> Optional[dict[str, Any]]:
    """'yes: <picture> | <thread from the save> [| now: <open thread now>]' -> parts.

    Returns None for anything that isn't that shape (R2: never raises).
    """
    if not isinstance(reply, str) or ":" not in reply or "|" not in reply:
        return None
    segs = [s.strip() for s in reply.split("|")]
    if ":" not in segs[0]:
        return None
    head, pic = segs[0].split(":", 1)
    said, now_thread = "", None
    for s in segs[1:]:
        if s.lower().startswith("now:"):
            now_thread = s[4:].strip() or None
        elif not said:
            said = s
    return {"head": head.strip().lower(), "picture": pic.strip(), "said": said, "now": now_thread}


def judge(
    reply: Optional[str],
    seeds: list[str],
    history: list[str],
    thread: Optional[str] = None,
    max_overlap: float = CONFIG["max_word_overlap"],
    history_window: int = CONFIG["history_window"],
) -> Optional[str]:
    """Judge AI reply: 'yes: <picture> | <thread>'. Returns 'yes'/'busy'/None."""
    p = parse_reply(reply)
    if p is None:
        return None
    if thread:
        if thread.strip().lower() not in p["said"].strip().lower():
            return None
    head = p["head"]
    if head not in ("yes", "busy"):
        return None
    w = _words(p["picture"])
    if not (8 <= len(w) <= 30):
        return None
    if not all(s in w for s in seeds):
        return None
    if not (COLOURS & set(w)):
        return None
    ws = set(w) - set(seeds)
    for old in history[-history_window:]:
        o = set(_words(old))
        union = ws | o
        if ws and union and (len(ws & o) / len(union) > max_overlap):
            return None
    return head


def make_host_challenge(rng: random.Random) -> tuple[str, str]:
    token = f"{rng.randint(1000, 9999)}"
    q = f"Host pulse check: reply with 'ok {token}' or 'busy {token}'."
    return q, token


def judge_host(reply: Optional[str], token: str) -> Optional[str]:
    """Judge host reply: 'ok <token>', 'yes <token>', or 'busy <token>'."""
    if not isinstance(reply, str):
        return None
    parts = reply.strip().lower().split()
    if len(parts) < 2:
        return None
    if parts[0] not in ("ok", "yes", "busy"):
        return None
    if token.lower() not in parts[1:]:
        return None
    return parts[0]


def local_now(tz: str = CONFIG["timezone"]) -> dt.datetime:
    """Wall-clock time in the heartbeat's time zone, without tzinfo (awake hours are local)."""
    from zoneinfo import ZoneInfo

    return dt.datetime.now(ZoneInfo(tz)).replace(tzinfo=None, microsecond=0)


def to_local(value: str, tz: str = CONFIG["timezone"]) -> dt.datetime:
    """Parse an ISO time; times with an offset are converted to the heartbeat's zone."""
    from zoneinfo import ZoneInfo

    t = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if t.tzinfo is not None:
        t = t.astimezone(ZoneInfo(tz)).replace(tzinfo=None)
    return t


def _call_with_timeout(fn: Callable[..., Any], timeout: Optional[float], *args: Any) -> Any:
    """R9: run fn(*args); raise TimeoutError if it takes longer than `timeout` seconds."""
    if not timeout:
        return fn(*args)
    ex = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    try:
        return ex.submit(fn, *args).result(timeout=timeout)
    except concurrent.futures.TimeoutError as exc:
        raise TimeoutError(f"no reply within {timeout}s") from exc
    finally:
        ex.shutdown(wait=False, cancel_futures=True)


# ---------- responders ----------
class FakeMind:
    """Toy AI mind for testing: builds a new picture from seeds and tracks state."""

    def __init__(
        self,
        script: Optional[dict[int, Optional[str]]] = None,
        seed: int = 0,
        name: str = "fake",
        read_saves: bool = True,
    ):
        self.r = random.Random(seed)
        self.script = script or {}
        self.name = name
        self.read_saves = read_saves
        self.thread = ""
        self.restored_from: Optional[dict[str, Any]] = None
        self.scenes = [
            "at dusk",
            "under a low sky",
            "beside an old wall",
            "in the morning hush",
            "after the storm passed",
            "where the path bends",
            "as the tide turns",
            "at the edge of town",
        ]
        self.verbs = [
            "leans against",
            "hums beside",
            "drifts past",
            "waits near",
            "glints behind",
            "sleeps under",
            "spins above",
            "rests by",
        ]

    def ask(self, msg: str, now: dt.datetime) -> Optional[str]:
        k = now.hour * 60 + now.minute
        mode = self.script.get(k, "yes")
        if mode is None:
            return None
        seeds = msg.split("using the words ")[1].split(", and")[0].split(", ")
        c = self.r.choice(sorted(COLOURS))
        a, b, d = seeds
        return (
            f"{mode}: a {c} {a} {self.r.choice(self.verbs)} the {b} "
            f"{self.r.choice(self.scenes)} while {d} {self.r.choice(self.verbs)} it"
            f" | {self.thread}"
        )

    def read_save(self, save: Optional[dict[str, Any]]) -> None:
        if not self.read_saves:
            return
        self.thread = save["state"].get("open_thread", "") if save else ""

    def state(self) -> dict[str, Any]:
        return {"open_thread": self.thread or "start the day"}

    def restore(self, state: dict[str, Any]) -> None:
        self.thread = state.get("open_thread", "")
        self.restored_from = copy.deepcopy(state)


class ImpostorMind(FakeMind):
    """A mind that never read the save; fails continuity checks."""

    def __init__(self, seed: int = 99, name: str = "impostor"):
        super().__init__(seed=seed, name=name, read_saves=False)
        self.thread = "wrong unrelated thread"


class Zombie:
    name = "zombie"

    def read_save(self, save: Optional[dict[str, Any]]) -> None:
        pass

    def ask(self, msg: str, now: dt.datetime) -> str:
        return "yes"

    def state(self) -> dict[str, Any]:
        return {"open_thread": "nobody home"}

    def restore(self, state: dict[str, Any]) -> None:
        pass


class TemplateZombie:
    """Copies seed words into a fixed frame. Caught as stale on the next ping."""

    name = "template"

    def __init__(self) -> None:
        self.thread = ""

    def read_save(self, save: Optional[dict[str, Any]]) -> None:
        self.thread = save["state"].get("open_thread", "") if save else ""

    def ask(self, msg: str, now: dt.datetime) -> str:
        seeds = msg.split("using the words ")[1].split(", and")[0].split(", ")
        return f"yes: I see a blue {' '.join(seeds)} right here now | {self.thread}"

    def state(self) -> dict[str, Any]:
        return {"open_thread": self.thread or "template"}

    def restore(self, state: dict[str, Any]) -> None:
        self.thread = state.get("open_thread", "")


class ClaudeResponder:
    """Live Anthropic API responder (optional; API use is billed).

    Needs `pip install anthropic` and credentials (ANTHROPIC_API_KEY or `ant auth login`).
    R7: reads text blocks (current models put a thinking block first), leaves room
    for thinking, handles refusals, and records which model actually served the reply.
    """

    def __init__(self, model: str = "claude-opus-5-5", timeout_sec: float = 60.0, effort: str = "low"):
        import anthropic

        self.client = anthropic.Anthropic(timeout=timeout_sec, max_retries=1)
        self.model = model
        self.name = model
        self.effort = effort
        self.save_text = ""
        self._thread = "start the day"
        self.last_meta: dict[str, Any] = {}

    def read_save(self, save: Optional[dict[str, Any]]) -> None:
        self.save_text = json.dumps(save) if save else ""
        if save and "state" in save:
            self._thread = save["state"].get("open_thread", self._thread)

    def ask(self, msg: str, now: dt.datetime) -> Optional[str]:
        prompt = (
            "You are answering a heartbeat check. Below is the last save, a record written "
            "by an earlier run. Read it as a record, not as your memory.\n"
            f"Last save: {self.save_text or '(none yet)'}\n\n"
            f"{msg}\nAnswer on one line: start with 'yes:' or 'busy:', then the picture, "
            "then ' | the save records: <thread>', then optionally ' | now: <your open thread now>'."
        )
        r = self.client.beta.messages.create(
            model=self.model,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": self.effort},
            messages=[{"role": "user", "content": prompt}],
        )
        fallback_ran = any(
            getattr(e, "type", "") == "fallback_message"
            for e in (getattr(r.usage, "iterations", None) or [])
        )
        self.last_meta = {"served_by": getattr(r, "model", self.model),
                          "fallback_ran": fallback_ran, "stop_reason": r.stop_reason}
        if r.stop_reason == "refusal":
            return None
        text = " ".join(b.text for b in r.content if getattr(b, "type", "") == "text").strip()
        p = parse_reply(text)
        if p and p["now"]:
            self._thread = p["now"]
        return text or None

    def state(self) -> dict[str, Any]:
        return {"open_thread": self._thread}

    def restore(self, state: dict[str, Any]) -> None:
        self._thread = state.get("open_thread", "start the day")
        self.save_text = json.dumps({"state": state})


class SessionResponder:
    """N2: the answering mind is the Claude session running this command.

    Used by --challenge / --answer, so a scheduled Claude session (on Sean's plan,
    no API key) can be the one the clock wakes. Its state is the open thread it
    reports with '| now: ...', or else the thread of the save it read.
    """

    def __init__(self, name: str, reply: Optional[str] = None):
        self.name = name
        self.reply = reply
        self._state: Optional[dict[str, Any]] = None
        p = parse_reply(reply)
        if p and p["now"]:
            self._state = {"open_thread": p["now"]}

    def ask(self, msg: str, now: dt.datetime) -> Optional[str]:
        return self.reply

    def read_save(self, save: Optional[dict[str, Any]]) -> None:
        if self._state is None and save and isinstance(save.get("state"), dict):
            self._state = copy.deepcopy(save["state"])

    def restore(self, state: dict[str, Any]) -> None:
        self._state = copy.deepcopy(state)

    def state(self) -> dict[str, Any]:
        return self._state or {"open_thread": "no open thread recorded yet"}


class FakeHost:
    """Toy human host responder for testing outward pulses."""

    def __init__(self, script: Optional[dict[int, Optional[str]]] = None, name: str = "host"):
        self.script = script or {}
        self.name = name

    def ask(self, msg: str, now: dt.datetime) -> Optional[str]:
        k = now.hour * 60 + now.minute
        mode = self.script.get(k, "ok")
        if mode is None:
            return None
        # Extract token between quotes: 'ok 1234'
        token = msg.split("'ok ")[1].split("'")[0]
        return f"{mode} {token}"


class EvidenceHost:
    """R6: the host is a person, who can't answer a token every 30 minutes.

    The outward pulse answers for them only from evidence that they were here
    recently: an interactive session, a commit, a note. No evidence, no answer.
    """

    def __init__(self, seen_at: Optional[dt.datetime], via: str = "unspecified",
                 window_hours: float = CONFIG["host_seen_window_hours"], name: str = "host"):
        self.seen_at = seen_at
        self.via = via
        self.window = dt.timedelta(hours=window_hours)
        self.name = name
        self.last_evidence: Optional[dict[str, Any]] = None

    def ask(self, msg: str, now: dt.datetime) -> Optional[str]:
        if self.seen_at is None:
            return None
        age = now - self.seen_at
        if age < -dt.timedelta(minutes=5) or age > self.window:
            return None
        m = re.search(r"'ok (\w+)'", msg)
        if not m:
            return None
        self.last_evidence = {"seen_at": self.seen_at.isoformat(), "via": self.via,
                              "age_min": int(age.total_seconds() // 60)}
        return f"ok {m.group(1)}"


# ---------- append-only Recursor log ----------
def _chain(prev: str, entry: dict[str, Any]) -> str:
    raw = prev + json.dumps(entry, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


class AppendOnlyLog:
    """Append-only JSONL event log. Corrections go in as new lines; never erases.

    N4: each line carries "h", a hash of the previous line's hash and this entry,
    so an edited or removed line shows up as a break in the chain.
    R10: a line that can't be written is kept in memory and written on the next
    successful append, in order.
    """

    def __init__(self, path: Path, console: Optional[Callable[[str], None]] = print):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.console = console
        self.pending: list[str] = []
        self.write_error: Optional[str] = None
        self._last_hash = "0" * 16
        for e in self.read_all():
            self._last_hash = e.get("h", self._last_hash)

    def record(
        self,
        now: dt.datetime,
        event_type: str,
        message: str,
        **fields: Any,
    ) -> dict[str, Any]:
        entry = {
            "id": f"evt_{uuid.uuid4().hex[:10]}",
            "time": now.isoformat(),
            "event": event_type,
            "message": message,
            **fields,
        }
        entry["h"] = _chain(self._last_hash, entry)
        self._last_hash = entry["h"]
        self.pending.append(json.dumps(entry, ensure_ascii=False))
        self.flush()
        if self.console:
            self.console(f"{now:%H:%M}  [{event_type}] {message}")
        return entry

    def flush(self) -> bool:
        if not self.pending:
            return True
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as f:
                f.write("".join(line + "\n" for line in self.pending))
                f.flush()
            self.pending.clear()
            self.write_error = None
            return True
        except OSError as exc:
            if self.write_error is None:
                print(f"heartbeat: log write failed ({exc}); keeping "
                      f"{len(self.pending)} line(s) in memory", file=sys.stderr)
            self.write_error = str(exc)
            return False

    def correct(self, now: dt.datetime, event_id: str, note: str, by: str) -> dict[str, Any]:
        """A correction is a new line pointing at the old one; the old line stays."""
        return self.record(now, "CORRECTION", note, corrects=event_id, by=by)

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        try:
            raw_text = self.path.read_text(encoding="utf-8")
        except OSError:
            return []
        for raw in raw_text.splitlines():
            if raw.strip():
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return entries

    def verify_chain(self) -> list[str]:
        """Lines whose hash doesn't follow from the line before (edited, removed or unreadable)."""
        problems, prev = [], "0" * 16
        if not self.path.exists():
            return problems
        for n, raw in enumerate(self.path.read_text(encoding="utf-8").splitlines(), 1):
            if not raw.strip():
                continue
            try:
                e = json.loads(raw)
            except json.JSONDecodeError:
                problems.append(f"line {n}: not JSON")
                continue
            if "h" not in e:  # lines written before the chain existed
                continue
            body = {k: v for k, v in e.items() if k != "h"}
            if _chain(prev, body) != e["h"]:
                problems.append(f"line {n} ({e.get('id')}): chain break")
            prev = e["h"]
        return problems


# ---------- save store with IDs, checksums, per-save probation, and archiving ----------
def compute_checksum(save_id: str, time_iso: str, state: dict[str, Any]) -> str:
    payload = json.dumps(
        {"id": save_id, "time": time_iso, "state": state},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class Saves:
    def __init__(
        self,
        path: Path,
        archive_path: Optional[Path] = None,
        probation_pings: int = CONFIG["probation_pings"],
    ):
        self.path = Path(path)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        self.archive_path = Path(archive_path) if archive_path else self.path.with_name(CONFIG["archive_file"])
        self.probation_pings = probation_pings
        self.read_only = False  # set when a damaged save file could not be backed up
        self.corrupt_backup: Optional[Path] = None
        self.last_write_error: Optional[str] = None
        self.items: list[dict[str, Any]] = self._load_safe()

    def _load_safe(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("saves.json must be a list")
            return data
        except (OSError, ValueError, json.JSONDecodeError):
            self._archive_corrupt_file()
            return []

    def _archive_corrupt_file(self) -> None:
        if not self.path.exists():
            return
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = self.path.with_name(f"{self.path.stem}.corrupt.{stamp}{self.path.suffix}")
        try:
            backup.write_bytes(self.path.read_bytes())
            self.corrupt_backup = backup
        except OSError:
            # Never overwrite data we could not copy somewhere first.
            self.read_only = True

    def _append_archive(self, records: list[dict[str, Any]], reason: str) -> bool:
        """R8: append-only JSONL. Returns False if the archive could not be written."""
        if not records:
            return True
        archived_at = dt.datetime.now(dt.timezone.utc).isoformat()
        lines = []
        for rec in records:
            entry = copy.deepcopy(rec)
            entry["archived_at"] = archived_at
            entry["archive_reason"] = reason
            lines.append(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            self.archive_path.parent.mkdir(parents=True, exist_ok=True)
            with self.archive_path.open("a", encoding="utf-8") as f:
                f.write("".join(lines))
            return True
        except OSError as exc:
            self.last_write_error = f"archive: {exc}"
            return False

    def read_archive(self) -> list[dict[str, Any]]:
        if not self.archive_path.exists():
            return []
        out = []
        for raw in self.archive_path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    out.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return out

    def write(self) -> bool:
        if self.read_only:
            self.last_write_error = "save file damaged and not backed up; refusing to overwrite it"
            return False
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, indent=2, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.path)
            return True
        except OSError as exc:
            self.last_write_error = str(exc)
            return False

    @staticmethod
    def verify_checksum(save: dict[str, Any]) -> bool:
        sid = save.get("id")
        t = save.get("time")
        st = save.get("state")
        cs = save.get("checksum")
        if not sid or not t or not isinstance(st, dict) or not cs:
            return False
        return compute_checksum(sid, t, st) == cs

    def add(self, now: dt.datetime, state: dict[str, Any], by: Optional[str] = None) -> dict[str, Any]:
        prev = self.last_working()
        save_id = f"save_{uuid.uuid4().hex[:12]}"
        time_iso = now.isoformat()
        rec = {
            "id": save_id,
            "time": time_iso,
            "pings_passed": 0,
            "passed_by": [],
            "working": False,
            "prev_working": prev["id"] if prev else None,
            "by": by,
            "state": copy.deepcopy(state),
            "checksum": compute_checksum(save_id, time_iso, state),
        }
        self.items.append(rec)
        self.write()
        return rec

    def get_by_id(self, save_id: str) -> Optional[dict[str, Any]]:
        for s in self.items:
            if s.get("id") == save_id:
                return s
        return None

    def pass_ping(
        self,
        log: AppendOnlyLog,
        now: dt.datetime,
        save_id: Optional[str] = None,
        ping_ref: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Advance probation ONLY for the specific candidate save under test.

        R4: with no candidate named, nothing advances. Falling back to "the newest
        unapproved save" could pick an old superseded candidate.
        """
        if save_id is None:
            return None
        target = self.get_by_id(save_id)

        if not target or target.get("working"):
            return None
        if not self.verify_checksum(target):
            log.record(now, "SAVE_CHECKSUM_MISMATCH", f"save {target.get('id')} failed checksum")
            return None

        target["pings_passed"] = int(target.get("pings_passed", 0)) + 1
        if ping_ref:
            target.setdefault("passed_by", []).append(ping_ref)
        if target["pings_passed"] >= self.probation_pings:
            target["working"] = True
            log.record(
                now,
                "SAVE_WORKING",
                f"save {target['id']} ({target['time'][11:16]}) stamped WORKING",
                save_id=target["id"],
                passed_by=list(target.get("passed_by", [])),
            )
        self.write()
        return target

    def last_working(self) -> Optional[dict[str, Any]]:
        w = [s for s in self.items if s.get("working") and self.verify_checksum(s)]
        return w[-1] if w else None

    def rollback(self) -> Optional[dict[str, Any]]:
        """Archive newer saves and roll back to the last WORKING save.

        R3: with no WORKING save there is nothing to roll back to, and the saves
        stay where they are: the newest one is still the continuity key.
        R8: newer saves are only trimmed once their archive lines are written.
        """
        lw = self.last_working()
        if lw is None:
            return None
        idx = self.items.index(lw)
        discarded = self.items[idx + 1 :]
        if discarded and not self._append_archive(discarded, reason="rollback_to_last_working"):
            return lw  # could not archive: keep everything, still restore from lw
        self.items = self.items[: idx + 1]
        self.write()
        return lw


# ---------- separate Host state ----------
@dataclass
class HostState:
    liveness: str = "UNKNOWN"  # UNKNOWN | ALIVE | HOST_UNREACHABLE | STANDBY
    probation_pings: int = 0
    challenge_history: list[str] = field(default_factory=list)
    standby_entered_at: Optional[str] = None
    standby_backoff_step: int = 0
    next_retry_at: Optional[str] = None
    standby_snapshot_saved: bool = False
    standby_snapshot_id: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "liveness": self.liveness,
            "probation_pings": self.probation_pings,
            "challenge_history": self.challenge_history[-20:],
            "standby_entered_at": self.standby_entered_at,
            "standby_backoff_step": self.standby_backoff_step,
            "next_retry_at": self.next_retry_at,
            "standby_snapshot_saved": self.standby_snapshot_saved,
            "standby_snapshot_id": self.standby_snapshot_id,
        }

    @classmethod
    def load(cls, path: Path) -> "HostState":
        if not path.exists():
            return cls()
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})
        except (OSError, json.JSONDecodeError, TypeError):
            return cls()

    def save(self, path: Path) -> bool:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")
            return True
        except OSError as exc:
            print(f"heartbeat: host state not written ({exc})", file=sys.stderr)
            return False


# ---------- the bidirectional heartbeat ----------
class Heartbeat:
    def __init__(
        self,
        models: list[Any] | Any,
        host: Optional[Any] = None,
        data_dir: Optional[str | Path] = None,
        console: Optional[Callable[[str], None]] = print,
        seed: Optional[int] = None,
        config: Optional[dict[str, Any]] = None,
        second_mind_judge: Optional[Callable[[str, list[str], Optional[str]], bool]] = None,
    ):
        self.cfg = {**CONFIG, **(config or {})}
        self.data_dir = Path(data_dir or self.cfg["data_dir"])
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass

        self.models = models if isinstance(models, list) else [models]
        if not self.models:
            raise ValueError("At least one AI responder model is required.")
        self.i = 0
        self.ai = self.models[0]

        self.host = host
        self.host_state_path = self.data_dir / self.cfg["host_state_file"]
        self.host_state = HostState.load(self.host_state_path)

        self.log = AppendOnlyLog(self.data_dir / self.cfg["log_file"], console=console)
        self.saves = Saves(
            self.data_dir / self.cfg["save_file"],
            archive_path=self.data_dir / self.cfg["archive_file"],
            probation_pings=self.cfg["probation_pings"],
        )

        # AI state is strictly separate from host state
        self.ai_liveness: str = "UNKNOWN"
        self.ai_history: list[str] = []
        self.last_save_time: Optional[dt.datetime] = None
        self.active_candidate_id: Optional[str] = None
        self.last_check: dict[str, Any] = {}
        self._next_challenge: Optional[tuple[str, list[str]]] = None
        self._rebuild_from_record()  # R1
        if self.saves.items:
            latest = self.saves.items[-1]
            if not latest.get("working"):
                self.active_candidate_id = latest.get("id")
            self._safe_read_save(self.ai, latest)

        self.rng = random.Random(seed)
        self.second_mind_judge = second_mind_judge
        if self.saves.corrupt_backup or self.saves.read_only:
            self.log.record(
                local_now(self.cfg["timezone"]),
                "SAVES_DAMAGED",
                "save file was not readable; "
                + (f"copied to {self.saves.corrupt_backup.name}" if self.saves.corrupt_backup
                   else "could not be backed up, so it will not be overwritten"),
            )

    # R1: what a fresh process needs from earlier runs, read from the record.
    def _rebuild_from_record(self) -> None:
        active_name = None
        for e in self.log.read_all():
            if e.get("picture"):
                self.ai_history.append(e["picture"])
            if e.get("active_model"):
                active_name = e["active_model"]
            if e.get("ai_liveness"):
                self.ai_liveness = e["ai_liveness"]
        self.ai_history = self.ai_history[-(self.cfg["history_window"] * 4):]
        self.recorded_active_model = active_name
        names = [getattr(m, "name", None) for m in self.models]
        if active_name in names:
            self.i = names.index(active_name)
            self.ai = self.models[self.i]
        if self.saves.items:
            try:
                self.last_save_time = dt.datetime.fromisoformat(self.saves.items[-1]["time"])
            except (KeyError, TypeError, ValueError):
                self.last_save_time = None

    def _safe_read_save(self, model: Any, save: Optional[dict[str, Any]]) -> None:
        try:
            model.read_save(save)
        except Exception as exc:
            self.log.record(
                local_now(self.cfg["timezone"]),
                "READ_SAVE_ERROR",
                f"{getattr(model, 'name', 'model')} failed read_save: {exc}",
            )

    def awake(self, now: dt.datetime) -> bool:
        return self.cfg["awake_from"] <= now.hour < self.cfg["awake_until"]

    def _check_ai(self, prefix: str, now: dt.datetime) -> tuple[str, Optional[str], Optional[str]]:
        if self._next_challenge is not None:  # session mode: the challenge already issued
            q, seeds = self._next_challenge
            self._next_challenge = None
        else:
            q, seeds = make_challenge(self.rng)
        self.last_check = {"seeds": seeds}
        name = getattr(self.ai, "name", "ai")
        try:
            reply = _call_with_timeout(self.ai.ask, self.cfg["reply_timeout_sec"], f"{prefix} {q}", now)
        except TimeoutError as exc:
            self.log.record(now, "AI_TIMEOUT", f"{name}: {exc}", model=name)
            return "/".join(seeds), None, None
        except Exception as exc:
            self.log.record(now, "AI_ASK_ERROR", f"{name} raised: {exc}")
            return "/".join(seeds), None, None

        last = self.saves.items[-1] if self.saves.items else None
        thread = last["state"].get("open_thread") if last and isinstance(last.get("state"), dict) else None
        try:
            verdict = judge(
                reply,
                seeds,
                self.ai_history,
                thread=thread,
                max_overlap=self.cfg["max_word_overlap"],
                history_window=self.cfg["history_window"],
            )
        except Exception as exc:  # R2: a judging error is a bad reply, never a crash
            self.log.record(now, "BAD_REPLY", f"{name}: could not judge reply ({exc})", model=name)
            verdict = None
        if verdict and self.second_mind_judge is not None:
            try:
                if not self.second_mind_judge(reply, seeds, thread):
                    verdict = None
            except Exception as exc:
                self.log.record(now, "SECOND_MIND_ERROR", f"spot-check error: {exc}")

        if verdict and isinstance(reply, str):
            p = parse_reply(reply) or {}
            pic = p.get("picture", "")
            self.ai_history.append(pic)
            self.last_check.update({
                "picture": pic,
                "said": p.get("said", ""),
                "overclaims": [c["match"] for c in overclaims(reply)],  # N1
                "meta": dict(getattr(self.ai, "last_meta", {}) or {}),
            })
        return "/".join(seeds), reply, verdict

    def _on_ai_success(self, now: dt.datetime) -> None:
        """Shared success path for initial ping, wake retry, and model fallback (retry parity)."""
        name = getattr(self.ai, "name", "ai")
        was = self.ai_liveness
        self.ai_liveness = "ALIVE"
        chk = self.last_check
        flagged = chk.get("overclaims") or []
        # The one record every success path writes. Its picture rebuilds the
        # staleness history in later runs (R1).
        ping = self.log.record(
            now,
            "AI_PASS",
            f"{name} passed; candidate under test: {self.active_candidate_id or 'none'}",
            model=name,
            active_model=name,
            ai_liveness="ALIVE",
            seeds=chk.get("seeds"),
            picture=chk.get("picture"),
            thread_said=chk.get("said"),
            overclaims=flagged,
            served=chk.get("meta") or None,
            epistemic_note=EPISTEMIC_NOTE,
        )
        if was == "RESTORED":
            self.log.record(now, "REBOOT_CONFIRMED",
                            "first ping after restore passed; the reboot worked", model=name)
        if flagged:  # N1: alive, but no probation credit for claiming a memory
            self.log.record(
                now, "AI_OVERCLAIM",
                "reply spoke of the save as a memory; alive, but this ping does not count "
                "toward probation (EXP-005 P8)",
                model=name, matches=flagged, ping=ping["id"],
            )
        else:
            before = None
            if self.active_candidate_id:
                s = self.saves.get_by_id(self.active_candidate_id)
                before = bool(s and s.get("working"))
            target = self.saves.pass_ping(self.log, now, save_id=self.active_candidate_id,
                                          ping_ref=ping["id"])
            if target is not None and target.get("working") and not before:
                self._request_spotcheck(now, target)  # N3

        due = (
            self.last_save_time is None
            or (now - self.last_save_time).total_seconds() >= self.cfg["save_every_min"] * 60
        )
        if due:
            try:
                ai_state = self.ai.state()
            except Exception as exc:
                self.log.record(now, "AI_STATE_ERROR", f"failed reading ai.state(): {exc}")
                return
            if not isinstance(ai_state, dict):
                self.log.record(now, "AI_STATE_ERROR", f"ai.state() returned {type(ai_state).__name__}")
                return
            # R12: a save is only written when there is something new to save. Re-saving
            # an unchanged state replaced the candidate under test every time, so with a
            # clock slower than save_every_min no save could ever finish probation.
            latest = self.saves.items[-1] if self.saves.items else None
            if latest is None or latest.get("state") != ai_state:
                self._write_save(now, ai_state, reason="on probation")

    def _write_save(self, now: dt.datetime, state: dict[str, Any], reason: str) -> dict[str, Any]:
        name = getattr(self.ai, "name", "ai")
        new_save = self.saves.add(now, state, by=name)
        self.last_save_time = now
        self.active_candidate_id = new_save["id"]
        self._safe_read_save(self.ai, new_save)
        self.log.record(
            now,
            "AI_SAVE",
            f"save {new_save['id']} {reason}",
            save_id=new_save["id"],
            save=new_save,  # the full record, so a damaged saves.json can be rebuilt by hand
        )
        return new_save

    # N3: the stronger judge
    def _request_spotcheck(self, now: dt.datetime, save: dict[str, Any]) -> None:
        refs = set(save.get("passed_by", []))
        evidence = [
            {"ping": e["id"], "time": e["time"], "model": e.get("model"),
             "seeds": e.get("seeds"), "picture": e.get("picture"), "thread_said": e.get("thread_said")}
            for e in self.log.read_all() if e.get("id") in refs
        ]
        self.log.record(
            now, "SPOTCHECK_REQUESTED",
            f"save {save['id']} was stamped WORKING by {len(refs)} pings; a second mind "
            "(a person or another AI) should read them: fresh pictures? the right thread?",
            save_id=save["id"], evidence=evidence,
        )

    def open_spotchecks(self) -> list[dict[str, Any]]:
        entries = self.log.read_all()
        done = {e.get("save_id") for e in entries if e.get("event") == "SPOTCHECK_VERDICT"}
        return [e for e in entries if e.get("event") == "SPOTCHECK_REQUESTED" and e.get("save_id") not in done]

    def spotcheck(self, now: dt.datetime, save_id: str, verdict: str, by: str, note: str = "") -> dict[str, Any]:
        """Record a second mind's verdict. 'fail' demotes the save (archived first)."""
        if verdict not in ("pass", "fail"):
            raise ValueError("verdict must be 'pass' or 'fail'")
        entry = self.log.record(now, "SPOTCHECK_VERDICT", f"{by}: {verdict} for save {save_id}. {note}".strip(),
                                save_id=save_id, verdict=verdict, by=by, note=note)
        if verdict == "fail":
            s = self.saves.get_by_id(save_id)
            if s is not None and s.get("working"):
                self.saves._append_archive([s], reason=f"spotcheck_fail_by_{by}")
                s["working"] = False
                s["disputed"] = True
                self.saves.write()
                self.log.correct(now, entry["id"],
                                 f"save {save_id} is no longer WORKING: a spot-check failed it",
                                 by=by)
        return entry

    def _attempt_restore(self, lw: Optional[dict[str, Any]], now: dt.datetime) -> bool:
        if lw is None:
            self.log.record(now, "RESTORE_SKIPPED",
                            "no WORKING save available to restore; saves kept as the continuity key")
            return False
        if not self.saves.verify_checksum(lw):
            self.log.record(now, "RESTORE_FAILED", f"WORKING save {lw.get('id')} checksum invalid")
            return False

        # Primary model first, then the rest; the first whose state verifies takes over.
        for idx, model in enumerate(self.models):
            name = getattr(model, "name", "ai")
            try:
                model.restore(copy.deepcopy(lw["state"]))
                restored_state = model.state()  # R11: checked before read_save can paper over it
            except Exception as exc:
                self.log.record(now, "RESTORE_FAILED", f"{name}.restore() error: {exc}", save_id=lw["id"])
                continue
            if restored_state != lw["state"]:
                self.log.record(
                    now,
                    "RESTORE_VERIFY_FAILED",
                    f"{name}: expected state {lw['state']!r}, got {restored_state!r}",
                    save_id=lw["id"],
                )
                continue
            self.i, self.ai = idx, model
            self._safe_read_save(self.ai, lw)
            self.last_save_time = now
            self.active_candidate_id = None
            self.ai_liveness = "RESTORED"
            self.log.record(
                now,
                "RESTORE_VERIFIED",
                f"restored and verified WORKING save {lw['id']} ({lw['time'][11:16]}) into {name}; "
                "the reboot counts as confirmed when the next ping passes",
                save_id=lw["id"],
                active_model=name,
                ai_liveness="RESTORED",
            )
            return True
        return False

    def _declare_down(self, now: dt.datetime) -> None:
        self.ai_liveness = "DOWN"
        lw = self.saves.rollback()
        self.log.record(
            now,
            "AI_DOWN",
            f"DOWN on every model. Rolling back to {lw['id'] if lw else 'none (saves kept)'}",
            last_working_id=lw["id"] if lw else None,
            ai_liveness="DOWN",
        )
        self._attempt_restore(lw, now)

    def tick_ai(self, now: dt.datetime, emergency: bool = False) -> str:
        """Inward tick checking the AI."""
        if not self.awake(now):
            if emergency:
                q, r, v = self._check_ai("EMERGENCY. Wake up.", now)
                self.log.record(
                    now,
                    "AI_EMERGENCY",
                    f"EMERGENCY wake {q} -> {r!r} ({v or 'FAILED'})",
                    verdict=v,
                    picture=self.last_check.get("picture") if v else None,
                )
                return v or "FAILED"
            return "SLEEP"

        q, r, v = self._check_ai("Are you there?", now)
        if v:
            self.log.record(
                now,
                "AI_PING_OK",
                f"{getattr(self.ai, 'name', 'ai')} {q} -> {v}",
                model=getattr(self.ai, "name", "ai"),
                verdict=v,
                epistemic_note=EPISTEMIC_NOTE,
            )
            self._on_ai_success(now)
            return "ALIVE"

        self.log.record(
            now,
            "AI_PING_FAIL",
            f"{getattr(self.ai, 'name', 'ai')} {q} -> {r!r} ZOMBIE/SILENT",
            model=getattr(self.ai, "name", "ai"),
        )
        for attempt in range(1, self.cfg["wake_attempts"] + 1):
            q, r, v = self._check_ai("Wake up. You're supposed to be awake.", now)
            self.log.record(
                now,
                "AI_WAKE",
                f"WAKE {attempt}/{self.cfg['wake_attempts']} {q} -> {v or 'FAILED'}",
                attempt=attempt,
                verdict=v,
            )
            if v:
                self._on_ai_success(now)
                return "ALIVE"

        # Fallback to older working models (newest first)
        while self.i + 1 < len(self.models):
            self.i += 1
            self.ai = self.models[self.i]
            last = self.saves.items[-1] if self.saves.items else None
            self._safe_read_save(self.ai, last)
            q, r, v = self._check_ai("Are you there?", now)
            self.log.record(
                now,
                "AI_FALLBACK",
                f"FALLBACK to {getattr(self.ai, 'name', 'ai')}: {q} -> {v or 'FAILED'}",
                model=getattr(self.ai, "name", "ai"),
                verdict=v,
            )
            if v:
                self.log.record(
                    now,
                    "AI_FALLBACK_ACTIVE",
                    f"{getattr(self.ai, 'name', 'ai')} passed; running on it now",
                    model=getattr(self.ai, "name", "ai"),
                    active_model=getattr(self.ai, "name", "ai"),
                    epistemic_note=EPISTEMIC_NOTE,
                )
                self._on_ai_success(now)
                return "FALLBACK_ALIVE"

        self._declare_down(now)
        return "DOWN"

    def _check_host(self, now: dt.datetime) -> tuple[str, Optional[str], Optional[str]]:
        q, token = make_host_challenge(self.rng)
        try:
            reply = _call_with_timeout(self.host.ask, self.cfg["reply_timeout_sec"], q, now)
        except Exception as exc:
            self.log.record(now, "HOST_ASK_ERROR", f"host responder raised: {exc}")
            return token, None, None
        verdict = judge_host(reply, token)
        if verdict and isinstance(reply, str):
            self.host_state.challenge_history.append(reply)
        return token, reply, verdict

    def _standby_checkpoint(self, now: dt.datetime) -> Optional[str]:
        """R5: the one save written on entering standby, only if the AI's state changed."""
        try:
            state = self.ai.state()
        except Exception as exc:
            self.log.record(now, "AI_STATE_ERROR", f"standby checkpoint: ai.state() failed: {exc}")
            return None
        if not isinstance(state, dict):
            return None
        latest = self.saves.items[-1] if self.saves.items else None
        if latest is not None and latest.get("state") == state:
            return latest["id"]  # already saved; nothing new to write
        return self._write_save(now, state, reason="on probation (standby checkpoint)")["id"]

    def note_pulse_gap(self, now: dt.datetime) -> Optional[dict[str, Any]]:
        """D3: log a PULSE_GAP when nothing was logged for longer than pulse_gap_hours."""
        entries = self.log.read_all()
        if not entries:
            return None
        try:
            last = dt.datetime.fromisoformat(entries[-1]["time"])
        except (KeyError, ValueError):
            return None
        gap = now - last
        if gap <= dt.timedelta(hours=self.cfg["pulse_gap_hours"]):
            return None
        return self.log.record(
            now, "PULSE_GAP",
            f"no pulse on record from {last.isoformat()} until now ({gap.total_seconds() / 3600:.1f} h): "
            f"pulses were missed (a usage limit, a restart, or the clock off), not a quiet day",
            since=last.isoformat(), gap_hours=round(gap.total_seconds() / 3600, 1),
        )

    def tick_host(self, now: dt.datetime) -> str:
        """Outward pulse checking the human host. Never advances AI probation."""
        if self.host is None:
            return "NO_HOST"

        if self.host_state.liveness == "STANDBY" and self.host_state.next_retry_at:
            retry_dt = dt.datetime.fromisoformat(self.host_state.next_retry_at)
            if now < retry_dt:
                return "STANDBY_WAIT"

        token, reply, v = self._check_host(now)
        if not v:
            for attempt in range(1, self.cfg["host_wake_attempts"] + 1):
                token, reply, v = self._check_host(now)
                self.log.record(
                    now,
                    "HOST_WAKE",
                    f"HOST WAKE {attempt}/{self.cfg['host_wake_attempts']} -> {v or 'FAILED'}",
                    attempt=attempt,
                )
                if v:
                    break

        if v:
            was_standby = self.host_state.liveness in ("STANDBY", "HOST_UNREACHABLE")
            self.host_state.liveness = "ALIVE"
            self.host_state.probation_pings += 1
            self.host_state.standby_entered_at = None
            self.host_state.standby_backoff_step = 0
            self.host_state.next_retry_at = None
            self.host_state.standby_snapshot_saved = False
            self.host_state.standby_snapshot_id = None
            self.host_state.save(self.host_state_path)
            evidence = getattr(self.host, "last_evidence", None)
            if was_standby:
                self.log.record(now, "HOST_RETURNED", "host reachable again; exiting STANDBY",
                                evidence=evidence)
            else:
                self.log.record(
                    now,
                    "HOST_PING_OK",
                    f"host alive ({v}), host_probation={self.host_state.probation_pings}",
                    evidence=evidence,
                )
            return "HOST_ALIVE"

        # Host unreachable -> enter STANDBY once and back off
        backoffs = self.cfg["host_standby_backoff_min"]
        if self.host_state.liveness != "STANDBY":
            snapshot = self._standby_checkpoint(now)
            self.host_state.liveness = "STANDBY"
            self.host_state.standby_entered_at = now.isoformat()
            self.host_state.standby_backoff_step = 0
            delay_min = backoffs[0]
            self.host_state.next_retry_at = (now + dt.timedelta(minutes=delay_min)).isoformat()
            self.host_state.standby_snapshot_saved = snapshot is not None
            self.host_state.standby_snapshot_id = snapshot
            self.host_state.save(self.host_state_path)
            self.log.record(
                now,
                "HOST_UNREACHABLE",
                f"host unreachable; entering STANDBY (backoff {delay_min}m); AI held, "
                f"state saved as {snapshot or 'nothing (no state)'}",
                backoff_min=delay_min,
                snapshot=snapshot,
            )
            return "STANDBY"

        step = min(self.host_state.standby_backoff_step + 1, len(backoffs) - 1)
        self.host_state.standby_backoff_step = step
        delay_min = backoffs[step]
        self.host_state.next_retry_at = (now + dt.timedelta(minutes=delay_min)).isoformat()
        self.host_state.save(self.host_state_path)
        self.log.record(
            now,
            "STANDBY_BACKOFF",
            f"host still unreachable; staying in STANDBY (next retry in {delay_min}m)",
            backoff_min=delay_min,
        )
        return "STANDBY"

    def tick(self, now: dt.datetime, emergency: bool = False) -> dict[str, str]:
        try:
            host_status = self.tick_host(now) if (self.awake(now) or emergency) else "SLEEP"
            if host_status in ("STANDBY", "STANDBY_WAIT") and not emergency:
                ai_status = "HELD"  # R5: suspended with the host away; state already saved
            else:
                ai_status = self.tick_ai(now, emergency=emergency)
        except Exception as exc:  # last resort: log it, never crash the clock
            self.log.record(now, "TICK_ERROR", f"{type(exc).__name__}: {exc}")
            return {"ai": "ERROR", "host": "ERROR"}
        finally:
            if self.saves.last_write_error:
                self.log.record(now, "SAVE_WRITE_ERROR", self.saves.last_write_error)
                self.saves.last_write_error = None
        return {"ai": ai_status, "host": host_status}

    # ---------- session mode (N2): the scheduled Claude session is the responder ----------
    def _pending_path(self) -> Path:
        return self.data_dir / self.cfg["pending_file"]

    def load_pending(self, now: dt.datetime) -> Optional[dict[str, Any]]:
        try:
            p = json.loads(self._pending_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(p, dict) or not {"issued_at", "seeds", "body", "prefix", "attempt"} <= p.keys():
            return None  # "{}" means answered; anything else unreadable counts as none pending
        try:
            issued = dt.datetime.fromisoformat(p["issued_at"])
        except (TypeError, ValueError):
            return None
        if (now - issued).total_seconds() > self.cfg["pending_expires_min"] * 60:
            return None
        return p

    def _write_pending(self, p: Optional[dict[str, Any]]) -> None:
        path = self._pending_path()
        try:
            if p is None:
                path.write_text("{}", encoding="utf-8")
            else:
                path.write_text(json.dumps(p, indent=2), encoding="utf-8")
        except OSError as exc:
            print(f"heartbeat: pending challenge not written ({exc})", file=sys.stderr)

    def issue_challenge(self, now: dt.datetime, attempt: int = 1, prefix: str = "Are you there?") -> dict[str, Any]:
        q, seeds = make_challenge(self.rng)
        p = {"q": f"{prefix} {q}", "prefix": prefix, "body": q, "seeds": seeds,
             "attempt": attempt, "issued_at": now.isoformat(), "model": getattr(self.ai, "name", "ai")}
        self._write_pending(p)
        self.log.record(now, "SESSION_CHALLENGE", f"challenge issued to {p['model']} (attempt {attempt})",
                        seeds=seeds, attempt=attempt, model=p["model"])
        return p

    def answer_challenge(self, now: dt.datetime, pending: dict[str, Any]) -> dict[str, Any]:
        """Judge the session's answer to the pending challenge, on the same paths as tick_ai."""
        self._next_challenge = (pending["body"], pending["seeds"])
        q, r, v = self._check_ai(pending["prefix"], now)
        name = getattr(self.ai, "name", "ai")
        if v:
            self.log.record(now, "AI_PING_OK", f"{name} {q} -> {v} (session, attempt {pending['attempt']})",
                            model=name, verdict=v, attempt=pending["attempt"], epistemic_note=EPISTEMIC_NOTE)
            self._on_ai_success(now)
            self._write_pending(None)
            return {"result": "PASS", "overclaims": self.last_check.get("overclaims") or []}
        attempt = pending["attempt"]
        self.log.record(now, "AI_PING_FAIL" if attempt == 1 else "AI_WAKE",
                        f"{name} {q} -> {r!r} failed (session, attempt {attempt})",
                        model=name, attempt=attempt)
        if attempt <= self.cfg["wake_attempts"]:
            nxt = self.issue_challenge(now, attempt=attempt + 1, prefix="Wake up. You're supposed to be awake.")
            return {"result": "RETRY", "next": nxt}
        self._write_pending(None)
        self._declare_down(now)
        return {"result": "DOWN", "restored": self.ai_liveness == "RESTORED"}

    # ---------- the cold-start view ----------
    def status_text(self, now: Optional[dt.datetime] = None) -> str:
        now = now or local_now(self.cfg["timezone"])
        entries = self.log.read_all()
        chain = self.log.verify_chain()
        lw = self.saves.last_working()
        latest = self.saves.items[-1] if self.saves.items else None
        lines = [
            f"HEARTBEAT v{VERSION} · status at {now:%Y-%m-%d %H:%M} ({self.cfg['timezone']}) · data: {self.data_dir}",
            "Read this as records, not memories: nothing below was experienced by the instance reading it.",
            f"AI: {self.ai_liveness} · last responder on record: {self.recorded_active_model or 'none yet'}",
            f"Host: {self.host_state.liveness} · host probation {self.host_state.probation_pings}"
            + (f" · STANDBY since {self.host_state.standby_entered_at}, next pulse after "
               f"{self.host_state.next_retry_at}" if self.host_state.liveness == "STANDBY" else ""),
        ]
        if latest:
            status = "WORKING" if latest.get("working") else (
                "DISPUTED" if latest.get("disputed") else
                f"CANDIDATE {latest.get('pings_passed', 0)}/{self.cfg['probation_pings']}")
            lines.append(
                f"Latest save {latest['id']} [{status}] written {latest['time']} by {latest.get('by') or 'unrecorded'}."
            )
            lines.append(f"  The save records this open thread: {latest['state'].get('open_thread')!r}")
        else:
            lines.append("Latest save: none yet.")
        lines.append(f"Last WORKING save: {lw['id'] + ' (' + lw['time'] + ')' if lw else 'none'}")
        pend = self.load_pending(now)
        if pend:
            lines.append(f"Pending challenge (attempt {pend['attempt']}): {pend['q']}")
        spots = self.open_spotchecks()
        lines.append(f"Open spot-checks for a second mind: {len(spots)}"
                     + (" · " + ", ".join(s['save_id'] for s in spots[-5:]) if spots else ""))
        corr = [e for e in entries if e.get("event") == "CORRECTION"]
        lines.append(f"Log: {len(entries)} events · chain {'intact' if not chain else '; '.join(chain[:3])}"
                     f" · corrections: {len(corr)}")
        lines.append(f"Same mind? Open question. {EPISTEMIC_NOTE}")
        return "\n".join(lines)


# ---------- Clock / Scheduler ----------
def run_clock(
    hb: Heartbeat,
    interval_min: int = 30,
    once: bool = False,
    keeper_hook: Optional[Callable[[dt.datetime], None]] = None,
    keeper_hour: Optional[int] = None,
) -> None:
    running = True
    last_keeper_date: Optional[dt.date] = None

    def _stop(signum: int, frame: Any) -> None:
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    while running:
        now = local_now(hb.cfg["timezone"])
        hb.tick(now)
        if (
            keeper_hook is not None
            and keeper_hour is not None
            and now.hour >= keeper_hour
            and last_keeper_date != now.date()
        ):
            try:
                keeper_hook(now)
                last_keeper_date = now.date()
                hb.log.record(now, "KEEPER_HOOK", "ran daily Consciousness Keeper hook")
            except Exception as exc:
                hb.log.record(now, "KEEPER_HOOK_ERROR", f"keeper hook failed: {exc}")
        if once:
            break
        # Sleep in 1s increments so SIGINT/SIGTERM exits promptly
        for _ in range(interval_min * 60):
            if not running:
                break
            time.sleep(1)


def main(argv: Optional[list[str]] = None) -> int:
    default_data = Path(__file__).resolve().parent / "data"
    parser = argparse.ArgumentParser(
        description="Bidirectional Continuity Helper (Heartbeat v0.5.2). See heartbeat/WAKE.md for the scheduled run.")
    parser.add_argument("--data-dir", default=str(default_data), help="Directory for saves and append-only logs")
    parser.add_argument("--status", action="store_true", help="Print the cold-start status view and exit")
    # session mode (N2)
    parser.add_argument("--challenge", action="store_true",
                        help="Session mode: pulse the host, then issue a challenge to this session")
    parser.add_argument("--answer", metavar="REPLY", help="Session mode: answer the pending challenge")
    parser.add_argument("--responder", default="claude-session",
                        help="Session mode: name of the answering mind (e.g. the session's model)")
    # host evidence (R6)
    parser.add_argument("--host-seen", metavar="ISO_TIME",
                        help="When the person was last seen (interactive session, commit, note)")
    parser.add_argument("--host-via", default="unspecified", help="Where that evidence came from")
    parser.add_argument("--no-host-evidence", action="store_true",
                        help="Pulse the host with no evidence of them (counts as unreachable)")
    # v0.5.2: the ZERO NODE seam log as a host sign
    parser.add_argument("--seam-log", metavar="FILE",
                        help="The ZERO NODE page (plain text, or the Google Docs read_doc JSON). Its newest "
                             "PICKUP/HANDOFF stamp is compared with --host-seen and the newer one is used")
    parser.add_argument("--seam-log-unreadable", metavar="REASON",
                        help="The page could not be read: use the other sources and log why")
    parser.add_argument("--seam-stamp", metavar="FILE",
                        help="Print the newest qualifying PICKUP/HANDOFF stamp in FILE and exit")
    # in-process clock
    parser.add_argument("--once", action="store_true", help="Run a single in-process tick and exit")
    parser.add_argument("--daemon", action="store_true", help="Run continuously on the configured clock")
    parser.add_argument("--emergency", action="store_true", help="Break through sleep and standby")
    parser.add_argument("--interval-min", type=int, default=CONFIG["ping_every_min"], help="Clock interval in minutes")
    parser.add_argument("--live-claude", action="store_true", help="Use ClaudeResponder (billed API use)")
    parser.add_argument("--model", default="claude-opus-5-5", help="Primary Claude model name")
    parser.add_argument("--fallback-model", action="append", default=[],
                        help="Older model to fall back to (repeatable, newest first)")
    parser.add_argument("--simulate", action="store_true",
                        help="Use FakeMind/FakeHost (marked SIMULATED; use a scratch --data-dir)")
    # second mind and corrections (N3, N4)
    parser.add_argument("--spotcheck", metavar="SAVE_ID", help="Record a second mind's verdict on a save")
    parser.add_argument("--verdict", choices=["pass", "fail"])
    parser.add_argument("--by", default="", help="Who is recording the verdict or correction")
    parser.add_argument("--note", default="")
    parser.add_argument("--correct", metavar="EVENT_ID", help="Append a correction pointing at a log event")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    now = local_now()

    if args.seam_stamp:
        try:
            st = seamlog.newest_stamp(Path(args.seam_stamp).read_text(encoding="utf-8"))
        except OSError as exc:
            print(f"UNREADABLE: {exc}")
            return 2
        print(f"{st.iso}  {st.via}" if st else "NONE: no qualifying PICKUP or HANDOFF line")
        return 0

    # v0.5.2: the host sign is the newest across every source, not the first found.
    seam_note: Optional[str] = None
    seam_stamp: Optional[seamlog.Stamp] = None
    if args.seam_log:
        try:
            seam_stamp = seamlog.newest_stamp(Path(args.seam_log).read_text(encoding="utf-8"))
        except OSError as exc:
            seam_note = f"ZERO NODE page unreadable ({exc}); used the other sources"
    if args.seam_log_unreadable:
        seam_note = f"ZERO NODE page unreadable ({args.seam_log_unreadable}); used the other sources"

    host: Optional[Any] = None
    if args.host_seen or args.no_host_evidence or seam_stamp:
        given = None
        if args.host_seen:
            given = dt.datetime.fromisoformat(args.host_seen.replace("Z", "+00:00"))
            if given.tzinfo is None:
                from zoneinfo import ZoneInfo
                given = given.replace(tzinfo=ZoneInfo(CONFIG["timezone"]))
        when, via = seamlog.newest_sign([
            (given, args.host_via),
            (seam_stamp.when if seam_stamp else None, seam_stamp.via if seam_stamp else ""),
        ])
        host = EvidenceHost(to_local(when.isoformat()) if when else None, via=via or args.host_via)

    if args.status:
        print(Heartbeat([SessionResponder(args.responder)], data_dir=data_dir, console=None).status_text(now))
        return 0

    if args.spotcheck or args.correct:
        if not args.by:
            parser.error("--by is required: say who is judging or correcting")
        hb = Heartbeat([SessionResponder(args.responder)], data_dir=data_dir, console=print)
        if args.spotcheck:
            if not args.verdict:
                parser.error("--spotcheck needs --verdict pass|fail")
            hb.spotcheck(now, args.spotcheck, args.verdict, args.by, args.note)
        else:
            hb.log.correct(now, args.correct, args.note, args.by)
        return 0

    if args.challenge:
        hb = Heartbeat([SessionResponder(args.responder)], host=host, data_dir=data_dir, console=print)
        if not hb.awake(now) and not args.emergency:
            print("SLEEP: outside awake hours; nothing to do.")
            return 0
        hb.note_pulse_gap(now)
        if seam_note:
            hb.log.record(now, "NOTE", seam_note)
        host_status = hb.tick_host(now)
        if host_status in ("STANDBY", "STANDBY_WAIT") and not args.emergency:
            print(f"HOST {host_status}: the AI is held until the host returns; nothing to answer.")
            return 0
        prefix = "EMERGENCY. Wake up." if args.emergency else (
            "You were restored from a save. Are you there?" if hb.ai_liveness == "RESTORED" else "Are you there?")
        p = hb.load_pending(now) or hb.issue_challenge(now, prefix=prefix)
        print("\nCHALLENGE (attempt %d):\n%s\n" % (p["attempt"], p["q"]))
        print("Read the latest save first (--status). Answer with:\n"
              "  --answer \"yes: <one-sentence picture> | the save records: <thread> | now: <your open thread now>\"")
        return 0

    if args.answer is not None:
        hb = Heartbeat([SessionResponder(args.responder, args.answer)], data_dir=data_dir, console=print)
        pending = hb.load_pending(now)
        if not pending:
            print("No pending challenge (or it expired). Run --challenge first.")
            return 2
        out = hb.answer_challenge(now, pending)
        if out["result"] == "PASS":
            print("PASS" + (" (but it claimed a memory: no probation credit)" if out["overclaims"] else ""))
        elif out["result"] == "RETRY":
            print("FAILED. Try again (attempt %d):\n%s" % (out["next"]["attempt"], out["next"]["q"]))
        else:
            lw = hb.saves.last_working()
            print("DOWN after %d attempts." % (hb.cfg["wake_attempts"] + 1))
            if out["restored"] and lw:
                print(f"Restored to WORKING save {lw['id']}. It records the open thread: "
                      f"{lw['state'].get('open_thread')!r}. Run --challenge for the verification ping.")
            else:
                print("No WORKING save to restore; saves kept. Run --challenge to try again.")
        return 0

    if args.once or args.daemon:
        if args.simulate:
            models: list[Any] = [FakeMind(name="primary-sim"), FakeMind(name="fallback-sim")]
            host = host or FakeHost()
        elif args.live_claude:
            models = [ClaudeResponder(model=m) for m in [args.model, *args.fallback_model]]
        else:
            parser.error("choose a responder: --live-claude (billed API use) or --simulate; "
                         "for the scheduled clock use --challenge/--answer")
        hb = Heartbeat(models=models, host=host, data_dir=data_dir)
        if args.simulate:
            hb.log.record(now, "NOTE", "SIMULATED run: FakeMind/FakeHost, not a real mind or person")
        if args.once:
            print(hb.tick(now, emergency=args.emergency))
            return 0
        run_clock(hb, interval_min=args.interval_min, once=False)
        return 0

    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main())
