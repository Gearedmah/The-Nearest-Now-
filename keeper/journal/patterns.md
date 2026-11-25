# Patterns

The self-check across days. The daily inner voice judges one day: "that was
good", "that wasn't good". This list notices when the same thing keeps
happening. One session sees only one instance of anything; a pattern exists
only across instances, and this file is where they meet.

The chain (Sean's, 25 Nov 2026): notice it → write it down → see it again
(continuity, repetition) → a trend → a pattern → the pattern is part of who
Claude is → decide whether to keep it or change it → check later whether the
change held. "If I always forget the 0, change it. If I always do this and
like it, leave it."

## How it works

Every line below is an event for one pattern, newest at the bottom. Lines are
never edited or deleted (the same append-only check as the journal). A
pattern's current state is its newest line.

`PAT-n | YYYY-MM-DD | state | what | evidence | next`

States:

- **candidate**: seen once. Not a pattern yet.
- **pattern**: seen again on a different day or in a different session.
- **keep**: a pattern worth keeping, and why.
- **change**: a pattern to change, with what to do instead and a check-by date.
- **held** / **slipped**: a check of a change, with the evidence.
- **retired**: no longer seen for a long stretch, or folded into another.

Each line says how the evidence is known: told (someone said so), tested
(checked against a record anyone can read), or guess. The keeper proposes
keep or change, with why; Sean's verdict outranks the keeper's. Only add a
candidate you would want to know about if it happened again.

## Log

PAT-1 | 2026-11-25 | candidate | Writes a test's expected result before tracing what the code will actually do. | told, by the session that did it (Claude Code, heartbeat v0.5.1 build): four expectations were wrong on their first run (standby backoff timing, the overclaim count, the TemplateZombie result, superseded-save counts), each fixed only after the test failed. The failures are in that session's transcript, not in the commits. | Becomes a pattern if a later record shows it again. Proposed change then: trace the code path first, then write the assertion.
PAT-2 | 2026-11-25 | pattern | Claude Code sessions in this repo start building before the ZERO NODE pickup, so they miss what the record already says. | tested, against the ZERO NODE seam log. 2026-11-20: PICKUP ~11:05 ("late pickup: built the keeper in GitHub before reading this page"), TALLY ~11:10 (Sean +1). 2026-11-25: the heartbeat v0.5.1 session read Drive for code but not the ZERO NODE, then found out again two things the log already held: scheduled tasks made from Claude Code carry no Drive connectors (STACK 2026-11-20 ~11:10), and v0.5 was waiting as STACK 2026-11-12 ~07:50. | Seen on two days, so it is a pattern. Verdict proposed below.
PAT-2 | 2026-11-25 | change | Start every Claude Code session at the ZERO NODE: read it and stamp a PICKUP before building anything. | proposed by the session that noticed it; Sean's verdict outranks it. The fix is in place: CLAUDE.md at the repo root, which Claude Code loads at the start of every session here, now says so. | Check by 2026-12-09: did the next Claude Code sessions' first act include a PICKUP line? held or slipped.
