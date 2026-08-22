"""Maintenance Agent (AGT-1): produces maintenance action plans.

It SHALL NOT release equipment: this module requests no transitions, and the
RELEASED state is HUM-1-gated at the only state-write path — the boundary
holds by construction, and the negative test proves the gate refuses it.
"""

import json
from typing import Any

from forge_common import synthetic_data
from forge_common.specialist import make_work_package_handler

ROLE = "maintenance"
PROMPT_FILE = "maintenance_action_plan.v3.md"
OUTPUT_SCHEMA = "maintenance_action_plan.v2"


def _discrepancy(assignment_payload: dict[str, Any]) -> str:
    return assignment_payload.get("inputs", {}).get("discrepancy_code", "")


def _variables(assignment_payload: dict[str, Any]) -> dict[str, Any]:
    # AGT-1 grounding (two live findings, Days 7-8): an ungrounded planner
    # invents both TASK CODES nobody is qualified for AND PART NUMBERS not
    # approved for the discrepancy. The first stalls the roster; the second
    # earns a correct Safety veto that stalls the rework loop (run 9 of the
    # 10x acceptance). So the prompt carries BOTH registries — the staffable
    # task catalog and the approved parts for THIS discrepancy — exactly as
    # Supply is grounded. The planner plans; Safety still authorizes.
    return {
        "task_catalog": ", ".join(sorted(synthetic_data.staffable_task_codes())),
        "approved_parts_excerpt": json.dumps(
            synthetic_data.parts_for_discrepancy(_discrepancy(assignment_payload)), indent=1
        ),
    }


def _payload_check(assignment_payload: dict[str, Any]):
    discrepancy = _discrepancy(assignment_payload)

    def check(payload: dict[str, Any]) -> list[str]:
        # Reject a plan that strays from either registry AT SOURCE, so the
        # AGT-7 retry re-plans with the approved list in front of it, rather
        # than passing a doomed plan downstream to a Safety veto.
        errors: list[str] = []
        for rule, reason in synthetic_data.plan_violations(payload, discrepancy_code=discrepancy):
            errors.append(f"{reason} ({rule} — plan only registry-backed work/parts, AGT-1)")
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
