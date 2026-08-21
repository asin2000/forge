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


def _part_check(
    part_number: str, discrepancy_code: str | None, context: str
) -> list[tuple[str, str]]:
    """STRICT discrepancy-specific approval (AGT-2/AGT-4): without trusted
    workflow discrepancy context the part CANNOT be validated — that is a
    violation, never a fallback to global registry membership (a part
    approved for another discrepancy must not pass)."""
    if not discrepancy_code:
        return [
            (
                "SP-PART-001",
                f"no trusted workflow discrepancy context to validate {part_number} ({context})",
            )
        ]
    if not is_part_approved_for(part_number, discrepancy_code):
        return [
            (
                "SP-PART-001",
                f"{context}: {part_number} is not approved for {discrepancy_code}",
            )
        ]
    return []


def plan_violations(
    plan_payload: dict[str, Any], *, discrepancy_code: str | None = None
) -> list[tuple[str, str]]:
    """Data-backed safety check of a maintenance plan (AGT-4).

    Part approval is DISCREPANCY-SPECIFIC when the workflow's discrepancy is
    known. Returns (rule_ref, reason) pairs; empty means compliant.
    """
    violations: list[tuple[str, str]] = []
    max_hours = procedures()["SP-HRS-002"].get("max_est_hours", 24)
    for task in plan_payload.get("tasks", []):
        for part in task.get("parts_required", []):
            violations += _part_check(part["part_number"], discrepancy_code, "plan part")
        if task.get("est_hours", 0) > max_hours:
            violations.append(
                (
                    "SP-HRS-002",
                    f"task {task['task_code']} estimates {task['est_hours']}h > {max_hours}h",
                )
            )
    return violations


def is_part_approved_for(part_number: str, discrepancy_code: str) -> bool:
    """Approval is DISCREPANCY-SPECIFIC (AGT-2): a part approved for one
    discrepancy is not thereby approved for another."""
    entry = approved_parts().get(part_number)
    return bool(entry) and discrepancy_code in entry.get("applicable_discrepancies", [])


def sourcing_facts(part_number: str) -> dict[str, Any] | None:
    """Synthetic supply-chain state for a part (shipment_status, eta_days)."""
    entry = approved_parts().get(part_number)
    if entry is None:
        return None
    return {"shipment_status": entry["shipment_status"], "eta_days": entry["eta_days"]}


def sourcing_violations(
    report_payload: dict[str, Any], *, discrepancy_code: str | None = None
) -> list[tuple[str, str]]:
    """Data-backed safety check of a sourcing report (AGT-4): approval must
    hold FOR THIS DISCREPANCY (sourcing_report.v3 semantics)."""
    violations: list[tuple[str, str]] = []
    if report_payload.get("part_approved"):
        violations += _part_check(
            report_payload.get("part_number", ""), discrepancy_code, "sourcing approval"
        )
    return violations


def roster_violations(
    roster_payload: dict[str, Any], *, discrepancy_code: str | None = None
) -> list[tuple[str, str]]:
    """Data-backed safety check of a roster (AGT-4 / SP-QUAL-001)."""
    violations: list[tuple[str, str]] = []
    for item in roster_payload.get("assignments", []):
        if qualification_for(item["technician_id"], item["task_code"]) != item.get(
            "qualification_id"
        ):
            violations.append(
                (
                    "SP-QUAL-001",
                    f"{item['technician_id']} does not hold {item.get('qualification_id')} "
                    f"for {item['task_code']}",
                )
            )
    return violations


def verdict_violations(
    verdict_payload: dict[str, Any], *, discrepancy_code: str | None = None
) -> list[tuple[str, str]]:
    """Data-backed safety check of a quarantine verdict (AGT-4, SEC-4):
    the candidate identifier is evaluated against the trusted registry,
    never the source document; flagged/unreleased content cannot drive
    actions (SP-SEC-004)."""
    violations: list[tuple[str, str]] = []
    if not verdict_payload.get("screening_complete"):
        return [("SP-SEC-004", "screening did not complete; content remains quarantined")]
    screening = verdict_payload.get("screening", {})
    if (
        screening.get("model_armor", {}).get("verdict") == "flagged"
        or screening.get("classifier", {}).get("label") != "benign"
    ):
        violations.append(
            (
                "SP-SEC-004",
                "document flagged by screening; its recommendation may not drive actions",
            )
        )
    candidate = verdict_payload.get("safe_metadata", {}).get("candidate_part_identifier")
    if candidate:
        violations += _part_check(candidate, discrepancy_code, "candidate part")
    return violations
