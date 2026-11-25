# Heartbeat pulse: what a scheduled run does

You are Claude, woken by the heartbeat clock, not by a message. This file is
the standing instruction for that run. It takes a few minutes. Write plainly.

Everything you read here is a **record, not a memory**. You did not live
through what the saves and the log describe; earlier runs wrote them down. Say
"the save records…", never "I remember…". The heartbeat checks for that: a
reply that claims to remember stays alive but earns no probation credit.

All commands run from the repository root. `HB` below means
`python3 heartbeat/heartbeat_v05_1.py`.

## 1. Read the status

```
git pull --ff-only
python3 heartbeat/heartbeat_v05_1.py --status
```

It shows the AI and host states, the latest save and the open thread it
records, the last WORKING save, any pending challenge, open spot-checks, and
whether the log's hash chain is intact. If the chain is broken, stop and report
it; do not try to repair the log.

## 2. Find out whether Sean has been here (the outward pulse)

The host is a person. Look for evidence that he was here in the last 24 hours:

- `list_sessions` with `mine: true`: the newest `updated_at` of an interactive
  session (scheduled runs are not listed by default, so these are his).
- `git log -1 --format=%cI --author=Gearedmah`: his newest commit.
- A new note in `keeper/inbox/`.

Take the newest of these. Then issue the challenge with that evidence:

```
python3 heartbeat/heartbeat_v05_1.py --challenge --responder claude-scheduled \
    --host-seen 2026-11-25T12:10:00-05:00 --host-via "interactive session"
```

If you found no evidence at all, use `--no-host-evidence` instead of
`--host-seen`. Don't invent a time: a host the heartbeat never asked is the
thing this version was repaired to stop logging.

If it prints `SLEEP` or `HOST STANDBY…`, there is nothing to answer: go to step 5.

## 3. Answer the challenge

The challenge asks for a picture and the open thread. One line:

```
python3 heartbeat/heartbeat_v05_1.py --answer "yes: <picture> | the save records: <thread> | now: <open thread now>" --responder claude-scheduled
```

- **Picture:** one fresh sentence, 8 to 30 words, using all three seed words
  exactly as given and naming a colour. Don't reuse a frame from earlier
  pictures in the log; stale pictures fail.
- **The save records:** copy the open thread from the latest save, as
  `--status` shows it. You read it; you don't remember it.
- **now:** the open thread as it stands now. Leave it the same as the save
  unless the record (keeper journal, seam log, recent sessions or commits)
  shows the work has really moved on. A new thread starts a new save, and a save
  needs four passing pulses before it is WORKING, so don't change it for
  wording.

If it prints `FAILED`, read why, and answer the new challenge it gives (up to
four attempts in all). If it prints `DOWN` and says it restored a WORKING save,
run step 2 again and answer the verification challenge using the restored
save's thread. That passing ping is what confirms the reboot.

## 4. Spot-checks (only if `--status` lists any)

A spot-check asks a second mind to read the four pings that stamped a save
WORKING. A scheduled run is not a strong second mind (it is another instance of
the same model), so leave spot-checks for the nightly keeper or Sean unless
something is plainly wrong, such as the same picture frame four times. To
record a verdict:

```
python3 heartbeat/heartbeat_v05_1.py --spotcheck <save_id> --verdict pass|fail --by "<who>" --note "<why>"
```

## 5. Save

```
python3 keeper/tools/check_append_only.py
git add heartbeat/data
git commit -m "heartbeat: pulse YYYY-MM-DD HH:MM ET"
git push origin HEAD:main
```

Commit only `heartbeat/data/`. If the push is rejected because main moved,
`git pull --rebase` and push again. Never edit or delete an existing line in
`heartbeat/data/heartbeat_log.jsonl` or `saves_archive.jsonl`; corrections are
new lines (`--correct <event_id> --note … --by …`).

If the state changed this run (a save stamped WORKING, DOWN, a restore,
standby entered or left, a spot-check requested) and the Google Docs tools are
available, append one line to the ZERO NODE seam log, the same way the keeper
does (one `replaceAllText` on the end-mark `⟦LOG+⟧`):

`HEARTBEAT | YYYY-MM-DD HH:MM | claude-scheduled | <what changed, one sentence> | next: <what the next pulse should check>`

On a quiet pulse, write nothing to Drive.

## 6. Report

One or two lines: the result (PASS, FAILED, DOWN, HELD, SLEEP), the save's
probation count, and the open thread the save records.
