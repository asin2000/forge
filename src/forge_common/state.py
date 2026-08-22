"""Workflow state machine with transactional, audited transitions (ORC-5,
HUM-1, AUD-2, AUD-3).

The transition table encodes the demo spine (Section 8). Two transitions are
human-gated (HUM-1): entering ``SUSPENDED_AWAITING_PART`` (the 21-day schedule
override) and entering ``RELEASED`` (equipment release). A gated transition
without an approved ``approval_decision`` raises :class:`GateBlocked` — the
CI-4 assertion "blocked before approval, succeeds after". Each approval is
consumed transactionally (a marker keyed by ``approval_id``), so a replayed
approval cannot authorize a second gated transition, and the approval's
``approver_identity``/``action_type``/``decision``/``decided_at`` are recorded
verbatim in the audit event's ``detail`` so HUM-1 evidence reconstructs from
Firestore alone (AUD-2).

``apply_transition`` is the ONLY legal state-write path: it runs inside a
caller-supplied transaction so bus handlers get gated, audited transitions
atomically coupled with their inbox marker (ICD-5 + AUD-2). Every transition
reads ``system/logical_clock`` in the same transaction and stamps the current
Logical Clock day into both the state doc and the audit ``effective_at``, so
reconstruction across the time skip is unambiguous (AUD-3).
"""

from __future__ import annotations

from typing import Any

from forge_common import layout
from forge_common.audit import bounded_json_detail, build_audit_event, now_iso
from forge_common.contracts import validate_message

# Spine states (workflow_state.v1 enum). AWAITING_RELEASE_APPROVAL ->
# ASSEMBLY_RESUMED is the rejected-release rework path (HUM-1 allows reject).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "INTAKE": {"PLANNING", "BLOCKED_AGENT_FAILURE"},
    "PLANNING": {"VALIDATING", "BLOCKED_AGENT_FAILURE"},
    "VALIDATING": {"AWAITING_SCHEDULE_APPROVAL", "PLANNING", "BLOCKED_AGENT_FAILURE"},
    "AWAITING_SCHEDULE_APPROVAL": {"SUSPENDED_AWAITING_PART", "PLANNING"},
    "SUSPENDED_AWAITING_PART": {"ASSEMBLY_RESUMED"},
    "ASSEMBLY_RESUMED": {"AWAITING_RELEASE_APPROVAL", "BLOCKED_AGENT_FAILURE"},
    "AWAITING_RELEASE_APPROVAL": {"RELEASED", "ASSEMBLY_RESUMED"},
    "RELEASED": set(),
    "BLOCKED_AGENT_FAILURE": {"PLANNING"},
}

#: target state -> required HUM-1 action_type
GATED_TARGETS: dict[str, str] = {
    "SUSPENDED_AWAITING_PART": "schedule_override",
    "RELEASED": "equipment_release",
}


class InvalidTransition(Exception):
    """Transition not allowed by the table or its due_at rules."""


class GateBlocked(Exception):
    """HUM-1: gated transition without a valid, unconsumed approved decision."""


def record_approval_decision(
    db: Any, message: dict[str, Any], *, enqueue_outbox: bool = False
) -> str:
    """Persist an authoritative approval_decision.v2 record (HUM-1).

    Contract-validates the FULL message, then stores it under
    ``workflows/{id}/approvals/{approval_id}``. In production this is called
    only by the approval surface behind Cloud Run IAM/IAP, where
    ``approver_identity`` derives from the authenticated principal — the
    state layer then proves "a recorded approval exists", and authentication
    is enforced at the only writer. Returns the approval_id.
    """
    if validate_message(message) != "approval_decision.v2":
        raise GateBlocked("only approval_decision.v2 may be recorded (HUM-1)")
    if message["envelope"]["trust_state"] != "TRUSTED":
        raise GateBlocked("approval records must carry trust_state TRUSTED (DAT-2)")
    payload = message["payload"]
    workflow_id = message["envelope"]["workflow_id"]

    def _record(txn: Any) -> None:
        # Leading read: see create_workflow — write-only transactions never
        # begin on the real client. Also an explicit duplicate check.
        if layout.txn_get_dict(txn, layout.approval_ref(db, workflow_id, payload["approval_id"])):
            raise GateBlocked(f"approval {payload['approval_id']} already recorded (HUM-1)")
        clock_doc = layout.txn_get_dict(txn, layout.clock_ref(db))
        # AUD-1: the recording itself is audited in the same transaction — a
        # REJECTED decision that never causes a transition still appears in
        # the trail, with the full evidence verbatim.
        audit = build_audit_event(
            workflow_id=workflow_id,
            trace_id=message["envelope"]["trace_id"],
            agent_identity="forge-approval-surface",
            event_kind="approval",
            reason_code="APPROVAL_RECORDED",
            input_obj=payload,
            output_obj={"recorded": True},
            effective_at=int(clock_doc["logical_time"]) if clock_doc else 0,
            detail=bounded_json_detail({"approval": payload}),
        )
        txn.create(
            layout.approval_ref(db, workflow_id, payload["approval_id"]),
            {"message": message, "recorded_observed_at": now_iso()},
        )
        txn.create(layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]), audit)
        if enqueue_outbox:
            # The bus copy for downstream consumers rides the SAME
            # transaction as the authoritative record and its audit.
            txn.create(
                layout.outbox_ref(db, workflow_id, message["envelope"]["event_id"]),
                {"message": message, "published": False, "enqueued_at": now_iso()},
            )

    layout.run_in_transaction(db, _record)
    return payload["approval_id"]


