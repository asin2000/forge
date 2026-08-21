"""Workflow state machine with transactional, audited transitions (ORC-5,
HUM-1, AUD-2).

The transition table encodes the demo spine (Section 8). Two transitions are
human-gated (HUM-1): entering ``SUSPENDED_AWAITING_PART`` (the 21-day schedule
override) and entering ``RELEASED`` (equipment release). A gated transition
without an approved ``approval_decision`` payload raises :class:`GateBlocked`
— this is the CI-4 assertion "blocked before approval, succeeds after".

Every transition commits, in ONE transaction (AUD-2): the state document
write, the audit event, and any outbox messages the caller supplies.
"""

from __future__ import annotations

from typing import Any

from forge_common import layout
from forge_common.audit import build_audit_event, now_iso

# Spine states (workflow_state.v1 enum).
ALLOWED_TRANSITIONS: dict[str, set[str]] = {
    "INTAKE": {"PLANNING", "BLOCKED_AGENT_FAILURE"},
    "PLANNING": {"VALIDATING", "BLOCKED_AGENT_FAILURE"},
    "VALIDATING": {"AWAITING_SCHEDULE_APPROVAL", "PLANNING", "BLOCKED_AGENT_FAILURE"},
    "AWAITING_SCHEDULE_APPROVAL": {"SUSPENDED_AWAITING_PART", "PLANNING"},
    "SUSPENDED_AWAITING_PART": {"ASSEMBLY_RESUMED"},
    "ASSEMBLY_RESUMED": {"AWAITING_RELEASE_APPROVAL", "BLOCKED_AGENT_FAILURE"},
    "AWAITING_RELEASE_APPROVAL": {"RELEASED"},
    "RELEASED": set(),
    "BLOCKED_AGENT_FAILURE": {"PLANNING"},
}

#: target state -> required HUM-1 action_type
GATED_TARGETS: dict[str, str] = {
    "SUSPENDED_AWAITING_PART": "schedule_override",
    "RELEASED": "equipment_release",
}


class InvalidTransition(Exception):
    """Transition not in the ALLOWED_TRANSITIONS table."""


class GateBlocked(Exception):
    """HUM-1: gated transition attempted without an approved decision."""


def _check_gate(target: str, approval: dict[str, Any] | None) -> str | None:
    """Return approver identity if the gate passes; raise GateBlocked if not."""
    action_type = GATED_TARGETS.get(target)
    if action_type is None:
        return None
    if approval is None:
        raise GateBlocked(f"{target} requires HUM-1 approval ({action_type})")
    payload = approval.get("payload", approval)
    if (
        payload.get("action_type") != action_type
        or payload.get("decision") != "approved"
        or not payload.get("approver_identity")
    ):
        raise GateBlocked(
            f"{target} requires an approved {action_type} decision with approver_identity (HUM-1)"
        )
    return str(payload["approver_identity"])


def create_workflow(
    db: Any, *, workflow_id: str, equipment_id: str, trace_id: str, logical_time: int
) -> dict[str, Any]:
    """Create the workflow state document in INTAKE, audited (ORC-5, AUD-1)."""

    def _create(txn: Any) -> dict[str, Any]:
        doc = layout.validate_state_doc(
            {
                "workflow_id": workflow_id,
                "status": "INTAKE",
                "logical_time": logical_time,
                "due_at": None,
                "equipment_id": equipment_id,
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


def transition_workflow(
    db: Any,
    *,
    workflow_id: str,
    target: str,
    agent_identity: str,
    trace_id: str,
    reason_code: str,
    approval: dict[str, Any] | None = None,
    due_at: int | None = None,
    outbox_messages: list[dict[str, Any]] | None = None,
    detail: str | None = None,
) -> dict[str, Any]:
    """Atomically transition state + audit + outbox in one transaction (AUD-2).

    ``approval`` is a full approval_decision.v2 message (or its payload) and is
    required for gated targets (HUM-1). ``due_at`` sets the Logical Clock due
    day when suspending (ORC-5). ``outbox_messages`` are contract-validated
    bus messages enqueued atomically with the transition (ICD-5).
    """

    def _transition(txn: Any) -> dict[str, Any]:
        snapshot = txn.get(layout.workflow_ref(db, workflow_id))
        current = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if not current:
            raise InvalidTransition(f"workflow {workflow_id} does not exist")
        source = current["status"]
        if target not in ALLOWED_TRANSITIONS.get(source, set()):
            raise InvalidTransition(f"{source} -> {target} is not allowed")
        approver = _check_gate(target, approval)

        doc = layout.validate_state_doc(
            {
                **current,
                "status": target,
                "due_at": due_at,
                "updated_observed_at": now_iso(),
            }
        )
        audit_input: dict[str, Any] = {"source": source, "target": target}
        if approver is not None:
            audit_input["approver_identity"] = approver
        audit = build_audit_event(
            workflow_id=workflow_id,
            trace_id=trace_id,
            agent_identity=agent_identity,
            event_kind="approval" if approver is not None else "state_change",
            reason_code=reason_code,
            input_obj=audit_input,
            output_obj=doc,
            effective_at=current["logical_time"],
            state_after=target,
            detail=detail,
        )
        txn.set(layout.workflow_ref(db, workflow_id), doc)
        txn.create(layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]), audit)
        for message in outbox_messages or []:
            event_id = message["envelope"]["event_id"]
            txn.create(
                layout.outbox_ref(db, workflow_id, event_id),
                {"message": message, "published": False, "enqueued_at": now_iso()},
            )
        return doc

    return layout.run_in_transaction(db, _transition)


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
