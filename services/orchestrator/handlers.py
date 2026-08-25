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
from forge_common.state import TERMINAL_STATES

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
        wf = _get_dict(layout.workflow_ref(db, workflow_id))
        if not wf or wf.get("status") != "INTAKE":
            # Late or replayed trigger — e.g. the workflow was cancelled
            # (HUM-3) while this event was in flight: audited stale no-op
            # BEFORE the model call, never an illegal-edge 500 into the DLQ.
            writes.audit_events.append(
                build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code="NMC_EVENT_STALE",
                    input_obj=payload,
                    output_obj={"status": (wf or {}).get("status")},
                    effective_at=read_clock(db),
                )
            )
            return
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
                    "effective_at": read_clock(db),
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
        wf = _get_dict(layout.workflow_ref(db, workflow_id))
        if (wf or {}).get("status") in TERMINAL_STATES:
            # Finished workflow (HUM-3 cancel or release): consume with NO
            # writes, mirroring the stale-ownership semantics below. The
            # monitor also skips terminal workflows; this covers the
            # in-flight race window.
            return
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
        wf_now = _get_dict(layout.workflow_ref(db, workflow_id))
        if (wf_now or {}).get("status") in TERMINAL_STATES:
            # A plan landing after cancel/release must not claim packages or
            # publish assignments into a finished workflow (HUM-3): audited
            # stale no-op with ZERO side effects.
            writes.audit_events.append(
                build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code="PLAN_STALE",
                    input_obj={"plan_id": plan["plan_id"]},
                    output_obj={"status": (wf_now or {}).get("status")},
                    effective_at=read_clock(db),
                )
            )
            return
        task_codes = [task["task_code"] for task in plan.get("tasks", [])]
        inputs = {
            "task_codes": task_codes,
            "equipment_id": plan["equipment_id"],
            "plan_id": plan["plan_id"],
        }
        standing = _get_dict(
            layout.work_package_ref(
                db, workflow_id, f"wp-workforce-{workflow_id.removeprefix('wf-')}"
            )
        )
        if standing is not None:
            # REPLAN (veto -> PLANNING -> revised plan): the staffing package
            # already exists and stays with its owner — re-claiming would
            # collide and NACK-loop into the DLQ. Replans REVALIDATE (Safety
            # sees the revised plan in VALIDATING); staffing changes belong
            # to the ORC-3 reassignment machinery, never to a replan.
            wf = _get_dict(layout.workflow_ref(db, workflow_id))
            if (wf or {}).get("status") == "PLANNING":
                writes.transition = {
                    "target": "VALIDATING",
                    "agent_identity": ORCHESTRATOR_IDENTITY,
                    "trace_id": trace_id,
                    "reason_code": "PLAN_REVISED",
                    "detail": bounded_json_detail({"plan_id": plan["plan_id"]}),
                }
            else:
                # A revised plan racing its own veto (workflow still
                # VALIDATING) is consumed but cannot transition. AUDITED so
                # the trail shows the held revision. Today no automated
                # replan producer exists (humans resubmit under a fresh
                # event id); before automating replans, an in-band
                # re-trigger must land here — see docs/decisions.md.
                writes.audit_events.append(
                    build_audit_event(
                        workflow_id=workflow_id,
                        trace_id=trace_id,
                        agent_identity=ORCHESTRATOR_IDENTITY,
                        event_kind="escalation",
                        reason_code="PLAN_REVISION_HELD",
                        input_obj={"plan_id": plan["plan_id"]},
                        output_obj={"status": (wf or {}).get("status")},
                        effective_at=read_clock(db),
                    )
                )
            return
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
                "effective_at": read_clock(db),
            }
        )
        wf = _get_dict(layout.workflow_ref(db, workflow_id))
        if (wf or {}).get("status") == "PLANNING":
            # the plan is in hand: Safety validates it in VALIDATING and the
            # verdict handler routes onward. apply_transition re-validates
            # the edge transactionally; a raced state aborts and redelivery
            # re-reads (never a NACK loop on an illegal edge).
            writes.transition = {
                "target": "VALIDATING",
                "agent_identity": ORCHESTRATOR_IDENTITY,
                "trace_id": trace_id,
                "reason_code": "PLAN_RECEIVED",
                "detail": bounded_json_detail({"plan_id": plan["plan_id"]}),
            }

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


def _subject_schema(db: Any, workflow_id: str, subject_event_id: str) -> str | None:
    """Schema of the outbox message a verdict passes judgment on — verdicts
    route by WHAT they validated (plan/sourcing -> schedule gate evidence;
    roster -> release/completion evidence), never by arrival timing."""
    snapshot = layout.outbox_ref(db, workflow_id, subject_event_id).get()
    record = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
    if not record:
        return None
    return record["message"]["envelope"]["schema_version"]


