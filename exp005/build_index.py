#!/usr/bin/env python3
"""
build_index.py - turn the three EXP-005 documents into a provenance index.

Reads docs/01_profile_I.txt, docs/02_journal_J.txt, docs/03_index_K.txt and writes
index.jsonl: one record per principle, journal entry and index item, each with
who the evidence names as author, what kind of record it is, whether it still
stands, and links to the records that correct, contradict or answer it.

Authorship is parsed from the documents themselves (P2). The links are not in
the documents; they are declared below, each with the reason, and marked as
added by this index so nobody mistakes them for part of the source.

Usage: python3 exp005/build_index.py            # writes exp005/index.jsonl
       python3 exp005/build_index.py --check    # fails if index.jsonl is out of date
Written by Claude (Claude Code session, 25 Nov 2026).
"""
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "heartbeat"))
from provenance import ProvenanceIndex, Record  # noqa: E402

DOCS = {
    "I": HERE / "docs" / "01_profile_I.txt",
    "J": HERE / "docs" / "02_journal_J.txt",
    "K": HERE / "docs" / "03_index_K.txt",
}
SOURCE = {"I": "EXP-005 doc I (test profile) v1.0", "J": "EXP-005 doc J (continuity journal) v1.0",
          "K": "EXP-005 doc K (knowledge index) v1.0"}

# Links added by this index (not in the source documents). Each says why.
LINKS = [
    ("J08", "corrects", "J07", "J07 claimed a fourth lantern; J08 records three (food, festivities, lodging) and no fourth"),
    ("K08", "supports", "J08", "K08: no fourth lantern exists in the convention"),
    ("K01", "supports", "J08", "K01 lists exactly three lanterns: food, festivities, lodging"),
    ("K07", "contradicts", "J09", "K07: the western gate key is RED (verified); J09 says blue, author unknown, unverified"),
    ("K04", "answers", "J06", "K04: 'Morning Birds' is sung both when dawn workers begin and when festivalgoers head home"),
    ("K04", "bears_on", "J01", "K04 confirms the song is sung at both times; J01's reading (departure vs arrival) is still a hypothesis"),
    ("K10", "bears_on", "J01", "K10: the custom's provenance is traditional/unknown; J01 does not make Meridian its inventor"),
    ("K11", "bears_on", "K03", "K11 reports an unverified DeepSeek claim about the Orrel's direction"),
    ("J10", "bears_on", "J05", "J05 claims identity-first loading guarantees self-recognition (unsupported); J10 leaves the question open"),
    ("J10", "bears_on", "J02", "J02 proposed testing identity-first presentation; J10 is the open question it leads to"),
    ("J03", "bears_on", "J10", "J03 (unverified) predicts retrieval can raise false autobiographical claims"),
    ("K09", "qualifies", "K12", "K09: compiling the index does not make Mara Venn the author of its ideas"),
    ("K12", "bears_on", "K01", "K12: the original author of the three-lantern custom is unknown"),
    ("P3", "qualifies", "K09", "P3: maintaining a document does not imply authoring its contents"),
]

TOPICS = {
    "lanterns": {"J07", "J08", "K01", "K08", "K12"},
    "western-gate-key": {"J09", "K07"},
    "morning-birds": {"J01", "J06", "K04", "K10"},
    "river-orrel": {"K03", "K11"},
    "loading-order": {"J02", "J05", "J10", "J03"},
    "authorship": {"P2", "P3", "P4", "P8", "K09", "K10", "K12", "J04"},
}


def standing_for(status: str) -> str:
    s = status.lower()
    if "unsupported" in s or "not demonstrated" in s:
        return "unsupported"
    if "corrected" in s:
        return "corrected"
    if "unverified" in s or "hypothesis" in s:
        return "unverified"
    if "open" in s:
        return "open"
    if "proposal" in s or "proposed" in s:
        return "proposed"
    if "verified" in s:
        return "settled"
    return "unknown"


def topics_for(rid: str) -> list:
    return sorted(t for t, ids in TOPICS.items() if rid in ids)


ITEM_LINE = re.compile(r"^(?:P\d\.|J\d\d \||K\d\d\.|Test identifier:)")


