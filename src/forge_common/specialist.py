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

from forge_common import layout
from forge_common.agent_base import StructuredAgent
from forge_common.audit import build_audit_event
from forge_common.bus import TxnWrites
from forge_common.clock import read_clock
from forge_common.state import TERMINAL_STATES


def make_work_package_handler(
    db: Any,
    *,
    role: str,
    model: Any,
    prompt_file: str,
    output_schema_version: str,
    payload_check_factory: Any = None,
    variables_factory: Any = None,
):
    """Build a bus handler for one specialist role.

    ``payload_check_factory(assignment_payload)`` returns the data-backed
    check applied to the model's output; ``variables_factory`` may add
    grounding context (e.g. an approved-parts excerpt) to the prompt.
    """

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        payload = message["payload"]
        if payload["role"] != role:
            return  # addressed to another role; consumption is recorded, no output
        envelope = message["envelope"]
        agent_id = payload["assigned_agent_id"]
        wf_snapshot = layout.workflow_ref(db, envelope["workflow_id"]).get()
        wf = wf_snapshot.to_dict() if hasattr(wf_snapshot, "to_dict") else wf_snapshot
        if (wf or {}).get("status") in TERMINAL_STATES:
            # The workflow finished (HUM-3 cancel or release) while this
            # assignment was in flight: audited stale no-op BEFORE the model
            # call — no domain output for a finished workflow.
            writes.audit_events.append(
                build_audit_event(
                    workflow_id=envelope["workflow_id"],
                    trace_id=envelope["trace_id"],
                    agent_identity=agent_id,
                    event_kind="escalation",
                    reason_code="ASSIGNMENT_STALE",
                    input_obj={"work_package_id": envelope["work_package_id"]},
                    output_obj={"status": (wf or {}).get("status")},
                    effective_at=read_clock(db),
                    work_package_id=envelope["work_package_id"],
                )
            )
            return
        agent = StructuredAgent(agent_id=agent_id, role=role, model=model)
        variables = {**payload.get("inputs", {}), "objective": payload["objective"]}
        if variables_factory is not None:
            variables.update(variables_factory(payload))
        result = agent.run(
            prompt_file=prompt_file,
            variables=variables,
            schema_version=output_schema_version,
            workflow_id=envelope["workflow_id"],
            work_package_id=envelope["work_package_id"],
            trace_id=envelope["trace_id"],
            source_event_id=envelope["event_id"],
            payload_check=(
                payload_check_factory(payload) if payload_check_factory is not None else None
            ),
        )
        failed = result["envelope"]["schema_version"] == "agent_failure_event.v2"
        # EVERYTHING package-scoped — domain output, audit, status — rides
        # ONE transactional ownership guard (owned_effects): if this worker
        # was reassigned away before commit, none of it lands, so a stale
        # primary can never publish a competing result (entrant blocker 1).
        writes.owned_effects = {
            "work_package_id": envelope["work_package_id"],
            "expected_owner": agent_id,
            "status": "FAILED_PENDING_REPAIR" if failed else "COMPLETED",
            "outbox_messages": [result],
            "audit_events": [],
        }
        writes.owned_effects["audit_events"].append(
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
