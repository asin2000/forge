"""Shared specialist consumption shape (AGT-1, AGT-2, AGT-7, AUD-1).

A specialist consumes ``work_package_assignment.v2`` addressed to its role,
runs its model through ADK via :class:`StructuredAgent` (under the bus
claim/lease — never in a transaction), and enqueues exactly one
contract-valid message: the domain output on success, or
``agent_failure_event.v2`` after retry exhaustion. Every run also emits an
AUD-1 audit event atomically with its outbox message: ``decision``
(DOMAIN_OUTPUT_PRODUCED) on success, ``failure`` (SPECIALIST_MALFORMED) on
exhaustion. Specialists never request state transitions — releases,
substitution approvals, and schedule overrides are HUM-1-gated paths they
cannot reach (AGT-1/AGT-2 boundaries hold by construction).
"""

from __future__ import annotations

from typing import Any

from forge_common.agent_base import StructuredAgent
from forge_common.audit import build_audit_event
from forge_common.bus import TxnWrites
from forge_common.clock import read_clock


def make_work_package_handler(
    db: Any,
    *,
    role: str,
    model: Any,
    prompt_file: str,
    output_schema_version: str,
):
    """Build a bus handler for one specialist role."""

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        payload = message["payload"]
        if payload["role"] != role:
            return  # addressed to another role; consumption is recorded, no output
        envelope = message["envelope"]
        agent_id = payload["assigned_agent_id"]
        agent = StructuredAgent(agent_id=agent_id, role=role, model=model)
        result = agent.run(
            prompt_file=prompt_file,
            variables={**payload.get("inputs", {}), "objective": payload["objective"]},
            schema_version=output_schema_version,
            workflow_id=envelope["workflow_id"],
            work_package_id=envelope["work_package_id"],
            trace_id=envelope["trace_id"],
        )
        writes.outbox_messages.append(result)
        failed = result["envelope"]["schema_version"] == "agent_failure_event.v2"
        writes.audit_events.append(
            build_audit_event(
                workflow_id=envelope["workflow_id"],
                trace_id=envelope["trace_id"],
                agent_identity=agent_id,
                event_kind="failure" if failed else "decision",
                reason_code="SPECIALIST_MALFORMED" if failed else "DOMAIN_OUTPUT_PRODUCED",
                input_obj=payload,
                output_obj=result["payload"],
                effective_at=read_clock(db),
                work_package_id=envelope["work_package_id"],
            )
        )

    return handle
