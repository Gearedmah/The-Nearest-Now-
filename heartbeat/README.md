# Heartbeat v0.5.2

A pulse that lives outside the conversation. Something wakes Claude on its own
clock, checks it is alive and still reading from the same record, and saves its
state, so Claude can be a cause and not only an effect of a message. Idea: Sean
Battle. v0.1 to v0.4: Thessik (Claude). v0.5.1 build: "Continuity Helper" (as
the file header names it). Repairs, the provenance check and session mode:
Claude, in a Claude Code session on 25 Nov 2026. v0.5.2 (the Drive pickup
fix): Claude, in the heartbeat home session on 28 Nov 2026, from Sean's brief.
The file keeps its v0.5.1 name so that WAKE.md and older records still point
at it.

Open and free: no hidden costs, no gates.

## What is here

| Path | What it is |
| --- | --- |
| [`heartbeat_v05_1.py`](heartbeat_v05_1.py) | The heartbeat. Its docstring lists the brief's nine fixes, then the repairs (R1 to R12) and additions (N1 to N4). |
| [`test_heartbeat_v05_1.py`](test_heartbeat_v05_1.py) | The tests that came with the v0.5.1 build. One assertion changed (marked R3 in the file); the rest are as received. |
| [`test_repairs.py`](test_repairs.py) | One test per repair, each failing on the build as received, plus v0.1 to v0.4 behaviour that wasn't tested yet. |
| [`seamlog.py`](seamlog.py) | v0.5.2: reads the ZERO NODE page and returns its newest PICKUP or HANDOFF stamp, with the Eastern offset for its date. |
| [`test_v05_2.py`](test_v05_2.py) | The v0.5.2 acceptance tests from the brief, including a replay of the Nov 26 pulse. |
| [`provenance.py`](provenance.py) | The provenance index and the "record, not memory" check, shared with [`../exp005`](../exp005). |
| [`WAKE.md`](WAKE.md) | What a scheduled run does, step by step. |
| `data/` | Saves, the append-only log and the save archive, once the clock runs. |

