"""Readiness Orchestrator: decompose + assign (ORC-1, ORC-2, REG-3).

ORC-2 by construction: this module binds NO domain tools — no procedures
library, no parts registry, no roster, no screening. Its ADK model call
produces only role objectives (internal schema below), and its bus outputs
are orchestration artifacts: work_package_assignment messages, exclusive
ownership claims, and state transitions.

Failure dispositions are bounded and audited:
- ``NoCapableAgent`` during discovery → escalation audit + BLOCKED, with
  ZERO assignments (all required capabilities are resolved BEFORE any write
  accumulates — a partial assignment can never commit).
- Reasoning exhaustion (``AgentOutputMalformed`` after AGT-7's retry limit)
  → escalation audit + BLOCKED. The BLOCKED → PLANNING edge remains the
  recovery path; no unbounded NACK loop.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from forge_common import layout, registry
from forge_common.agent_base import (
    AdkTextRunner,
    AgentOutputMalformed,
    constrained_json,
    load_prompt,
)
from forge_common.audit import bounded_json_detail, build_audit_event
from forge_common.bus import TxnWrites
from forge_common.clock import read_clock
from forge_common.contracts import validate_message
from forge_common.messages import build_envelope, deterministic_event_id

ORCHESTRATOR_IDENTITY = "forge-orchestrator"

#: role -> registry capability required to execute it (REG-3 discovery).
ROLE_CAPABILITIES = {"maintenance": "maintenance-planning", "supply": "parts-sourcing"}

_DECOMPOSITION_VALIDATOR = Draft202012Validator(
    {
        "type": "object",
        "additionalProperties": False,
        "required": ["objectives"],
        "properties": {
            "objectives": {
                "type": "object",
                "additionalProperties": False,
                "required": list(ROLE_CAPABILITIES),
                "properties": {
                    role: {"type": "string", "minLength": 5, "maxLength": 500}
                    for role in ROLE_CAPABILITIES
                },
            }
        },
    }
)


def build_assignment(
    *,
    workflow_id: str,
    trace_id: str,
    role: str,
    objective: str,
    instance_id: str,
    inputs: dict[str, Any],
    assignment_seq: int = 1,
) -> dict[str, Any]:
    """One contract-valid work_package_assignment.v2 (ORC-1)."""
    work_package_id = f"wp-{role}-{workflow_id.removeprefix('wf-')}"
    event_id = deterministic_event_id("assign", workflow_id, work_package_id, str(assignment_seq))
    message = {
        "envelope": build_envelope(
            workflow_id=workflow_id,
            work_package_id=work_package_id,
            schema_version="work_package_assignment.v2",
            event_id=event_id,
            trace_id=trace_id,
            idempotency_key=f"idem-assign-{work_package_id}-{assignment_seq:02d}",
        ),
        "payload": {
            "role": role,
            "objective": objective,
            "assigned_agent_id": instance_id,
            "assignment_seq": assignment_seq,
            "inputs": inputs,
        },
    }
    validate_message(message)
    return message


def make_nmc_handler(db: Any, *, model: Any):
    """Handler for nmc_event.v2: decompose, discover, assign (runs under the
    bus claim/lease — the ADK call is here, never in a transaction)."""
    runner = AdkTextRunner(name="forge_orchestrator", model=model)

    def _block(
        writes: TxnWrites,
        *,
        workflow_id: str,
        trace_id: str,
        reason_code: str,
        payload: dict[str, Any],
        error: str,
    ) -> None:
        writes.transition = {
            "target": "BLOCKED_AGENT_FAILURE",
            "agent_identity": ORCHESTRATOR_IDENTITY,
            "trace_id": trace_id,
            "reason_code": reason_code,
            "detail": error[:1000],
        }
        writes.audit_events.append(
            build_audit_event(
                workflow_id=workflow_id,
                trace_id=trace_id,
                agent_identity=ORCHESTRATOR_IDENTITY,
                event_kind="escalation",
                reason_code=reason_code,
                input_obj=payload,
                output_obj={"error": error},
                effective_at=read_clock(db),
            )
        )

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        payload = message["payload"]
        workflow_id = envelope["workflow_id"]
        trace_id = envelope["trace_id"]
        try:
            decomposition = constrained_json(
                runner,
                load_prompt("orchestrator_decompose.v1.md", payload),
                _DECOMPOSITION_VALIDATOR,
            )
            # Phase 1 — resolve EVERY required capability before a single
            # write accumulates: partial assignments must never commit.
            resolved = {
                role: registry.discover(db, capability)
                for role, capability in ROLE_CAPABILITIES.items()
            }
        except registry.NoCapableAgent as exc:
            _block(
                writes,
                workflow_id=workflow_id,
                trace_id=trace_id,
                reason_code="NO_CAPABLE_AGENT",
                payload=payload,
                error=str(exc),
            )
            return
        except AgentOutputMalformed as exc:
            # Bounded terminal disposition after AGT-7's retry limit — not a
            # silent NACK loop (entrant review, Day 3 gap 5).
            _block(
                writes,
                workflow_id=workflow_id,
                trace_id=trace_id,
                reason_code="ORCHESTRATOR_REASONING_EXHAUSTED",
                payload=payload,
                error=f"attempts={exc.attempts}: {exc.detail}",
            )
            return

        # Phase 2 — all roles resolvable: accumulate the full effect plan.
        for role in ROLE_CAPABILITIES:
            instance_id = resolved[role]["instance"]["instance_id"]
            assignment = build_assignment(
                workflow_id=workflow_id,
                trace_id=trace_id,
                role=role,
                objective=decomposition["objectives"][role],
                instance_id=instance_id,
                inputs=payload,
            )
            writes.outbox_messages.append(assignment)
            writes.work_package_claims.append(
                {
                    "work_package_id": assignment["envelope"]["work_package_id"],
                    "instance_id": instance_id,
                    "role": role,
                    "objective": decomposition["objectives"][role],
                }
            )
        writes.transition = {
            "target": "PLANNING",
            "agent_identity": ORCHESTRATOR_IDENTITY,
            "trace_id": trace_id,
            "reason_code": "DECOMPOSED",
            "detail": bounded_json_detail(decomposition),
        }

    return handle


def make_failure_handler(db: Any):
    """Handler for agent_failure_event.v2 (ORC-3/ORC-4 dispositions).

    Workforce failures reassign to the held reserve: ownership transfer,
    instance state flips, the reassignment audit event, and the seq-2
    assignment all commit in ONE transaction (ORC-3, atomically, exactly
    once — a double-fired failure event is skipped by the ownership guard).
    Failures of any other role place the workflow in BLOCKED_AGENT_FAILURE
    with an audited escalation (ORC-4).
    """

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        payload = message["payload"]
        workflow_id = envelope["workflow_id"]
        trace_id = envelope["trace_id"]
        role = payload["role"]
        work_package_id = envelope["work_package_id"]
        if role != "workforce":
            _make_escalation(
                db,
                writes,
                workflow_id=workflow_id,
                trace_id=trace_id,
                reason_code="SPECIALIST_FAILURE_NO_RESERVE",
                payload=payload,
                error=(
                    f"{payload['agent_id']} failed ({payload['failure_kind']}); "
                    f"role {role} holds no reserve (ORC-4)"
                ),
            )
            return
        reserve = registry.find_reserve_instance(db, "workforce")
        if reserve is None:
            _make_escalation(
                db,
                writes,
                workflow_id=workflow_id,
                trace_id=trace_id,
                reason_code="RESERVE_UNAVAILABLE",
                payload=payload,
                error="workforce reserve is not available (ORC-3)",
            )
            return
        snapshot = layout.work_package_ref(db, workflow_id, work_package_id).get()
        wp = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        objective = (wp or {}).get("objective") or "Re-execute the failed work package."
        seq = int((wp or {}).get("assignment_seq", 1)) + 1
        writes.reassignments.append(
            {
                "work_package_id": work_package_id,
                "failed_instance_id": payload["agent_id"],
                "reserve_instance_id": reserve["instance_id"],
            }
        )
        reassignment = build_assignment(
            workflow_id=workflow_id,
            trace_id=trace_id,
            role="workforce",
            objective=objective,
            instance_id=reserve["instance_id"],
            inputs=(wp or {}).get("inputs", {}),
            assignment_seq=seq,
        )
        reassignment["payload"]["reassigned_from"] = payload["agent_id"]
        validate_message(reassignment)
        writes.outbox_messages.append(reassignment)
        writes.audit_events.append(
            build_audit_event(
                workflow_id=workflow_id,
                trace_id=trace_id,
                agent_identity=ORCHESTRATOR_IDENTITY,
                event_kind="reassignment",
                reason_code="WORKFORCE_RESERVE_DEPLOYED",
                input_obj=payload,
                output_obj={
                    "work_package_id": work_package_id,
                    "from": payload["agent_id"],
                    "to": reserve["instance_id"],
                    "assignment_seq": seq,
                },
                effective_at=read_clock(db),
                work_package_id=work_package_id,
            )
        )

    return handle


def _make_escalation(
    db: Any,
    writes: TxnWrites,
    *,
    workflow_id: str,
    trace_id: str,
    reason_code: str,
    payload: dict[str, Any],
    error: str,
) -> None:
    writes.transition = {
        "target": "BLOCKED_AGENT_FAILURE",
        "agent_identity": ORCHESTRATOR_IDENTITY,
        "trace_id": trace_id,
        "reason_code": reason_code,
        "detail": error[:1000],
    }
    writes.audit_events.append(
        build_audit_event(
            workflow_id=workflow_id,
            trace_id=trace_id,
            agent_identity=ORCHESTRATOR_IDENTITY,
            event_kind="escalation",
            reason_code=reason_code,
            input_obj=payload,
            output_obj={"error": error},
            effective_at=read_clock(db),
        )
    )
