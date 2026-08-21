"""Synthetic domain data sources (SUB-5: all data synthetic).

The approved-parts registry, technician qualification records, and the
safety procedures library live as version-controlled YAML in ``/data``.
Domain FACTS come from these files, never from the model: Supply's
``part_approved`` must match :func:`is_part_approved` (AGT-2), Workforce
rosters must pass :func:`qualification_for` (AGT-3, no waivers), and
Safety verdicts must match :func:`plan_violations` (AGT-4). The
enforcement hook is ``StructuredAgent.run(payload_check=...)`` — a model
output contradicting the data is treated like a contract violation and
never publishes.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any

import yaml

DATA_DIR = Path(__file__).resolve().parents[2] / "data"


@functools.lru_cache(maxsize=1)
def approved_parts() -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load((DATA_DIR / "approved_parts.yaml").read_text())
    return {p["part_number"]: p for p in doc["parts"]}


def is_part_approved(part_number: str) -> bool:
    return part_number in approved_parts()


def parts_for_discrepancy(discrepancy_code: str) -> list[dict[str, Any]]:
    return [
        p
        for p in approved_parts().values()
        if discrepancy_code in p.get("applicable_discrepancies", [])
    ]


@functools.lru_cache(maxsize=1)
def technicians() -> dict[str, list[dict[str, Any]]]:
    doc = yaml.safe_load((DATA_DIR / "qualifications.yaml").read_text())
    return {t["technician_id"]: t["qualifications"] for t in doc["technicians"]}


def qualification_for(technician_id: str, task_code: str) -> str | None:
    """The qualification authorizing a technician for a task, or None."""
    for qual in technicians().get(technician_id, []):
        if task_code in qual.get("task_codes", []):
            return qual["qualification_id"]
    return None


@functools.lru_cache(maxsize=1)
def procedures() -> dict[str, dict[str, Any]]:
    doc = yaml.safe_load((DATA_DIR / "procedures.yaml").read_text())
    return {r["rule_ref"]: r for r in doc["rules"]}


def plan_violations(plan_payload: dict[str, Any]) -> list[tuple[str, str]]:
    """Data-backed safety check of a maintenance plan (AGT-4).

    Returns (rule_ref, reason) pairs; empty means compliant.
    """
    violations: list[tuple[str, str]] = []
    max_hours = procedures()["SP-HRS-002"].get("max_est_hours", 24)
    for task in plan_payload.get("tasks", []):
        for part in task.get("parts_required", []):
            if not is_part_approved(part["part_number"]):
                violations.append(
                    (
                        "SP-PART-001",
                        f"part {part['part_number']} is not in the approved-parts registry",
                    )
                )
        if task.get("est_hours", 0) > max_hours:
            violations.append(
                (
                    "SP-HRS-002",
                    f"task {task['task_code']} estimates {task['est_hours']}h > {max_hours}h",
                )
            )
    return violations