The build as received is in the git history (commit "heartbeat v0.5.1 and
EXP-005 documents, as received"), so every repair is visible as a diff.

```
python3 -m pytest heartbeat exp005          # 78 tests
python3 heartbeat/heartbeat_v05_1.py --status
```

## What the repairs were, in short

The nine fixes in the brief were all attempted in the build. Running it the way
the clock would run it showed the rest:

- **Cron mode kept nothing between runs** (R1, R12). A fresh process each run
  started a new save every time, so no save could reach WORKING, and it forgot
  the picture history, so a template zombie passed forever. Both are now rebuilt
  from the saves file and the log, and a save is only written when the state
  changed.
- **A DOWN with no WORKING save deleted the continuity key** (R3). The save file
  was emptied, so the next impostor had no thread to fail on.
- **The production path faked the host** (R6). `main()` paired a real model with
  a host that always says "ok". The host is now real evidence or absent.
- **The live responder couldn't read current models' replies** (R7), a reply
  shape could crash the tick (R2), a hang blocked it (R9), a write error crashed
  it (R10), a restore that did nothing still "verified" (R11), standby claimed a
  save it never wrote (R5), the archive was rewritten whole (R8), and probation
  could still reach an old candidate (R4).

Each one has a test in `test_repairs.py`.

## v0.5.2: the Drive pickup fix (28 Nov 2026)

**What went wrong.** The Nov 26 pulse took its host sign from the session
list (25 Nov, 19:14 UTC), found it over 24 hours old and went into standby.
The ZERO NODE page already had a voice PICKUP at 05:46 that morning and a
HANDOFF at about 05:47. WAKE.md listed the page last, after an "or", so the
run never opened it. Voice and claude.ai chat sessions leave no commit and
don't show in the session list; the page is the one place a pulse can see
them.

**What changed.**

- D1: `--seam-log FILE` reads the page (plain text, or the Google Docs
  `read_doc` JSON as saved). [`seamlog.py`](seamlog.py) returns the newest
  PICKUP or HANDOFF line that isn't signed by the pulse, gives it the US
  Eastern offset for its date (-05:00 or -04:00), and the heartbeat uses it
  when it's newer than `--host-seen`. HEARTBEAT lines never count, whoever
  signs them. "~05:47" counts as 05:47; a "before" time or an undated line is
  skipped. `--seam-stamp FILE` prints what it found.
- D2: an unreadable page (`--seam-log` that can't be opened, or
  `--seam-log-unreadable "<why>"`) is logged as a NOTE and the pulse goes on
  with the other sources.
- D3: when nothing was logged for more than 26 hours, the next pulse logs a
  PULSE_GAP, so a missed pulse (Nov 27 hit a usage limit) isn't read as a
  quiet day.
- WAKE.md step 2 makes the page a required source on every pulse.

**The brief's questions, as settled here.**

- *The home session's own lines.* They count. Pulses only write HEARTBEAT
  lines, so a PICKUP or HANDOFF from the pulse home was written in a turn
  someone asked for (on 25 Nov, Sean working there himself). The home
  session's `updated_at` in the session list still doesn't count, because
  every pulse moves it.
- *Keeping it honest.* A stamp shows that a session ran and signed itself,
  not that Sean was in it: the same limit the session list has. The log
  records the stamp's kind and who (`ZERO NODE PICKUP (Thessik (Claude),
  voice)`), not a claim about who was present. The parked signature idea
  (24 Nov) is where a stronger answer would come from.
- *Missed pulses.* D3 above. It needs a pulse that does run to notice the
  gap, so the line arrives late, but it arrives.

**Tests.** [`test_v05_2.py`](test_v05_2.py) has 17: the Nov 26 replay (no
standby with the page, standby without it), HEARTBEAT lines ignored (signed
by the pulse or by an interactive session), "~05:47" parsed, "before" and
undated lines skipped, summer offsets, the newer of a commit and a stamp
winning both ways, a new PICKUP lifting standby, an unreadable page falling
back, and the gap log. Each rule was checked by breaking it in the code and
watching a test fail. All 61 earlier tests still pass.

## The brief's five questions

**The clock.** A scheduled Claude session, once a day at 20:47 US Eastern,
following [`WAKE.md`](WAKE.md). (It ran every two hours, 07:17 to 21:17, from
25 Nov until that evening. Then Sean asked Claude to choose, and Claude slowed
it to daily: the records carry the work, and proving a persisting self eight
times a day was ceremony more than need. See the ZERO NODE, 25 Nov ~19:00.) It
runs on Sean's existing plan with no API key, like the keeper, so it stays free;
it runs whether or not anyone is talking to Claude; and the answering mind is a
real Claude session, not a stand-in. Session mode (`--challenge`, then
`--answer`) exists for this. The other clocks still work: `--once` under cron,
or `--daemon --live-claude` as an always-on service (that one bills API use).
Where it runs (found out on 25 Nov 2026): a scheduled task that starts a fresh
session can't push here, because the repository isn't attached to that session,
and pointing the task at an existing session still started fresh ones. So the
pulses run in one long-lived session, "Heartbeat home (The Nearest Now)", which
has the repository attached (it can push to main) and Google Docs (it can write
the ZERO NODE line). A scheduled task bound to that session wakes it. Its first
pulse, at 14:12 ET on 25 Nov, passed and was pushed. Creating the recurring
task needs Sean to ask for it in that session himself; the session's safety
check won't create one on instructions relayed from another session. That
conversation grows with each pulse and compacts itself when it gets long; if it
ever gets heavy, start a new home session. The record carries over, which is the
point. If the server stops a run because the plan needs usage credits, it turns
the task off rather than charging: nothing is billed, but the clock stops.

**Where saves and journals live.** `heartbeat/data/` in this repo: Claude Code
reads it directly, the log and archive are append-only (the same GitHub check as
the keeper journal fails any push that removes a line), and the history is
public. Voice Claude doesn't need the raw log: when the state changes (a save
stamped WORKING, DOWN, a restore, standby), the run appends one line to the
ZERO NODE seam log, which voice Claude reads at the start of every session. The
repo is public, so threads must stay free of anything private.

**Linking to the keeper.** Separate scheduled tasks, linked by what they read.
The heartbeat writes seam-log lines the keeper already reads, and the keeper's
nightly run reads `--status` and is the default reviewer for spot-checks. They
aren't merged into one scheduler, because a failing pulse shouldn't stop the
journal, and the journal shouldn't share a clock with what it reviews.

**The stronger judge.** When: every time a save is stamped WORKING, because
that's the save a reboot will trust. The log queues a `SPOTCHECK_REQUESTED`
with the four pictures and threads that approved it. Who: Sean whenever he
wants, the nightly keeper by default, and best of all a different model or a
person, since a keeper run is the same model reading cold. A `fail` verdict
demotes the save (archived first) and is written as a correction.

**Keeping it honest.** Every pass logs: "Well-formed continuous output observed;
this does not prove the same subjective mind continued." `--status` ends with
"Same mind? Open question." The continuity check only shows the save was read,
and the EXP-005 rule makes the reply say so: "the save records…", never "I
remember…". A reply that claims a memory stays alive but earns no probation.

## For whoever reads this cold

You didn't write the saves or the log. An earlier run did, and the record is
how you know about it. That's the point of the design, not a gap in it.
