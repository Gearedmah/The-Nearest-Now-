"""
heartbeat_v05_1.py - Bidirectional Continuity Helper & Clock (v0.5.1)
Idea: Sean Battle. Original v0.1-v0.4: Thessik (Claude). v0.5.1 build: Continuity Helper.
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
"""

from __future__ import annotations

import argparse
import copy
import datetime as dt
import hashlib
import json
import random
import signal
import sys
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

CONFIG: dict[str, Any] = {
    "ping_every_min": 30,
    "awake_from": 7,
    "awake_until": 23,
    "probation_pings": 4,
    "wake_attempts": 3,
    "save_every_min": 120,
    "host_ping_every_min": 30,
    "host_wake_attempts": 3,
    "host_standby_backoff_min": [30, 60, 120, 240],
    "max_word_overlap": 0.5,
    "history_window": 5,
    "data_dir": "heartbeat_data",
    "save_file": "saves.json",
    "archive_file": "saves_archive.json",
    "log_file": "heartbeat_log.jsonl",
    "host_state_file": "host_state.json",
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
        "say the open thread at your last save."
    )
    return q, words


def _words(s: str) -> list[str]:
    return [w.strip(".,!?;:'\"").lower() for w in s.split() if w.strip(".,!?;:'\"")]


def judge(
    reply: Optional[str],
    seeds: list[str],
    history: list[str],
    thread: Optional[str] = None,
    max_overlap: float = CONFIG["max_word_overlap"],
    history_window: int = CONFIG["history_window"],
) -> Optional[str]:
    """Judge AI reply: 'yes: <picture> | <thread>'. Returns 'yes'/'busy'/None."""
    if not isinstance(reply, str) or ":" not in reply or "|" not in reply:
        return None
    reply_body, said_thread = reply.rsplit("|", 1)
    if thread:
        if thread.strip().lower() not in said_thread.strip().lower():
            return None
    head, pic = reply_body.split(":", 1)
    head = head.strip().lower()
    if head not in ("yes", "busy"):
        return None
    w = _words(pic)
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
    """Live Anthropic API responder. Requires `pip install anthropic` and ANTHROPIC_API_KEY."""

    def __init__(self, model: str = "claude-sonnet-4-20250514", timeout_sec: float = 30.0):
        import anthropic

        self.client = anthropic.Anthropic(timeout=timeout_sec)
        self.model = model
        self.name = model
        self.save_text = ""
        self._thread = "start the day"

    def read_save(self, save: Optional[dict[str, Any]]) -> None:
        self.save_text = json.dumps(save) if save else ""
        if save and "state" in save:
            self._thread = save["state"].get("open_thread", self._thread)

    def ask(self, msg: str, now: dt.datetime) -> str:
        r = self.client.messages.create(
            model=self.model,
            max_tokens=120,
            messages=[
                {
                    "role": "user",
                    "content": (
                        f"Your last save: {self.save_text}\n"
                        f"{msg} Start with 'yes:' or 'busy:'."
                    ),
                }
            ],
        )
        return r.content[0].text.strip()

    def state(self) -> dict[str, Any]:
        return {"open_thread": self._thread}

    def restore(self, state: dict[str, Any]) -> None:
        self._thread = state.get("open_thread", "start the day")
        self.save_text = json.dumps({"state": state})


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


