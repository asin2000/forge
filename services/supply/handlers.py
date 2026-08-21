"""Supply Agent (AGT-2): locates approved parts, tracks shipment status.

It SHALL NOT approve substitutions or purchases: sourcing_report.v2 has no
substitution-approval field (additionalProperties: false), so a model output
attempting one fails contract validation — the boundary is enforced by the
contract, and the negative test proves it.
"""

from __future__ import annotations

from collections.abc import Callable

from forge_common.specialist import make_work_package_handler

ROLE = "supply"
PROMPT_FILE = "supply_sourcing_report.v1.md"
OUTPUT_SCHEMA = "sourcing_report.v2"


def make_handler(model: Callable[[str], str]):
    return make_work_package_handler(
        role=ROLE, model=model, prompt_file=PROMPT_FILE, output_schema_version=OUTPUT_SCHEMA
    )
