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
                    "inputs": payload,
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
        snapshot = layout.work_package_ref(db, workflow_id, work_package_id).get()
        wp = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if wp and (
            wp.get("owner_instance_id") != payload["agent_id"] or wp.get("status") == "COMPLETED"
        ):
            # Stale failure event: ownership already moved on, or the package
            # completed. Consume with NO writes — no block, no audit, no
            # output. The commit-time ownership guard covers the race window
            # (a reassignment plan that turns stale commits nothing either).
            return
        if role != "workforce":
            # Conditional disposition (entrant blocker 2): the package is
            # transactionally re-read at commit; if it completed or ownership
            # moved between this pre-read and the commit, the event is
            # consumed with ZERO effects — otherwise package+instance are
            # marked FAILED, the workflow blocks, and the escalation audits,
            # all in one transaction.
            error = (
                f"{payload['agent_id']} failed ({payload['failure_kind']}); "
                f"role {role} holds no reserve (ORC-4)"
            )
            writes.failure_disposition = {
                "work_package_id": work_package_id,
                "failed_instance_id": payload["agent_id"],
                "transition": {
                    "target": "BLOCKED_AGENT_FAILURE",
                    "agent_identity": ORCHESTRATOR_IDENTITY,
                    "trace_id": trace_id,
                    "reason_code": "SPECIALIST_FAILURE_NO_RESERVE",
                    "detail": error[:1000],
                },
                "audit_event": build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code="SPECIALIST_FAILURE_NO_RESERVE",
                    input_obj=payload,
                    output_obj={"error": error},
                    effective_at=read_clock(db),
                    work_package_id=work_package_id,
                ),
            }
            return
        reserve = registry.find_reserve_instance(db, "workforce")
        if reserve is None:
            # Same transactionally guarded disposition as other roles: if
            # Workforce completed between this pre-read and the commit, the
            # event is consumed with zero effects instead of blocking a
            # completed workflow.
            error = "workforce reserve is not available (ORC-3)"
            writes.failure_disposition = {
                "work_package_id": work_package_id,
                "failed_instance_id": payload["agent_id"],
                "transition": {
                    "target": "BLOCKED_AGENT_FAILURE",
                    "agent_identity": ORCHESTRATOR_IDENTITY,
                    "trace_id": trace_id,
                    "reason_code": "RESERVE_UNAVAILABLE",
                    "detail": error,
                },
                "audit_event": build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code="RESERVE_UNAVAILABLE",
                    input_obj=payload,
                    output_obj={"error": error},
                    effective_at=read_clock(db),
                    work_package_id=work_package_id,
                ),
            }
            return
        objective = (wp or {}).get("objective") or "Re-execute the failed work package."
        seq = int((wp or {}).get("assignment_seq", 1)) + 1
        reassignment = build_assignment(
            workflow_id=workflow_id,
            trace_id=trace_id,
            role="workforce",
            objective=objective,
            instance_id=reserve["instance_id"],
            inputs=(wp or {}).get("inputs", {}),  # gap 3: inputs preserved
            assignment_seq=seq,
        )
        reassignment["payload"]["reassigned_from"] = payload["agent_id"]
        validate_message(reassignment)
        audit_event = build_audit_event(
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
        # Bundled: audit + seq-2 assignment commit ONLY if the transfer
        # applies — a stale reassignment is a transactional no-op (gap 4).
        writes.reassignments.append(
            {
                "work_package_id": work_package_id,
                "failed_instance_id": payload["agent_id"],
                "reserve_instance_id": reserve["instance_id"],
                "audit_event": audit_event,
                "assignment_message": reassignment,
            }
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


def make_plan_handler(db: Any):
    """Handler for maintenance_action_plan.v2: create the Workforce
    assignment from the ACTUAL workflow (gap 2 — the repair scene is
    reachable from an NMC event, not a hand-built package).

    Extracts the plan's task codes, discovers the workforce capability via
    the registry, and commits the exclusive ownership claim + assignment
    atomically with the inbox marker. NoCapableAgent escalates + blocks.
    """

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        plan = message["payload"]
        workflow_id = envelope["workflow_id"]
        trace_id = envelope["trace_id"]
        task_codes = [task["task_code"] for task in plan.get("tasks", [])]
        inputs = {
            "task_codes": task_codes,
            "equipment_id": plan["equipment_id"],
            "plan_id": plan["plan_id"],
        }
        try:
            resolved = registry.discover(db, "technician-assignment")
        except registry.NoCapableAgent as exc:
            _make_escalation(
                db,
                writes,
                workflow_id=workflow_id,
                trace_id=trace_id,
                reason_code="NO_CAPABLE_AGENT",
                payload=plan,
                error=str(exc),
            )
            return
        instance_id = resolved["instance"]["instance_id"]
        objective = f"Staff maintenance plan {plan['plan_id']} ({', '.join(task_codes)})."
        assignment = build_assignment(
            workflow_id=workflow_id,
            trace_id=trace_id,
            role="workforce",
            objective=objective,
            instance_id=instance_id,
            inputs=inputs,
        )
        writes.outbox_messages.append(assignment)
        writes.work_package_claims.append(
            {
                "work_package_id": assignment["envelope"]["work_package_id"],
                "instance_id": instance_id,
                "role": "workforce",
                "objective": objective,
                "inputs": inputs,
            }
        )

    return handle


def _get_dict(ref: Any) -> dict[str, Any] | None:
    snapshot = ref.get()
    return snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot


def _latest_outbox_payload(db: Any, workflow_id: str, schema_version: str) -> dict[str, Any] | None:
    """Newest outbox message of a type for this workflow (evidence lookup)."""
    best: dict[str, Any] | None = None
    for snapshot in layout.workflow_ref(db, workflow_id).collection("outbox").stream():
        record = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        message = (record or {}).get("message", {})
        if message.get("envelope", {}).get("schema_version") != schema_version:
            continue
        if best is None or record.get("enqueued_at", "") >= best.get("enqueued_at", ""):
            best = record
    return best["message"] if best else None


RESUME_CONSTRAINT_PREFIX = "resume_on_day:"


def build_approval_request(
    *,
    workflow_id: str,
    trace_id: str,
    action_type: str,
    subject_event_id: str,
    recommended_action: str,
    source_refs: list[str],
    extracted_facts: list[str],
    applicable_rules: list[str],
    constraints: list[str],
    alternatives_considered: list[dict[str, str]],
) -> dict[str, Any]:
    """approval_request.v2 — the FULL HUM-2 decision record.

    The recommendation is a deterministic function of the trusted safety
    verdict (cited in source_refs), so confidence is 1.0 and versions name
    the policy, not a model — the model-produced evidence keeps its own
    provenance in the audit trail.
    """
    approval_id = (
        f"apr-{deterministic_event_id('apr', workflow_id, action_type, subject_event_id)}"[:44]
    )
    message = {
        "envelope": build_envelope(
            workflow_id=workflow_id,
            schema_version="approval_request.v2",
            event_id=deterministic_event_id("apr-req", workflow_id, action_type, subject_event_id),
            trace_id=trace_id,
            idempotency_key=f"idem-apr-{action_type}-{subject_event_id[:8]}",
        ),
        "payload": {
            "approval_id": approval_id,
            "action_type": action_type,
            "recommended_action": recommended_action[:500],
            "source_refs": source_refs[:20],
            "extracted_facts": [f[:500] for f in extracted_facts][:20],
            "applicable_rules": applicable_rules[:20],
            "constraints": [c[:500] for c in constraints][:20],
            "confidence": 1.0,
            "alternatives_considered": alternatives_considered[:10],
            "versions": {
                "agent_id": ORCHESTRATOR_IDENTITY,
                "model_id": "deterministic-policy",
                "prompt_version": "n/a",
                "schema_version": "approval_request.v2",
            },
        },
    }
    validate_message(message)
    return message


def make_verdict_handler(db: Any):
    """Handler for validation_verdict.v2: route a Safety verdict to the next
    spine step and, when the next step is a HUM-1 gate, compose the
    approval_request.v2 decision record IN THE SAME COMMIT as the transition
    into the awaiting state (HUM-2 — the human always has the evidence).

    - VALIDATING + approved  -> AWAITING_SCHEDULE_APPROVAL + schedule_override
      request (resume day = clock + sourcing ETA; no sourcing evidence in the
      workflow outbox means the override request would be unfounded ->
      escalation instead).
    - VALIDATING + vetoed    -> PLANNING (rework, audited with the reasons).
    - ASSEMBLY_RESUMED + approved -> AWAITING_RELEASE_APPROVAL +
      equipment_release request.
    - ASSEMBLY_RESUMED + vetoed   -> BLOCKED_AGENT_FAILURE (human attention).
    - Any other state: the verdict is stale — audited no-op, never a NACK
      loop. apply_transition re-validates the edge transactionally, so a
      state raced between the read here and the commit aborts safely and the
      redelivery re-reads.
    """

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        verdict = message["payload"]
        workflow_id = envelope["workflow_id"]
        trace_id = envelope["trace_id"]
        wf = _get_dict(layout.workflow_ref(db, workflow_id))
        status = (wf or {}).get("status")
        approved = verdict["verdict"] == "approved"

        def _audit_only(reason_code: str, note: str) -> None:
            writes.audit_events.append(
                build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code=reason_code,
                    input_obj=verdict,
                    output_obj={"note": note, "status": status},
                    effective_at=read_clock(db),
                )
            )

        if status == "VALIDATING" and approved:
            sourcing = _latest_outbox_payload(db, workflow_id, "sourcing_report.v3")
            if sourcing is None:
                _make_escalation(
                    db,
                    writes,
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    reason_code="NO_SOURCING_EVIDENCE",
                    payload=verdict,
                    error="schedule_override request requires a sourcing report ETA",
                )
                return
            report = sourcing["payload"]
            resume_day = read_clock(db) + int(report["eta_days"])
            request = build_approval_request(
                workflow_id=workflow_id,
                trace_id=trace_id,
                action_type="schedule_override",
                subject_event_id=verdict["subject_event_id"],
                recommended_action=(
                    f"Suspend {wf['equipment_id']} awaiting part {report['part_number']} "
                    f"(ETA {report['eta_days']} days); resume assembly on day {resume_day}."
                ),
                source_refs=[
                    f"validation_verdict:{envelope['event_id']}",
                    f"sourcing_report:{sourcing['envelope']['event_id']}",
                    f"workflow:{workflow_id}",
                ],
                extracted_facts=[
                    f"part {report['part_number']} approved={report['part_approved']} "
                    f"shipment={report['shipment_status']} eta_days={report['eta_days']}",
                    *verdict["reasons"],
                ],
                applicable_rules=verdict["rule_refs"],
                constraints=[
                    f"{RESUME_CONSTRAINT_PREFIX}{resume_day}",
                    "no substitution: Supply cannot approve substitutions or purchases (AGT-2)",
                ],
                alternatives_considered=[
                    {
                        "option": "reject the schedule override and return to planning",
                        "rejected_reason": "safety verdict approved the plan; the only "
                        "blocker is the part ETA reported from supply-chain data",
                    }
                ],
            )
            writes.outbox_messages.append(request)
            writes.transition = {
                "target": "AWAITING_SCHEDULE_APPROVAL",
                "agent_identity": ORCHESTRATOR_IDENTITY,
                "trace_id": trace_id,
                "reason_code": "VERDICT_APPROVED",
                "detail": bounded_json_detail({"approval_id": request["payload"]["approval_id"]}),
            }
        elif status == "VALIDATING" and not approved:
            writes.transition = {
                "target": "PLANNING",
                "agent_identity": ORCHESTRATOR_IDENTITY,
                "trace_id": trace_id,
                "reason_code": "VERDICT_VETOED",
                "detail": bounded_json_detail({"reasons": verdict["reasons"]}),
            }
        elif status == "ASSEMBLY_RESUMED" and approved:
            request = build_approval_request(
                workflow_id=workflow_id,
                trace_id=trace_id,
                action_type="equipment_release",
                subject_event_id=verdict["subject_event_id"],
                recommended_action=(
                    f"Release {wf['equipment_id']} to service: post-repair validation "
                    "verdict approved."
                ),
                source_refs=[
                    f"validation_verdict:{envelope['event_id']}",
                    f"workflow:{workflow_id}",
                ],
                extracted_facts=list(verdict["reasons"]),
                applicable_rules=verdict["rule_refs"],
                constraints=["release requires HUM-1 equipment_release approval"],
                alternatives_considered=[
                    {
                        "option": "return the equipment to rework",
                        "rejected_reason": "the safety verdict on the completed repair "
                        "is approved with no violations",
                    }
                ],
            )
            writes.outbox_messages.append(request)
            writes.transition = {
                "target": "AWAITING_RELEASE_APPROVAL",
                "agent_identity": ORCHESTRATOR_IDENTITY,
                "trace_id": trace_id,
                "reason_code": "VERDICT_APPROVED",
                "detail": bounded_json_detail({"approval_id": request["payload"]["approval_id"]}),
            }
        elif status == "ASSEMBLY_RESUMED":
            _make_escalation(
                db,
                writes,
                workflow_id=workflow_id,
                trace_id=trace_id,
                reason_code="RELEASE_VERDICT_VETOED",
                payload=verdict,
                error="; ".join(verdict["reasons"])[:1000],
            )
        else:
            _audit_only("VERDICT_STALE", "verdict arrived for a workflow not awaiting one")

    return handle


def make_decision_handler(db: Any):
    """Handler for approval_decision.v2 (the bus copy the approval surface
    enqueued atomically with the authoritative record): apply the human's
    decision to the spine.

    The gated transitions pass approval_id through apply_transition, which
    transactionally retrieves the RECORDED approval and consumes it once —
    a replayed or forged decision message cannot release equipment (HUM-1).
    A decision for a workflow no longer in the awaiting state is an audited
    no-op (never a NACK loop).
    """

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        decision = message["payload"]
        workflow_id = envelope["workflow_id"]
        trace_id = envelope["trace_id"]
        wf = _get_dict(layout.workflow_ref(db, workflow_id))
        status = (wf or {}).get("status")
        approved = decision["decision"] == "approved"
        action = decision["action_type"]

        def _audit_only(reason_code: str, note: str) -> None:
            writes.audit_events.append(
                build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code=reason_code,
                    input_obj=decision,
                    output_obj={"note": note, "status": status},
                    effective_at=read_clock(db),
                )
            )

        if action == "schedule_override" and status == "AWAITING_SCHEDULE_APPROVAL":
            if not approved:
                writes.transition = {
                    "target": "PLANNING",
                    "agent_identity": ORCHESTRATOR_IDENTITY,
                    "trace_id": trace_id,
                    "reason_code": "APPROVAL_REJECTED",
                    "detail": bounded_json_detail({"approval_id": decision["approval_id"]}),
                }
                return
            request = _latest_outbox_payload(db, workflow_id, "approval_request.v2")
            resume_day = None
            if request and request["payload"]["approval_id"] == decision["approval_id"]:
                for constraint in request["payload"]["constraints"]:
                    if constraint.startswith(RESUME_CONSTRAINT_PREFIX):
                        resume_day = int(constraint.removeprefix(RESUME_CONSTRAINT_PREFIX))
            if resume_day is None:
                _audit_only(
                    "DECISION_UNROUTABLE",
                    "approved schedule_override has no matching request with a resume day",
                )
                return
            writes.transition = {
                "target": "SUSPENDED_AWAITING_PART",
                "agent_identity": ORCHESTRATOR_IDENTITY,
                "trace_id": trace_id,
                "reason_code": "SCHEDULE_OVERRIDE_APPROVED",
                "approval_id": decision["approval_id"],
                "due_at": resume_day,
                "detail": bounded_json_detail({"resume_on_day": resume_day}),
            }
        elif action == "equipment_release" and status == "AWAITING_RELEASE_APPROVAL":
            if approved:
                writes.transition = {
                    "target": "RELEASED",
                    "agent_identity": ORCHESTRATOR_IDENTITY,
                    "trace_id": trace_id,
                    "reason_code": "RELEASE_APPROVED",
                    "approval_id": decision["approval_id"],
                    "detail": bounded_json_detail({"approver": decision["approver_identity"]}),
                }
            else:
                writes.transition = {
                    "target": "ASSEMBLY_RESUMED",
                    "agent_identity": ORCHESTRATOR_IDENTITY,
                    "trace_id": trace_id,
                    "reason_code": "APPROVAL_REJECTED",
                    "detail": bounded_json_detail({"approval_id": decision["approval_id"]}),
                }
        else:
            _audit_only("DECISION_STALE", f"{action} decision for workflow in {status}")

    return handle
