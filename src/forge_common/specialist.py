"""Shared specialist consumption shape (AGT-1, AGT-2, AGT-7).

A specialist consumes ``work_package_assignment.v2`` addressed to its role,
runs its model through :class:`StructuredAgent` (under the bus claim/lease —
never in a transaction), and enqueues exactly one contract-valid message:
the domain output on success, or ``agent_failure_event.v2`` after retry
exhaustion. Specialists never request state transitions — releases,
substitution approvals, and schedule overrides are HUM-1-gated paths they
cannot reach (AGT-1/AGT-2 boundaries hold by construction).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from forge_common.agent_base import StructuredAgent
from forge_common.bus import TxnWrites


def make_work_package_handler(
    *,
    role: str,
    model: Callable[[str], str],
    prompt_file: str,
    output_schema_version: str,
):
    """Build a bus handler for one specialist role."""

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        payload = message["payload"]
        if payload["role"] != role:
            return  # addressed to another role; consumption is recorded, no output
        envelope = message["envelope"]
        agent = StructuredAgent(agent_id=payload["assigned_agent_id"], role=role, model=model)
        result = agent.run(
            prompt_file=prompt_file,
            variables={**payload.get("inputs", {}), "objective": payload["objective"]},
            schema_version=output_schema_version,
            workflow_id=envelope["workflow_id"],
            work_package_id=envelope["work_package_id"],
            trace_id=envelope["trace_id"],
        )
        writes.outbox_messages.append(result)

    return handle
