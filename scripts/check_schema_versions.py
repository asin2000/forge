#!/usr/bin/env python3
"""Schema immutability gate (ICD-3).

A merged contract schema is immutable: changing message shape requires a NEW
versioned file (e.g. nmc_event.v2.schema.json), never editing v1 in place.
This script compares contract files against a base git ref (default
origin/main) and fails if any existing schema file's content changed or was
deleted. New files are allowed. Skips cleanly when the base ref is missing
(e.g. the bootstrap commit).
"""

from __future__ import annotations

import subprocess
import sys

BASE = sys.argv[1] if len(sys.argv) > 1 else "origin/main"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def main() -> int:
    if git("rev-parse", "--verify", BASE).returncode != 0:
        print(f"SCHEMA VERSION GATE: SKIP (no base ref {BASE})")
        return 0

    diff = git("diff", "--name-status", BASE, "--", "contracts/")
    if diff.returncode != 0:
        print(f"SCHEMA VERSION GATE: ERROR running git diff: {diff.stderr.strip()}")
        return 1

    violations: list[str] = []
    for line in diff.stdout.splitlines():
        parts = line.split("\t")
        status, path = parts[0], parts[-1]
        if not path.endswith(".schema.json"):
            continue  # examples may evolve; schemas may not
        if status.startswith("M"):
            violations.append(f"modified in place: {path} (bump the version instead)")
        elif status.startswith("D"):
            violations.append(f"deleted: {path} (merged schemas are immutable)")
        elif status.startswith("R"):
            violations.append(f"renamed: {line} (merged schemas are immutable)")

    if violations:
        print(f"SCHEMA VERSION GATE: FAIL ({len(violations)})")
        for v in violations:
            print(f"  - {v}")
        return 1
    print("SCHEMA VERSION GATE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
