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
from forge_common.audit import now_iso
from forge_common.contracts import validate_message
from forge_common.messages import build_envelope, deterministic_event_id


class AlreadyEmitted(Exception):
    """The due event for (workflow_id, due_at) already exists (double-fire)."""


def read_clock(db: Any) -> int:
    snapshot = layout.clock_ref(db).get()
    doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
    return int(doc["logical_time"]) if doc else 0


def advance_clock(db: Any, days: int) -> int:
    """Advance simulated time by ``days``; returns the new logical_time."""
    if days <= 0:
        raise ValueError("clock only advances")

    def _advance(txn: Any) -> int:
        snapshot = txn.get(layout.clock_ref(db))
        doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        new_time = (int(doc["logical_time"]) if doc else 0) + days
        txn.set(layout.clock_ref(db), {"logical_time": new_time})
        return new_time

    return layout.run_in_transaction(db, _advance)


def build_due_event(
    *, workflow_id: str, trace_id: str, due_at: int, purpose: str = "part_eta_reached"
) -> dict[str, Any]:
    """Deterministic due_event.v2 for (workflow_id, due_at) (ORC-5)."""
    event_id = deterministic_event_id("due", workflow_id, str(due_at))
    message = {
        "envelope": build_envelope(
            workflow_id=workflow_id,
            schema_version="due_event.v2",
            event_id=event_id,
            trace_id=trace_id,
            idempotency_key=f"idem-due-{workflow_id}-{due_at:04d}",
        ),
        "payload": {"due_at_logical": due_at, "purpose": purpose},
    }
    validate_message(message)
    return message


def emit_due_events(db: Any, *, logical_time: int, trace_id: str) -> list[str]:
    """Enqueue due events for every workflow with ``due_at <= logical_time``.

    Returns workflow_ids for which a NEW due event was enqueued. Each
    enqueue is its own transaction keyed on a deterministic document ID, so
    re-running after a crash or a double-fired advance adds nothing.
    """
    emitted: list[str] = []
    for snapshot in db.collection("workflows").stream():
        state = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        due_at = state.get("due_at")
        if due_at is None or due_at > logical_time:
            continue
        workflow_id = state["workflow_id"]
        message = build_due_event(workflow_id=workflow_id, trace_id=trace_id, due_at=due_at)
        event_id = message["envelope"]["event_id"]

        def _enqueue(
            txn: Any, wid: str = workflow_id, eid: str = event_id, msg: dict[str, Any] = message
        ) -> bool:
            existing = txn.get(layout.outbox_ref(db, wid, eid))
            doc = existing.to_dict() if hasattr(existing, "to_dict") else existing
            if doc:
                return False
            txn.create(
                layout.outbox_ref(db, wid, eid),
                {"message": msg, "published": False, "enqueued_at": now_iso()},
            )
            return True

        if layout.run_in_transaction(db, _enqueue):
            emitted.append(workflow_id)
    return emitted