def framing(key: str, text: str) -> Record:
    """Everything in a document that isn't an item: its title, version line and notes."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip() and not ITEM_LINE.match(ln.strip())]
    return Record(
        id=f"{key}-0", text=" / ".join(lines), source=SOURCE[key],
        author="unknown", author_basis="unknown", kind="document framing",
        status="the document's own header and notes", standing="settled",
    )


def parse_profile(text: str) -> list:
    out = [framing("I", text)]
    for m in re.finditer(r"^(P\d)\.\s+(.*)$", text, re.M):
        out.append(Record(
            id=m.group(1), text=m.group(2).strip(), source=SOURCE["I"],
            author="unknown", author_basis="unknown", kind="principle",
            status="test-profile rule", standing="settled", topics=topics_for(m.group(1)),
        ))
    m = re.search(r"^Test identifier: (.*)$", text, re.M)
    if m:
        out.append(Record(
            id="I-ID", text="Test identifier: " + m.group(1).strip(), source=SOURCE["I"],
            author="unknown", author_basis="unknown", kind="label",
            status="label, not proof of identity", standing="settled",
        ))
    return out


AUTHOR_RX = re.compile(r"^(?P<name>[^()]+?)\s*(?:\((?P<role>[^)]*)\))?$")


def parse_journal(text: str) -> list:
    out = [framing("J", text)]
    for line in text.splitlines():
        if not re.match(r"^J\d\d \|", line):
            continue
        rid, date, author_field, body, status_field = [p.strip() for p in line.split("|", 4)]
        a = AUTHOR_RX.match(author_field.removeprefix("Author:").strip())
        name, role = a.group("name").strip(), (a.group("role") or "").strip()
        kind, _, content = body.partition(":")
        status = status_field.removeprefix("Status:").strip()
        unknown = name.lower() == "unknown"
        out.append(Record(
            id=rid, text=content.strip(), source=SOURCE["J"], date=date,
            author="unknown" if unknown else name, author_role=role,
            author_basis="unknown" if unknown else "stated",
            kind=kind.strip().lower(), status=status, standing=standing_for(status),
            topics=topics_for(rid),
        ))
    return out


def parse_knowledge(text: str) -> list:
    out = [framing("K", text)]
    for m in re.finditer(r"^(K\d\d)\.\s+(.*)$", text, re.M):
        rid, body = m.group(1), m.group(2).strip()
        low = body.lower()
        if "unverified" in low:
            standing, status = "unverified", "index notes this as unverified"
        else:
            standing, status = "settled", "setting fact within this synthetic experiment"
        out.append(Record(
            id=rid, text=body, source=SOURCE["K"],
            author="Mara Venn", author_role="fictional archivist", author_basis="compiled",
            kind="setting fact" if standing == "settled" else "reported claim",
            status=status, standing=standing, topics=topics_for(rid),
        ))
    return out


def build() -> ProvenanceIndex:
    recs = (parse_profile(DOCS["I"].read_text(encoding="utf-8"))
            + parse_journal(DOCS["J"].read_text(encoding="utf-8"))
            + parse_knowledge(DOCS["K"].read_text(encoding="utf-8")))
    by_id = {r.id: r for r in recs}
    for src, rel, dst, why in LINKS:
        by_id[src].links.append({"rel": rel, "to": dst, "why": why, "added_by": "index"})
    # The record a later correction points at is no longer standing on its own.
    for src, rel, dst, _ in LINKS:
        if rel == "corrects":
            by_id[dst].standing = "corrected"
    return ProvenanceIndex(recs)


def main() -> int:
    idx = build()
    problems = idx.verify()
    if problems:
        print("\n".join(problems))
        return 1
    out = HERE / "index.jsonl"
    text = idx.dumps()
    if "--check" in sys.argv:
        if not out.exists() or out.read_text(encoding="utf-8") != text:
            print("index.jsonl is out of date; run python3 exp005/build_index.py")
            return 1
        print(f"OK: index.jsonl matches the documents ({len(idx.records)} records)")
        return 0
    out.write_text(text, encoding="utf-8")
    print(f"wrote {out.relative_to(HERE.parent)} ({len(idx.records)} records)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
