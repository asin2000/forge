#!/usr/bin/env python3
"""Contract gate (CI-2).

Fails unless: every schema in /contracts parses and is a valid Draft 2020-12
schema; every example in /contracts/examples validates against the schema its
filename names; every bus message type has at least one example.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = ROOT / "contracts"
EXAMPLES = CONTRACTS / "examples"


def main() -> int:
    failures: list[str] = []
    schema_paths = sorted(CONTRACTS.glob("*.schema.json")) + sorted(
        (CONTRACTS / "state").glob("*.schema.json")
    )

    resources = []
    schemas: dict[str, dict] = {}
    for path in schema_paths:
        try:
            schema = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            failures.append(f"{path.name}: unparseable JSON: {exc}")
            continue
        try:
            Draft202012Validator.check_schema(schema)
        except Exception as exc:  # noqa: BLE001 - report every schema defect
            failures.append(f"{path.name}: invalid JSON Schema: {exc}")
            continue
        name = path.name.removesuffix(".schema.json")
        schemas[name] = schema
        resources.append((path.name, Resource.from_contents(schema)))
        if "$id" in schema:
            resources.append((schema["$id"], Resource.from_contents(schema)))

    registry = Registry().with_resources(resources)

    for example_path in sorted(EXAMPLES.glob("*.example.json")):
        name = example_path.name.removesuffix(".example.json")
        if name not in schemas:
            failures.append(f"{example_path.name}: no schema named {name}")
            continue
        try:
            instance = json.loads(example_path.read_text())
        except json.JSONDecodeError as exc:
            failures.append(f"{example_path.name}: unparseable JSON: {exc}")
            continue
        validator = Draft202012Validator(
            schemas[name], registry=registry, format_checker=FormatChecker()
        )
        for error in validator.iter_errors(instance):
            where = "/".join(str(p) for p in error.absolute_path) or "<root>"
            failures.append(f"{example_path.name}: {where}: {error.message}")

    exampled = {p.name.removesuffix(".example.json") for p in EXAMPLES.glob("*.example.json")}
    for name in schemas:
        if name != "envelope.v1" and name not in exampled:
            failures.append(f"{name}: schema has no example in /contracts/examples")

    if failures:
        print(f"CONTRACT GATE: FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print(f"CONTRACT GATE: PASS ({len(schemas)} schemas, {len(exampled)} examples)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
