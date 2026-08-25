#!/usr/bin/env python3
"""Traceability gate (DFT-1).

Enforces: (a) every requirement ID from the frozen baseline is mapped with
implemented_by, verified_by, and a valid verification_method; (b) every
production module (src/**, services/**/*.py, scripts/*.py) is referenced by at
least one requirement; (c) in strict mode ('strict: true' in the YAML, or
--strict), no 'pending' placeholders remain.
"""

from __future__ import annotations

import datetime
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "requirements" / "traceability.yaml"

EXPECTED_IDS = (
    [f"ORC-{i}" for i in range(1, 6)]
    + [f"AGT-{i}" for i in range(1, 8)]
    + [f"SEC-{i}" for i in range(1, 5)]
    + [f"HUM-{i}" for i in range(1, 4)]  # v1.3: HUM-3
    + [f"AUD-{i}" for i in range(1, 4)]
    + [f"REG-{i}" for i in range(1, 6)]  # v1.2
    + [f"OBS-{i}" for i in range(1, 3)]  # v1.2
    + [f"DAT-{i}" for i in range(1, 4)]  # v1.2
    + [f"ICD-{i}" for i in range(1, 7)]
    + [f"PLT-{i}" for i in range(1, 7)]
    + [f"CI-{i}" for i in range(1, 10)]  # v1.2: CI-9
    + [f"DFT-{i}" for i in range(1, 6)]
    + [f"SUB-{i}" for i in range(1, 9)]  # v1.2: SUB-8
)
METHODS = {"test", "inspection", "demonstration", "document"}


def production_modules() -> list[str]:
    mods: list[str] = []
    for pattern in ("src/**/*.py", "services/**/*.py", "scripts/*.py"):
        for path in ROOT.glob(pattern):
            rel = str(path.relative_to(ROOT))
            if "__init__" not in rel:
                mods.append(rel)
    return sorted(mods)


def main() -> int:
    strict_flag = "--strict" in sys.argv
    data = yaml.safe_load(TRACE.read_text())
    strict = bool(data.get("strict", False)) or strict_flag
    reqs = data.get("requirements", {})
    failures: list[str] = []

    # Entrant QA (2026-08-25): "strict PASS" with open-ended exemptions is
    # misleading. Every exemption must be DATED with a reason and DIES on
    # its date — an expired exemption fails the gate loudly.
    pending_allowed: set[str] = set()
    today = datetime.date.today()
    for entry in data.get("pending_allowed", []) or []:
        if not isinstance(entry, dict) or not {"id", "until", "reason"} <= set(entry):
            failures.append(f"pending_allowed entry must be {{id, until, reason}}: {entry!r}")
            continue
        until = entry["until"]
        if not isinstance(until, datetime.date):
            failures.append(f"pending_allowed[{entry['id']}]: until must be a date, got {until!r}")
            continue
        if today > until:
            failures.append(
                f"pending_allowed[{entry['id']}]: exemption EXPIRED {until} "
                f"({entry['reason']}) — close it or re-date it with rationale"
            )
            continue
        pending_allowed.add(entry["id"])

    missing = [rid for rid in EXPECTED_IDS if rid not in reqs]
    unknown = [rid for rid in reqs if rid not in EXPECTED_IDS]
    if missing:
        failures.append(f"unmapped requirement IDs: {missing}")
    if unknown:
        failures.append(f"unknown requirement IDs (not in the requirements baseline): {unknown}")

    referenced: set[str] = set()
    for rid, entry in reqs.items():
        for field in ("implemented_by", "verified_by"):
            values = entry.get(field)
            if not isinstance(values, list) or not values:
                failures.append(f"{rid}: {field} must be a non-empty list")
                continue
            for v in values:
                if v == "pending":
                    if strict and rid not in pending_allowed:
                        failures.append(f"{rid}: {field} still 'pending' in strict mode")
                else:
                    referenced.add(v.rstrip("/"))
        method = entry.get("verification_method")
        if method not in METHODS:
            failures.append(f"{rid}: verification_method '{method}' not in {sorted(METHODS)}")

    for mod in production_modules():
        covered = any(
            mod == ref or mod.startswith(ref + "/") or ref == mod.rsplit("/", 1)[0]
            for ref in referenced
        )
        if not covered:
            failures.append(f"module not referenced by any requirement: {mod}")

    if failures:
        print(f"TRACEABILITY GATE: FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    mode = "strict" if strict else "build (pending allowed)"
    print(f"TRACEABILITY GATE: PASS ({len(reqs)} requirements, mode: {mode})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
