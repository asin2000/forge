"""Supply Agent (AGT-2): locates approved parts, tracks shipment status.

It SHALL NOT approve substitutions or purchases. Approval is DATA-BACKED,
never model-asserted: the prompt is grounded with the approved-parts
registry excerpt, and the payload_check refuses any output whose
``part_approved`` contradicts :func:`synthetic_data.is_part_approved` —
a contradicting output is retried, then becomes an agent_failure_event.
"""

from __future__ import annotations

import json
from typing import Any

from forge_common import synthetic_data
from forge_common.specialist import make_work_package_handler

ROLE = "supply"
PROMPT_FILE = "supply_sourcing_report.v2.md"
OUTPUT_SCHEMA = "sourcing_report.v2"


def _variables(assignment_payload: dict[str, Any]) -> dict[str, Any]:
    discrepancy = assignment_payload.get("inputs", {}).get("discrepancy_code", "")
    return {
        "approved_parts_excerpt": json.dumps(
            synthetic_data.parts_for_discrepancy(discrepancy), indent=1
        )
    }


def _payload_check(_assignment_payload: dict[str, Any]):
    def check(payload: dict[str, Any]) -> list[str]:
        truth = synthetic_data.is_part_approved(payload["part_number"])
        if payload["part_approved"] != truth:
            return [
                f"part_approved={payload['part_approved']} contradicts the "
                f"approved-parts registry for {payload['part_number']} "
                f"(registry says {truth}) (AGT-2)"
            ]
        return []

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