def _check_gate(
    txn: Any, db: Any, workflow_id: str, target: str, approval_id: str | None
) -> dict[str, Any] | None:
    """Retrieve and check the recorded approval; raise GateBlocked if absent
    or unsuitable. Returns the approval payload for gated targets."""
    action_type = GATED_TARGETS.get(target)
    if action_type is None:
        return None
    if approval_id is None:
        raise GateBlocked(f"{target} requires HUM-1 approval ({action_type})")
    record = layout.txn_get_dict(txn, layout.approval_ref(db, workflow_id, approval_id))
    if record is None:
        raise GateBlocked(f"no recorded approval {approval_id} for workflow {workflow_id} (HUM-1)")
    payload = record["message"]["payload"]
    if payload.get("action_type") != action_type or payload.get("decision") != "approved":
        raise GateBlocked(
            f"{target} requires an approved {action_type} decision; "
            f"{approval_id} is {payload.get('decision')} {payload.get('action_type')} (HUM-1)"
        )
    return payload


def apply_transition(
    txn: Any,
    db: Any,
    *,
    workflow_id: str,
    target: str,
    agent_identity: str,
    trace_id: str,
    reason_code: str,
    approval_id: str | None = None,
    due_at: int | None = None,
    outbox_messages: list[dict[str, Any]] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Apply one checked, gated, audited transition inside ``txn`` (AUD-2).

    Rules enforced here (and nowhere else, so there is no bypass):
    the ALLOWED_TRANSITIONS table; HUM-1 gates that transactionally retrieve
    the authoritative recorded approval by ``approval_id`` and consume it
    once; ``due_at`` REQUIRED when suspending and FORBIDDEN otherwise
    (ORC-5 — a suspension without a due day could never resume unattended);
    outbox messages contract-validated at enqueue (ICD-2).

    Retry-safe by construction: this function performs only Firestore reads,
    pure computation, and buffered writes — callers MUST NOT put model or
    tool calls inside the transaction callback (Firestore reruns it on
    contention); see bus.process_message for the boundary.
    """
    # All reads first (real Firestore requires reads before writes).
    current = layout.txn_get_dict(txn, layout.workflow_ref(db, workflow_id))
    if not current:
        raise InvalidTransition(f"workflow {workflow_id} does not exist")
    clock_doc = layout.txn_get_dict(txn, layout.clock_ref(db))

    # Table validity is prior to gating: an illegal edge is InvalidTransition
    # even when no approval accompanies it.
    source = current["status"]
    if target not in ALLOWED_TRANSITIONS.get(source, set()):
        raise InvalidTransition(f"{source} -> {target} is not allowed")

    approval_payload = _check_gate(txn, db, workflow_id, target, approval_id)
    consumed_ref = None
    if approval_payload is not None:
        consumed_ref = layout.consumed_approval_ref(
            db, workflow_id, approval_payload["approval_id"]
        )
        if layout.txn_get_dict(txn, consumed_ref) is not None:
            raise GateBlocked(
                f"approval {approval_payload['approval_id']} already consumed (HUM-1)"
            )
    if target == "SUSPENDED_AWAITING_PART" and due_at is None:
        raise InvalidTransition(
            "SUSPENDED_AWAITING_PART requires due_at — an unscheduled suspension "
            "can never resume unattended (ORC-5)"
        )
    if target != "SUSPENDED_AWAITING_PART" and due_at is not None:
        raise InvalidTransition(f"due_at is only valid when suspending, not for {target}")

    logical_now = int(clock_doc["logical_time"]) if clock_doc else int(current["logical_time"])
    doc = layout.validate_state_doc(
        {
            **current,
            "status": target,
            "logical_time": logical_now,
            "due_at": due_at,
            "updated_observed_at": now_iso(),
        }
    )
    audit_input: dict[str, Any] = {"source": source, "target": target}
    audit_detail = detail
    if approval_payload is not None:
        evidence = {
            key: approval_payload[key]
            for key in (
                "approval_id",
                "action_type",
                "decision",
                "approver_identity",
                "decided_at",
            )
        }
        audit_input["approval"] = evidence
        # HUM-1: the approval evidence must reconstruct from Firestore alone —
        # recorded verbatim, not only inside the input hash.
        audit_detail = bounded_json_detail({"approval": evidence, "detail": detail})
    audit = build_audit_event(
        workflow_id=workflow_id,
        trace_id=trace_id,
        agent_identity=agent_identity,
        event_kind="approval" if approval_payload is not None else "state_change",
        reason_code=reason_code,
        input_obj=audit_input,
        output_obj=doc,
        effective_at=logical_now,
        state_after=target,
        detail=audit_detail,
    )
    for message in outbox_messages or []:
        validate_message(message)  # ICD-2: publish-side gate at enqueue

    txn.set(layout.workflow_ref(db, workflow_id), doc)
    txn.create(layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]), audit)
    if consumed_ref is not None:
        txn.create(
            consumed_ref,
            {
                "approval_id": approval_payload["approval_id"],
                "action_type": approval_payload["action_type"],
                "consumed_observed_at": now_iso(),
                "audit_event_id": audit["envelope"]["event_id"],
            },
        )
    for message in outbox_messages or []:
        txn.create(
            layout.outbox_ref(db, workflow_id, message["envelope"]["event_id"]),
            {"message": message, "published": False, "enqueued_at": now_iso()},
        )
    return doc


def create_workflow(
    db: Any, *, workflow_id: str, equipment_id: str, trace_id: str, logical_time: int
) -> dict[str, Any]:
    """Create the workflow state document in INTAKE, audited (ORC-5, AUD-1)."""

    def _create(txn: Any) -> dict[str, Any]:
        # Leading read: real transactions begin lazily on the first read —
        # a write-only transaction never acquires an ID and cannot commit.
        # Doubles as an explicit existence check.
        if layout.txn_get_dict(txn, layout.workflow_ref(db, workflow_id)):
            raise InvalidTransition(f"workflow {workflow_id} already exists")
        doc = layout.validate_state_doc(
            {
                "workflow_id": workflow_id,
                "status": "INTAKE",
                "logical_time": logical_time,
                "due_at": None,
                "equipment_id": equipment_id,
                "trace_id": trace_id,
                "updated_observed_at": now_iso(),
            }
        )
        audit = build_audit_event(
            workflow_id=workflow_id,
            trace_id=trace_id,
            agent_identity="forge-orchestrator",
            event_kind="state_change",
            reason_code="WORKFLOW_CREATED",
            input_obj={"equipment_id": equipment_id},
            output_obj=doc,
            effective_at=logical_time,
            state_after="INTAKE",
        )
        txn.create(layout.workflow_ref(db, workflow_id), doc)
        txn.create(layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]), audit)
        return doc

    return layout.run_in_transaction(db, _create)


def transition_workflow(db: Any, **kwargs: Any) -> dict[str, Any]:
    """Run :func:`apply_transition` in its own transaction (AUD-2)."""
    return layout.run_in_transaction(db, lambda txn: apply_transition(txn, db, **kwargs))


def reconstruct_audit_trail(db: Any, workflow_id: str) -> list[dict[str, Any]]:
    """Rebuild the ordered audit trail from Firestore alone (AUD-2).

    Ordered by (effective_at, observed_at, event_id) so simulated time jumps
    order correctly and ties break deterministically (AUD-3).
    """
    docs = layout.workflow_ref(db, workflow_id).collection("audit").stream()
    events = [d.to_dict() if hasattr(d, "to_dict") else d for d in docs]
    return sorted(
        events,
        key=lambda e: (
            e["payload"]["effective_at"],
            e["payload"]["observed_at"],
            e["envelope"]["event_id"],
        ),
    )
