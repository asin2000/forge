"""Logical Clock: persisted simulated time with idempotent due events (ORC-5).

The clock is a Firestore document (``system/logical_clock``); system time is
never modified (Section 2). Advancing the clock scans workflow state for
``due_at <= logical_time`` and emits a ``due_event.v2`` into each due
workflow's outbox. The due event's document ID is deterministic in
``(workflow_id, due_at)``, so a double-fired advance collides on ``create``
and is skipped — processing of due events is idempotent (ORC-5; verified by
the double-fire test).
"""

from __future__ import annotations

from typing import Any

from forge_common import layout
from forge_common.audit import build_audit_event, now_iso
from forge_common.contracts import validate_message
from forge_common.messages import (
    build_envelope,
    deterministic_event_id,
    deterministic_trace_id,
)


class AlreadyEmitted(Exception):
    """The due event for (workflow_id, due_at) already exists (double-fire)."""


def read_clock(db: Any) -> int:
    snapshot = layout.clock_ref(db).get()
    doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
    return int(doc["logical_time"]) if doc else 0


#: Reserved workflow ID whose audit subcollection records Logical Clock
#: advances (AUD-1: every state change is audited; the clock is state).
CLOCK_AUDIT_WORKFLOW = "wf-system-clock"


def advance_clock(
    db: Any,
    days: int,
    *,
    agent_identity: str = "forge-orchestrator",
    detail: str | None = None,
) -> int:
    """Advance simulated time by ``days``; returns the new logical_time.

    The advance itself is audited (AUD-1) under ``wf-system-clock`` in the
    same transaction as the clock write, so the jump is reconstructable from
    Firestore alone (AUD-2/AUD-3). ``detail`` lets the HUM-3 operator
    surface record the authenticated principal behind a console advance.
    """
    if days <= 0:
        raise ValueError("clock only advances")

    def _advance(txn: Any) -> int:
        doc = layout.txn_get_dict(txn, layout.clock_ref(db))
        old_time = int(doc["logical_time"]) if doc else 0
        new_time = old_time + days
        audit = build_audit_event(
            workflow_id=CLOCK_AUDIT_WORKFLOW,
            trace_id=deterministic_trace_id(CLOCK_AUDIT_WORKFLOW),
            agent_identity=agent_identity,
            event_kind="state_change",
            reason_code="CLOCK_ADVANCED",
            input_obj={"from": old_time, "days": days},
            output_obj={"to": new_time},
            effective_at=new_time,
            detail=detail,
        )
        txn.set(layout.clock_ref(db), {"logical_time": new_time})
        txn.create(
            layout.audit_ref(db, CLOCK_AUDIT_WORKFLOW, audit["envelope"]["event_id"]),
            audit,
        )
        return new_time

    return layout.run_in_transaction(db, _advance)


def build_due_event(
    *, workflow_id: str, trace_id: str, due_at: int, purpose: str = "part_eta_reached"
) -> dict[str, Any]:
    """Deterministic due_event.v2 for (workflow_id, due_at, purpose) (ORC-5).

    Identity includes ``purpose`` so a same-day ``review_due`` is a distinct
    event from ``part_eta_reached``. Known scope limit (documented, demo
    spine never hits it): a workflow that re-arms a previously-fired absolute
    due day collides with the retained outbox record and will not re-fire; a
    suspension-epoch counter would require a workflow_state schema bump and
    is deliberately out of v1.2 scope (§10).
    """
    event_id = deterministic_event_id("due", workflow_id, str(due_at), purpose)
    message = {
        "envelope": build_envelope(
            workflow_id=workflow_id,
            schema_version="due_event.v2",
            event_id=event_id,
            trace_id=trace_id,
            idempotency_key=f"idem-due-{workflow_id}-{due_at:04d}-{purpose}",
        ),
        "payload": {"due_at_logical": due_at, "purpose": purpose},
    }
    validate_message(message)
    return message


def emit_due_events(db: Any) -> list[str]:
    """Enqueue due events for every workflow whose due day has arrived.

    The logical time is read from the clock document — never trusted from a
    caller. The collection scan is only a candidate list: each candidate is
    RE-CHECKED transactionally (status is SUSPENDED_AWAITING_PART, due_at
    still set and due against the clock read in the same transaction) before
    the due event is created, so a stale scan or a caller race cannot emit
    early or against changed state. Each due event rides the WORKFLOW's root
    trace (state doc trace_id, OBS-1) — never a caller-supplied one, so this
    unattended producer can never mint a second application trace. Returns
    workflow_ids newly enqueued; re-running after a crash or a double-fired
    advance adds nothing.
    """
    emitted: list[str] = []
    for snapshot in db.collection("workflows").stream():
        candidate = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if candidate.get("due_at") is None:
            continue
        workflow_id = candidate["workflow_id"]

        def _enqueue(txn: Any, wid: str = workflow_id) -> bool:
            current = layout.txn_get_dict(txn, layout.workflow_ref(db, wid))
            clock_doc = layout.txn_get_dict(txn, layout.clock_ref(db))
            logical_now = int(clock_doc["logical_time"]) if clock_doc else 0
            if (
                not current
                or current.get("status") != "SUSPENDED_AWAITING_PART"
                or current.get("due_at") is None
                or current["due_at"] > logical_now
            ):
                return False
            trace = current.get("trace_id") or deterministic_trace_id(wid)
            msg = build_due_event(workflow_id=wid, trace_id=trace, due_at=current["due_at"])
            eid = msg["envelope"]["event_id"]
            if layout.txn_get_dict(txn, layout.outbox_ref(db, wid, eid)):
                return False
            txn.create(
                layout.outbox_ref(db, wid, eid),
                {"message": msg, "published": False, "enqueued_at": now_iso()},
            )
            return True

        if layout.run_in_transaction(db, _enqueue):
            emitted.append(workflow_id)
    return emitted
