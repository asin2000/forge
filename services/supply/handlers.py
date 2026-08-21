"""Supply Agent (AGT-2): locates approved parts, tracks shipment status.

It SHALL NOT approve substitutions or purchases. ALL domain facts are
DATA-BACKED, never model-asserted:
- ``part_approved`` must equal the registry's DISCREPANCY-SPECIFIC answer
  (a part approved for an electrical discrepancy is not approved for the
  hydraulic one);
- ``shipment_status`` and ``eta_days`` must equal the synthetic
  supply-chain facts for known parts, and ``not_ordered``/0 for unknown
  parts.
A contradicting output is retried, then becomes an agent_failure_event.
"""

from __future__ import annotations

import json
from typing import Any

from forge_common import synthetic_data
from forge_common.specialist import make_work_package_handler

ROLE = "supply"
PROMPT_FILE = "supply_sourcing_report.v3.md"
OUTPUT_SCHEMA = "sourcing_report.v3"


def _variables(assignment_payload: dict[str, Any]) -> dict[str, Any]:
    discrepancy = assignment_payload.get("inputs", {}).get("discrepancy_code", "")
    return {
        "approved_parts_excerpt": json.dumps(
            synthetic_data.parts_for_discrepancy(discrepancy), indent=1
        )
    }


def _payload_check(assignment_payload: dict[str, Any]):
    discrepancy = assignment_payload.get("inputs", {}).get("discrepancy_code", "")

    def check(payload: dict[str, Any]) -> list[str]:
        errors: list[str] = []
        part = payload["part_number"]
        truth = synthetic_data.is_part_approved_for(part, discrepancy)
        if payload["part_approved"] != truth:
            errors.append(
                f"part_approved={payload['part_approved']} contradicts the registry "
                f"for {part} under {discrepancy or 'unknown discrepancy'} "
                f"(registry says {truth}) (AGT-2)"
            )
        facts = synthetic_data.sourcing_facts(part)
        if facts is None:
            if payload["shipment_status"] != "not_ordered" or payload["eta_days"] != 0:
                errors.append(
                    f"unknown part {part}: shipment_status must be not_ordered "
                    f"and eta_days 0, not {payload['shipment_status']}/{payload['eta_days']}"
                )
        else:
            if payload["shipment_status"] != facts["shipment_status"]:
                errors.append(
                    f"shipment_status {payload['shipment_status']} contradicts the "
                    f"supply-chain data ({facts['shipment_status']}) for {part}"
                )
            if payload["eta_days"] != facts["eta_days"]:
                errors.append(
                    f"eta_days {payload['eta_days']} contradicts the supply-chain "
                    f"data ({facts['eta_days']}) for {part}"
                )
        return errors

    return check


def make_handler(db: Any, model: Any):
    return make_work_package_handler(
        db,
        role=ROLE,
        model=model,
        prompt_file=PROMPT_FILE,
        output_schema_version=OUTPUT_SCHEMA,
        payload_check_factory=_payload_check,
        variables_factory=_variables,
    )
