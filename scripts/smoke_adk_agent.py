#!/usr/bin/env python3
"""Live ADK smoke (PLT-1/PLT-2): one StructuredAgent round-trip through
Google ADK's LlmAgent + Runner against live gemini-3.5-flash on Vertex.

Usage: PROJECT_ID=<id> python scripts/smoke_adk_agent.py
"""

import json
import os
import sys

os.environ.setdefault("GOOGLE_GENAI_USE_VERTEXAI", "true")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", os.environ.get("PROJECT_ID", ""))
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us")  # DAT-1 map: gemini at `us`

from forge_common.agent_base import StructuredAgent  # noqa: E402
from forge_common.messages import deterministic_trace_id  # noqa: E402

WF = "wf-smoke-adk-0001"

agent = StructuredAgent(agent_id="agent-supply-01", role="supply", model="gemini-3.5-flash")
message = agent.run(
    prompt_file="supply_sourcing_report.v1.md",
    variables={
        "objective": "Source the approved hydraulic actuator HYD-ACT-4402.",
        "equipment_id": "GX12-07",
        "discrepancy_code": "DSC-0042",
        "description": "Failed hydraulic actuator (synthetic smoke test)",
    },
    schema_version="sourcing_report.v2",
    workflow_id=WF,
    work_package_id="wp-supply-smoke-0001",
    trace_id=deterministic_trace_id(WF),
)
kind = message["envelope"]["schema_version"]
print(json.dumps(message["payload"], indent=2)[:600])
if kind != "sourcing_report.v2":
    print(f"ADK SMOKE: FAIL ({kind})")
    sys.exit(1)
print("ADK SMOKE: PASS — contract-valid sourcing_report.v2 via ADK Runner on Vertex")
