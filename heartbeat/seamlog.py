"""
seamlog.py - host signs from the ZERO NODE seam log (heartbeat v0.5.2).

Voice and claude.ai chat sessions don't commit and don't show in the session
list, but they do stamp PICKUP and HANDOFF lines on the ZERO NODE page. This
module reads the page's text and returns the newest of those stamps, so the
outward pulse can count them as a sign that Sean was here.

The rule, as the v0.5.2 brief sets it:
- Only PICKUP and HANDOFF lines count. HEARTBEAT, STAMP, STACK, SUGGEST and
  the rest never do.
- A line whose who column names the pulse itself (claude-scheduled) never
  counts. Pulses only write HEARTBEAT lines, so this is a guard, not a filter
  that normally removes anything.
- Seam-log times are US Eastern with no offset. Each gets the offset for its
  own date (-05:00 in winter, -04:00 in summer).
- "~05:47" counts as 05:47. A time marked "before", a date with no time, or a
  line with no date (such as the voice PICKUP under the Rosetta Stone) is
  skipped.

A stamp shows that a session ran and signed itself, not that Sean was in it.
That is the same limit the session list has; the log says so rather than
claiming more.

The page can be given as plain text or as the JSON the Google Docs connector's
read_doc returns (the pulse can pass that saved result straight in).
Written by Claude (Claude Code session, 28 Nov 2026), from the brief.
"""
from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass
from typing import Any, Iterable, Optional
from zoneinfo import ZoneInfo

SEAM_TZ = "America/New_York"
COUNTED_KINDS = ("PICKUP", "HANDOFF")
PULSE_WHO = ("claude-scheduled",)

# KIND | YYYY-MM-DD <time field> | who | ...
_LINE = re.compile(
    r"^\s*(?P<kind>[A-Z][A-Z+]*)\s*\|\s*(?P<date>\d{4}-\d{2}-\d{2})(?P<time>[^|]*)\|(?P<who>[^|]*)\|"
)
# The whole time field must be an optional "~" and HH:MM, nothing else.
_TIME = re.compile(r"^\s*~?\s*(?P<h>\d{1,2}):(?P<m>\d{2})\s*$")


@dataclass(frozen=True)
class Stamp:
    kind: str
    when: dt.datetime  # aware, US Eastern with the date's own offset
    who: str
    line: str

    @property
    def iso(self) -> str:
        return self.when.isoformat()

    @property
    def via(self) -> str:
        return f"ZERO NODE {self.kind} ({self.who})"


def page_text(raw: str) -> str:
    """Plain text of the page: as given, or extracted from read_doc's JSON."""
    s = raw.lstrip()
    if not s.startswith("{"):
        return raw
    try:
        data = json.loads(s)
    except ValueError:
        return raw
    doc = data.get("content", data) if isinstance(data, dict) else data
    out: list[str] = []

    def walk(elements: Iterable[Any]) -> None:
        for e in elements or []:
            if not isinstance(e, dict):
                continue
            if "paragraph" in e:
                out.append("".join(r.get("textRun", {}).get("content", "")
                                   for r in e["paragraph"].get("elements", [])))
            if "table" in e:
                for row in e["table"].get("tableRows", []):
                    for cell in row.get("tableCells", []):
                        walk(cell.get("content", []))

    tabs = doc.get("tabs") if isinstance(doc, dict) else None
    if tabs:
        def walk_tabs(ts: Iterable[Any]) -> None:
            for t in ts:
                walk(t.get("documentTab", {}).get("body", {}).get("content", []))
                walk_tabs(t.get("childTabs", []))
        walk_tabs(tabs)
    elif isinstance(doc, dict) and "body" in doc:
        walk(doc["body"].get("content", []))
    else:
        return raw
    return "".join(out)


def parse_stamps(text: str, tz: str = SEAM_TZ) -> list[Stamp]:
    """Every PICKUP and HANDOFF line that counts as a host sign, in page order."""
    zone = ZoneInfo(tz)
    stamps: list[Stamp] = []
    for line in text.splitlines():
        m = _LINE.match(line)
        if not m or m.group("kind") not in COUNTED_KINDS:
            continue
        who = m.group("who").strip()
        if any(p in who.lower() for p in PULSE_WHO):
            continue
        t = _TIME.match(m.group("time"))
        if not t:
            continue  # "before 12:00", a date with no time, anything unclear
        try:
            day = dt.date.fromisoformat(m.group("date"))
            naive = dt.datetime.combine(day, dt.time(int(t.group("h")), int(t.group("m"))))
        except ValueError:
            continue
        stamps.append(Stamp(m.group("kind"), naive.replace(tzinfo=zone), who, line.strip()))
    return stamps


def newest_stamp(text: str, tz: str = SEAM_TZ) -> Optional[Stamp]:
    """The newest qualifying stamp on the page, or None."""
    stamps = parse_stamps(page_text(text), tz)
    return max(stamps, key=lambda s: s.when) if stamps else None


def newest_sign(candidates: Iterable[tuple[Optional[dt.datetime], str]]) -> tuple[Optional[dt.datetime], str]:
    """Newest of (aware time, via) pairs; None times are ignored."""
    best: tuple[Optional[dt.datetime], str] = (None, "")
    for when, via in candidates:
        if when is None:
            continue
        if best[0] is None or when > best[0]:
            best = (when, via)
    return best