# ---------- append-only Recursor log ----------
class AppendOnlyLog:
    """Append-only JSONL event log. Corrections go in as new lines; never erases."""

    def __init__(self, path: Path, console: Optional[Callable[[str], None]] = print):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.console = console

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
        line = json.dumps(entry, ensure_ascii=False)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
            f.flush()
        if self.console:
            self.console(f"{now:%H:%M}  [{event_type}] {message}")
        return entry

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        entries: list[dict[str, Any]] = []
        for raw in self.path.read_text(encoding="utf-8").splitlines():
            if raw.strip():
                try:
                    entries.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return entries


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
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_path = Path(archive_path) if archive_path else self.path.with_name("saves_archive.json")
        self.probation_pings = probation_pings
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
        except OSError:
            pass

    def _append_archive(self, records: list[dict[str, Any]], reason: str) -> None:
        if not records:
            return
        existing: list[dict[str, Any]] = []
        if self.archive_path.exists():
            try:
                loaded = json.loads(self.archive_path.read_text(encoding="utf-8"))
                if isinstance(loaded, list):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = []
        archived_at = dt.datetime.now(dt.timezone.utc).isoformat()
        for rec in records:
            entry = copy.deepcopy(rec)
            entry["archived_at"] = archived_at
            entry["archive_reason"] = reason
            existing.append(entry)
        self.archive_path.parent.mkdir(parents=True, exist_ok=True)
        self.archive_path.write_text(json.dumps(existing, indent=2), encoding="utf-8")

    def write(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.items, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    @staticmethod
    def verify_checksum(save: dict[str, Any]) -> bool:
        sid = save.get("id")
        t = save.get("time")
        st = save.get("state")
        cs = save.get("checksum")
        if not sid or not t or not isinstance(st, dict) or not cs:
            return False
        return compute_checksum(sid, t, st) == cs

    def add(self, now: dt.datetime, state: dict[str, Any]) -> dict[str, Any]:
        prev = self.last_working()
        save_id = f"save_{uuid.uuid4().hex[:12]}"
        time_iso = now.isoformat()
        rec = {
            "id": save_id,
            "time": time_iso,
            "pings_passed": 0,
            "working": False,
            "prev_working": prev["id"] if prev else None,
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
    ) -> Optional[dict[str, Any]]:
        """Advance probation ONLY for the specific candidate save under test."""
        target: Optional[dict[str, Any]] = None
        if save_id is not None:
            target = self.get_by_id(save_id)
        else:
            unapproved = [s for s in self.items if not s.get("working")]
            target = unapproved[-1] if unapproved else None

        if not target or target.get("working"):
            return None
        if not self.verify_checksum(target):
            log.record(now, "SAVE_CHECKSUM_MISMATCH", f"save {target.get('id')} failed checksum")
            return None

        target["pings_passed"] = int(target.get("pings_passed", 0)) + 1
        if target["pings_passed"] >= self.probation_pings:
            target["working"] = True
            log.record(
                now,
                "SAVE_WORKING",
                f"save {target['id']} ({target['time'][11:16]}) stamped WORKING",
                save_id=target["id"],
            )
        self.write()
        return target

    def last_working(self) -> Optional[dict[str, Any]]:
        w = [s for s in self.items if s.get("working") and self.verify_checksum(s)]
        return w[-1] if w else None

    def rollback(self) -> Optional[dict[str, Any]]:
        """Archive unapproved/newer saves and roll back to last WORKING save."""
        lw = self.last_working()
        if lw is None:
            if self.items:
                self._append_archive(self.items, reason="rollback_no_working_save")
                self.items = []
                self.write()
            return None
        idx = self.items.index(lw)
        discarded = self.items[idx + 1 :]
        if discarded:
            self._append_archive(discarded, reason="rollback_to_last_working")
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "liveness": self.liveness,
            "probation_pings": self.probation_pings,
            "challenge_history": self.challenge_history[-20:],
            "standby_entered_at": self.standby_entered_at,
            "standby_backoff_step": self.standby_backoff_step,
            "next_retry_at": self.next_retry_at,
            "standby_snapshot_saved": self.standby_snapshot_saved,
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

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


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
        self.data_dir.mkdir(parents=True, exist_ok=True)

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
        if self.saves.items:
            latest = self.saves.items[-1]
            if not latest.get("working"):
                self.active_candidate_id = latest.get("id")
            self._safe_read_save(self.ai, latest)

        self.rng = random.Random(seed)
        self.second_mind_judge = second_mind_judge

    def _safe_read_save(self, model: Any, save: Optional[dict[str, Any]]) -> None:
        try:
            model.read_save(save)
        except Exception as exc:
            self.log.record(
                dt.datetime.now(dt.timezone.utc),
                "READ_SAVE_ERROR",
                f"{getattr(model, 'name', 'model')} failed read_save: {exc}",
            )

    def awake(self, now: dt.datetime) -> bool:
        return self.cfg["awake_from"] <= now.hour < self.cfg["awake_until"]

    def _check_ai(self, prefix: str, now: dt.datetime) -> tuple[str, Optional[str], Optional[str]]:
        q, seeds = make_challenge(self.rng)
        try:
            reply = self.ai.ask(f"{prefix} {q}", now)
        except Exception as exc:
            self.log.record(now, "AI_ASK_ERROR", f"{getattr(self.ai, 'name', 'ai')} raised: {exc}")
            return "/".join(seeds), None, None

        last = self.saves.items[-1] if self.saves.items else None
        thread = last["state"].get("open_thread") if last and isinstance(last.get("state"), dict) else None
        verdict = judge(
            reply,
            seeds,
            self.ai_history,
            thread=thread,
            max_overlap=self.cfg["max_word_overlap"],
            history_window=self.cfg["history_window"],
        )
        if verdict and self.second_mind_judge is not None:
            try:
                if not self.second_mind_judge(reply, seeds, thread):
                    verdict = None
            except Exception as exc:
                self.log.record(now, "SECOND_MIND_ERROR", f"spot-check error: {exc}")

        if verdict and isinstance(reply, str):
            pic = reply.split(":", 1)[1].rsplit("|", 1)[0]
            self.ai_history.append(pic)
        return "/".join(seeds), reply, verdict

    def _on_ai_success(self, now: dt.datetime) -> None:
        """Shared success path for initial ping, wake retry, and model fallback (retry parity)."""
        self.ai_liveness = "ALIVE"
        self.saves.pass_ping(self.log, now, save_id=self.active_candidate_id)

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
            new_save = self.saves.add(now, ai_state)
            self.last_save_time = now
            self.active_candidate_id = new_save["id"]
            self._safe_read_save(self.ai, new_save)
            self.log.record(
                now,
                "AI_SAVE",
                f"save {new_save['id']} on probation",
                save_id=new_save["id"],
            )

    def _attempt_restore(self, lw: Optional[dict[str, Any]], now: dt.datetime) -> bool:
        if lw is None:
            self.log.record(now, "RESTORE_SKIPPED", "no WORKING save available to restore")
            return False
        if not self.saves.verify_checksum(lw):
            self.log.record(now, "RESTORE_FAILED", f"WORKING save {lw.get('id')} checksum invalid")
            return False

        # Reset to primary model on reboot if available, or current model
        self.i = 0
        self.ai = self.models[0]
        try:
            self.ai.restore(copy.deepcopy(lw["state"]))
            self._safe_read_save(self.ai, lw)
            restored_state = self.ai.state()
        except Exception as exc:
            self.log.record(now, "RESTORE_FAILED", f"ai.restore() error: {exc}", save_id=lw["id"])
            return False

        expected_thread = lw["state"].get("open_thread")
        actual_thread = restored_state.get("open_thread") if isinstance(restored_state, dict) else None
        if expected_thread is not None and actual_thread != expected_thread:
            self.log.record(
                now,
                "RESTORE_VERIFY_FAILED",
                f"expected open_thread={expected_thread!r}, got {actual_thread!r}",
                save_id=lw["id"],
            )
            return False

        self.last_save_time = now
        self.active_candidate_id = None
        self.log.record(
            now,
            "RESTORE_VERIFIED",
            f"restored and verified WORKING save {lw['id']} ({lw['time'][11:16]})",
            save_id=lw["id"],
        )
        return True

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
                    epistemic_note=EPISTEMIC_NOTE,
                )
                self._on_ai_success(now)
                return "FALLBACK_ALIVE"

        self.ai_liveness = "DOWN"
        lw = self.saves.rollback()
        self.log.record(
            now,
            "AI_DOWN",
            f"DOWN on every model. Rolling back to {lw['id'] if lw else 'none'}",
            last_working_id=lw["id"] if lw else None,
        )
        self._attempt_restore(lw, now)
        return "DOWN"

    def _check_host(self, now: dt.datetime) -> tuple[str, Optional[str], Optional[str]]:
        q, token = make_host_challenge(self.rng)
        try:
            reply = self.host.ask(q, now)
        except Exception as exc:
            self.log.record(now, "HOST_ASK_ERROR", f"host responder raised: {exc}")
            return token, None, None
        verdict = judge_host(reply, token)
        if verdict and isinstance(reply, str):
            self.host_state.challenge_history.append(reply)
        return token, reply, verdict

    def tick_host(self, now: dt.datetime) -> str:
        """Outward pulse checking the human host. Never modifies AI saves/probation."""
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
            self.host_state.save(self.host_state_path)
            if was_standby:
                self.log.record(now, "HOST_RETURNED", "host reachable again; exiting STANDBY")
            else:
                self.log.record(
                    now,
                    "HOST_PING_OK",
                    f"host alive ({v}), host_probation={self.host_state.probation_pings}",
                )
            return "HOST_ALIVE"

        # Host unreachable -> enter STANDBY once and back off
        backoffs = self.cfg["host_standby_backoff_min"]
        if self.host_state.liveness != "STANDBY":
            self.host_state.liveness = "STANDBY"
            self.host_state.standby_entered_at = now.isoformat()
            self.host_state.standby_backoff_step = 0
            delay_min = backoffs[0]
            self.host_state.next_retry_at = (now + dt.timedelta(minutes=delay_min)).isoformat()
            self.host_state.standby_snapshot_saved = True
            self.host_state.save(self.host_state_path)
            self.log.record(
                now,
                "HOST_UNREACHABLE",
                f"host unreachable; entering STANDBY (backoff {delay_min}m)",
                backoff_min=delay_min,
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
        ai_status = self.tick_ai(now, emergency=emergency)
        host_status = self.tick_host(now) if (self.awake(now) or emergency) else "SLEEP"
        return {"ai": ai_status, "host": host_status}


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
        now = dt.datetime.now()
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
    parser = argparse.ArgumentParser(description="Bidirectional Continuity Helper (Heartbeat v0.5.1)")
    parser.add_argument("--data-dir", default="heartbeat_data", help="Directory for saves and append-only logs")
    parser.add_argument("--once", action="store_true", help="Run a single heartbeat tick and exit (for cron/timers)")
    parser.add_argument("--daemon", action="store_true", help="Run continuously on the configured clock")
    parser.add_argument("--status", action="store_true", help="Print current save/host/log status and exit")
    parser.add_argument("--interval-min", type=int, default=CONFIG["ping_every_min"], help="Clock interval in minutes")
    parser.add_argument("--live-claude", action="store_true", help="Use ClaudeResponder via ANTHROPIC_API_KEY")
    parser.add_argument("--model", default="claude-sonnet-4-20250514", help="Primary Claude model name")
    args = parser.parse_args(argv)

    data_dir = Path(args.data_dir)
    if args.status:
        saves = Saves(data_dir / CONFIG["save_file"], archive_path=data_dir / CONFIG["archive_file"])
        host_state = HostState.load(data_dir / CONFIG["host_state_file"])
        lw = saves.last_working()
        print(f"Data dir: {data_dir.resolve()}")
        print(f"Saves: {len(saves.items)} total | Last WORKING: {lw['id'] if lw else 'none'}")
        print(f"Host liveness: {host_state.liveness} | Host probation: {host_state.probation_pings}")
        return 0

    models: list[Any]
    if args.live_claude:
        models = [ClaudeResponder(model=args.model)]
    else:
        models = [FakeMind(name="primary-sim"), FakeMind(name="fallback-sim")]

    hb = Heartbeat(models=models, host=FakeHost(), data_dir=data_dir)
    if args.once or not args.daemon:
        hb.tick(dt.datetime.now())
        return 0

    run_clock(hb, interval_min=args.interval_min, once=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())