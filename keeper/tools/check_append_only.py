#!/usr/bin/env python3
"""Check that no line in the journal, inbox or heartbeat log/archive was ever erased.

Compares a base revision with a head revision (default: origin/main against
the working tree including uncommitted changes) and fails if any line under
the protected folders was deleted or changed, or any file was deleted or
renamed. Adding new lines and new files is always fine.

Usage:
    python3 keeper/tools/check_append_only.py            # origin/main vs working tree
    python3 keeper/tools/check_append_only.py BASE       # BASE vs working tree
    python3 keeper/tools/check_append_only.py BASE HEAD  # two commits
"""
import subprocess
import sys

PROTECTED = [
    "keeper/journal/",
    "keeper/inbox/",
    "heartbeat/data/heartbeat_log.jsonl",
    "heartbeat/data/saves_archive.jsonl",
]


def git(*args):
    return subprocess.run(
        ["git", *args], check=True, capture_output=True, text=True
    ).stdout


def main():
    if len(sys.argv) in (2, 3):
        rev_args = sys.argv[1:]  # BASE [HEAD]; no HEAD means the working tree
    elif len(sys.argv) == 1:
        rev_args = ["origin/main"]
    else:
        print(__doc__)
        return 2

    # An all-zero base means a brand-new branch: nothing existed to erase.
    if rev_args[0].strip("0") == "":
        print("New branch: nothing to compare against.")
        return 0

    problems = []

    status = git("diff", "-M", "--name-status", *rev_args, "--", *PROTECTED)
    for line in status.splitlines():
        code, *paths = line.split("\t")
        if code[0] in "DR":
            problems.append(f"{code[0]} {' -> '.join(paths)}: file removed or renamed")

    numstat = git("diff", "--numstat", *rev_args, "--", *PROTECTED)
    for line in numstat.splitlines():
        added, deleted, path = line.split("\t", 2)
        if deleted not in ("0", "-") and not any(path in p for p in problems):
            problems.append(f"{path}: {deleted} line(s) removed or changed")

    if problems:
        print("Nothing is ever erased. These changes remove journal history:")
        for p in problems:
            print("  " + p)
        print("Write corrections as new lines in a new entry instead.")
        return 1

    print("OK: journal history intact (only additions).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
