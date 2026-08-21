"""Maintenance Agent (AGT-1): produces maintenance action plans.

It SHALL NOT release equipment: this module requests no transitions, and the
RELEASED state is HUM-1-gated at the only state-write path — the boundary
holds by construction, and the negative test proves the gate refuses it.
"""

from __future__ import annotations

from collections.abc import Callable

from forge_common.specialist import make_work_package_handler

ROLE = "maintenance"
PROMPT_FILE = "maintenance_action_plan.v1.md"
OUTPUT_SCHEMA = "maintenance_action_plan.v2"


def make_handler(model: Callable[[str], str]):
    return make_work_package_handler(
        role=ROLE, model=model, prompt_file=PROMPT_FILE, output_schema_version=OUTPUT_SCHEMA
    )
