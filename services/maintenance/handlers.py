"""Maintenance Agent (AGT-1): produces maintenance action plans.

It SHALL NOT release equipment: this module requests no transitions, and the
RELEASED state is HUM-1-gated at the only state-write path — the boundary
holds by construction, and the negative test proves the gate refuses it.
"""

from __future__ import annotations

from typing import Any

from forge_common import synthetic_data
from forge_common.specialist import make_work_package_handler

ROLE = "maintenance"
PROMPT_FILE = "maintenance_action_plan.v2.md"
OUTPUT_SCHEMA = "maintenance_action_plan.v2"


def _variables(_assignment_payload: dict[str, Any]) -> dict[str, Any]:
    # AGT-1 grounding (live finding, Day 7): ungrounded plans invented task
    # codes nobody is qualified for — every roster then failed, the reserve
    # too, and the workflow blocked. Plans schedule STAFFABLE work only.
    return {"task_catalog": ", ".join(sorted(synthetic_data.staffable_task_codes()))}


def _payload_check(_assignment_payload: dict[str, Any]):
    def check(payload: dict[str, Any]) -> list[str]:
        catalog = synthetic_data.staffable_task_codes()
        return [
            f"task {task.get('task_code')} is not in the staffable task catalog (AGT-1)"
            for task in payload.get("tasks", [])
            if task.get("task_code") not in catalog
        ]

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
