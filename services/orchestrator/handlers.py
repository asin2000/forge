"""Readiness Orchestrator: decompose + assign (ORC-1, ORC-2, REG-3).

ORC-2 by construction: this module binds NO domain tools — no procedures
library, no parts registry, no roster, no screening. Its model call produces
only role objectives (internal schema below), and its bus outputs are
orchestration artifacts: work_package_assignment messages, exclusive
ownership claims, and state transitions.
"""

from __future__ import annotations

from typing import Any

from jsonschema import Draft202012Validator

from forge_common import registry
from forge_common.agent_base import AgentOutputMalformed, constrained_json, load_prompt
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
    bus claim/lease — the model call is here, never in a transaction)."""

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        payload = message["payload"]
        workflow_id = envelope["workflow_id"]
        trace_id = envelope["trace_id"]
        try:
            decomposition = constrained_json(
                model,
                load_prompt("orchestrator_decompose.v1.md", payload),
                _DECOMPOSITION_VALIDATOR,
            )
            for role, capability in ROLE_CAPABILITIES.items():
                resolved = registry.discover(db, capability)
                instance_id = resolved["instance"]["instance_id"]
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
                    }
                )
            writes.transition = {
                "target": "PLANNING",
                "agent_identity": ORCHESTRATOR_IDENTITY,
                "trace_id": trace_id,
                "reason_code": "DECOMPOSED",
                "detail": bounded_json_detail(decomposition),
            }
        except registry.NoCapableAgent as exc:
            # REG-3/ORC-4 disposition: audited escalation + BLOCKED state.
            writes.transition = {
                "target": "BLOCKED_AGENT_FAILURE",
                "agent_identity": ORCHESTRATOR_IDENTITY,
                "trace_id": trace_id,
                "reason_code": "NO_CAPABLE_AGENT",
                "detail": str(exc)[:1000],
            }
            writes.audit_events.append(
                build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code="NO_CAPABLE_AGENT",
                    input_obj=payload,
                    output_obj={"error": str(exc)},
                    effective_at=read_clock(db),
                )
            )
        except AgentOutputMalformed:
            # Orchestrator reasoning failure: let the delivery NACK/redeliver
            # under the lease — transient model trouble is not a specialist
            # failure event (their role enum does not include orchestration).
            raise

    return handle