def _latest_approved_verdict(
    db: Any, workflow_id: str, *, subject_schemas: tuple[str, ...]
) -> dict[str, Any] | None:
    """Newest approved validation_verdict whose SUBJECT is one of the given
    schemas (evidence lookup across the workflow outbox)."""
    best: dict[str, Any] | None = None
    best_key = ""
    for snapshot in layout.workflow_ref(db, workflow_id).collection("outbox").stream():
        record = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        message = (record or {}).get("message", {})
        if message.get("envelope", {}).get("schema_version") != "validation_verdict.v2":
            continue
        if message["payload"]["verdict"] != "approved":
            continue
        subject = _subject_schema(db, workflow_id, message["payload"]["subject_event_id"])
        if subject not in subject_schemas:
            continue
        if record.get("enqueued_at", "") >= best_key:
            best, best_key = message, record.get("enqueued_at", "")
    return best


def _open_schedule_gate(
    db: Any,
    writes: TxnWrites,
    *,
    wf_doc: dict[str, Any],
    workflow_id: str,
    trace_id: str,
    verdict_msg: dict[str, Any],
    sourcing_msg: dict[str, Any],
) -> None:
    """VALIDATING -> AWAITING_SCHEDULE_APPROVAL with the HUM-2 decision
    record composed in the SAME commit (verdict + sourcing ETA evidence)."""
    verdict = verdict_msg["payload"]
    report = sourcing_msg["payload"]
    resume_day = read_clock(db) + int(report["eta_days"])
    request = build_approval_request(
        workflow_id=workflow_id,
        trace_id=trace_id,
        action_type="schedule_override",
        subject_event_id=verdict["subject_event_id"],
        recommended_action=(
            f"Suspend {wf_doc['equipment_id']} awaiting part {report['part_number']} "
            f"(ETA {report['eta_days']} days); resume assembly on day {resume_day}."
        ),
        source_refs=[
            f"validation_verdict:{verdict_msg['envelope']['event_id']}",
            f"sourcing_report:{sourcing_msg['envelope']['event_id']}",
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


def make_verdict_handler(db: Any):
    """Handler for validation_verdict.v2: route a Safety verdict to the next
    spine step BY ITS SUBJECT — live push delivery makes arrival order
    arbitrary, so routing never depends on which message lands first.

    - VALIDATING, approved verdict on a PLAN or SOURCING report, sourcing
      evidence present -> AWAITING_SCHEDULE_APPROVAL + schedule_override
      request (HUM-2 record in the SAME commit). Sourcing evidence not yet
      arrived -> audited HOLD (the sourcing-report handler opens the gate
      when the report lands; whichever arrives LAST opens it).
    - VALIDATING, vetoed -> PLANNING (rework loop; replans revalidate).
    - approved verdict on a ROSTER while still pre-resume: recorded no-op —
      completion evidence is consumed at resume (the due handler re-keys
      it), because a workflow cannot seek release before its part arrives.
    - ASSEMBLY_RESUMED, approved verdict on a ROSTER ->
      AWAITING_RELEASE_APPROVAL + equipment_release request. A plan or
      sourcing verdict can NEVER open the release gate.
    - ASSEMBLY_RESUMED, vetoed -> BLOCKED_AGENT_FAILURE (human attention).
    - anything else: audited stale no-op, never a NACK loop.
    """

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        verdict = message["payload"]
        workflow_id = envelope["workflow_id"]
        trace_id = envelope["trace_id"]
        wf = _get_dict(layout.workflow_ref(db, workflow_id))
        status = (wf or {}).get("status")
        approved = verdict["verdict"] == "approved"
        subject = _subject_schema(db, workflow_id, verdict["subject_event_id"])

        def _audit_only(reason_code: str, note: str) -> None:
            writes.audit_events.append(
                build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code=reason_code,
                    input_obj=verdict,
                    output_obj={"note": note, "status": status, "subject_schema": subject},
                    effective_at=read_clock(db),
                )
            )

        if status in TERMINAL_STATES:
            # Cancelled/released workflows accept no verdicts — not even the
            # roster branch below may touch completion evidence (HUM-3).
            _audit_only("VERDICT_STALE", "verdict arrived for a finished workflow")
            return

        schedule_subjects = ("maintenance_action_plan.v2", "sourcing_report.v3")
        if status == "VALIDATING" and approved and subject in schedule_subjects:
            sourcing = _latest_outbox_payload(db, workflow_id, "sourcing_report.v3")
            if sourcing is None:
                _audit_only(
                    "VERDICT_HELD_AWAITING_EVIDENCE",
                    "approved verdict held; the sourcing-report handler opens "
                    "the gate when the ETA evidence lands",
                )
                return
            _open_schedule_gate(
                db,
                writes,
                wf_doc=wf,
                workflow_id=workflow_id,
                trace_id=trace_id,
                verdict_msg=message,
                sourcing_msg=sourcing,
            )
        elif subject == "roster_assignment.v2" and status != "ASSEMBLY_RESUMED":
            # completion evidence (approved OR vetoed) arriving pre-resume:
            # recorded via the transactional marker — the committing txn
            # re-reads the workflow doc and re-keys straight onto the bus if
            # the resume raced in (lost-wakeup closure). A vetoed roster is
            # NOT a plan veto: it re-keys at resume and blocks there.
            _audit_only(
                "ROSTER_VERDICT_RECORDED",
                "completion evidence recorded; re-keyed at resume",
            )
            writes.completion_evidence = {"message": message}
        elif status == "VALIDATING" and not approved and subject in schedule_subjects:
            writes.transition = {
                "target": "PLANNING",
                "agent_identity": ORCHESTRATOR_IDENTITY,
                "trace_id": trace_id,
                "reason_code": "VERDICT_VETOED",
                "detail": bounded_json_detail({"reasons": verdict["reasons"]}),
            }
        elif status == "ASSEMBLY_RESUMED" and approved and subject == "roster_assignment.v2":
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
        elif status == "ASSEMBLY_RESUMED" and not approved and subject == "roster_assignment.v2":
            # only vetoed COMPLETION evidence blocks the release path — a
            # late vetoed plan/sourcing verdict (live delivery timing) is
            # stale context, not grounds to block (observed live: run 1
            # blocked on a post-resume sourcing veto that run 2 shrugged off
            # as stale purely because it arrived earlier)
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


def make_sourcing_report_handler(db: Any):
    """Handler for sourcing_report.v3 at the orchestrator: the report is the
    schedule gate's ETA EVIDENCE. Live delivery order is arbitrary — if an
    approved plan/sourcing verdict is already HELD, the report's arrival
    opens the gate (whichever lands last opens it); otherwise the report
    simply waits in the outbox for the verdict handler. No-ops are silent:
    a report is evidence, not a state trigger by itself."""

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        workflow_id = envelope["workflow_id"]
        wf = _get_dict(layout.workflow_ref(db, workflow_id))
        if (wf or {}).get("status") != "VALIDATING":
            return
        held = _latest_approved_verdict(
            db, workflow_id, subject_schemas=("maintenance_action_plan.v2", "sourcing_report.v3")
        )
        if held is None:
            return
        _open_schedule_gate(
            db,
            writes,
            wf_doc=wf,
            workflow_id=workflow_id,
            trace_id=envelope["trace_id"],
            verdict_msg=held,
            sourcing_msg=message,
        )

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


def make_due_handler(db: Any):
    """Handler for due_event.v2 (Logical Clock, ORC-5): the part's ETA day
    has arrived — resume assembly. SUSPENDED_AWAITING_PART -> ASSEMBLY_RESUMED
    is the only edge; a due event for a workflow in any other state (already
    resumed, blocked, released) is an audited no-op, never a NACK loop. The
    clock's transactional recheck prevents early emission; this handler's
    apply_transition re-validates the edge at commit."""

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        workflow_id = envelope["workflow_id"]
        trace_id = envelope["trace_id"]
        wf = _get_dict(layout.workflow_ref(db, workflow_id))
        status = (wf or {}).get("status")
        if status != "SUSPENDED_AWAITING_PART":
            writes.audit_events.append(
                build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=trace_id,
                    agent_identity=ORCHESTRATOR_IDENTITY,
                    event_kind="escalation",
                    reason_code="DUE_EVENT_STALE",
                    input_obj=message["payload"],
                    output_obj={"status": status},
                    effective_at=read_clock(db),
                )
            )
            return
        writes.transition = {
            "target": "ASSEMBLY_RESUMED",
            "agent_identity": ORCHESTRATOR_IDENTITY,
            "trace_id": trace_id,
            "reason_code": "PART_ETA_REACHED",
            "detail": bounded_json_detail({"due_at_logical": message["payload"]["due_at_logical"]}),
        }
        # Completion evidence that arrived pre-resume is re-keyed onto the
        # bus IN THE COMMITTING TRANSACTION (bus reads the marker doc after
        # claiming, before any write): whichever of {evidence, resume}
        # commits second sees the other's durable record, so the release
        # gate can never lose the wakeup. The epoch (due day) is folded into
        # the re-key id — replay- and second-suspension-safe.
        writes.resume_rekey = {"due_at": message["payload"]["due_at_logical"]}

    return handle
