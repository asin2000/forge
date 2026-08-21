"""Workforce Agent (AGT-3): technicians matched strictly to task-code
qualifications — no waivers.

The prompt is grounded with the qualification records, and the
payload_check refuses any roster naming a technician who does not hold the
task-code qualification (or citing the wrong qualification_id). A waiver
attempt is retried, then becomes an agent_failure_event.
"""

from __future__ import annotations

import json
from typing import Any

from forge_common import synthetic_data
from forge_common.specialist import make_work_package_handler

ROLE = "workforce"
PROMPT_FILE = "workforce_roster.v1.md"
OUTPUT_SCHEMA = "roster_assignment.v2"


def _variables(assignment_payload: dict[str, Any]) -> dict[str, Any]:
    inputs = assignment_payload.get("inputs", {})
    return {
        "task_codes": ", ".join(inputs.get("task_codes", [])),
        "roster_excerpt": json.dumps(
            {t: quals for t, quals in synthetic_data.technicians().items()}, indent=1
        ),
    }


def _payload_check(_assignment_payload: dict[str, Any]):
    def check(payload: dict[str, Any]) -> list[str]:
        errors = []
        for item in payload.get("assignments", []):
            qual = synthetic_data.qualification_for(item["technician_id"], item["task_code"])
            if qual is None:
                errors.append(
                    f"{item['technician_id']} holds no qualification for "
                    f"{item['task_code']} — qualifications are never waived (AGT-3)"
                )
            elif qual != item["qualification_id"]:
                errors.append(
                    f"{item['technician_id']} qualification for {item['task_code']} "
                    f"is {qual}, not {item['qualification_id']} (AGT-3)"
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
