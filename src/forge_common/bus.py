"""Transactional inbox/outbox message processing (ICD-2, ICD-5, ICD-6, DAT-2).

Consumption: ``process_message`` validates the contract (ICD-2), rejects
non-TRUSTED messages for every consumer except Cyber Trust (DAT-2) — both
rejection paths emit an AUD-1 ``blocked_action`` audit event ("rejected and
audited", ICD-2) — then runs the handler inside one transaction with an inbox
marker keyed by ``(idempotency_key, consumer_identity)``: duplicate delivery
produces no duplicate side effects, and fan-out subscribers never suppress
each other (ICD-5/ICD-6).

Handlers are pure: they receive the message and a :class:`TxnWrites`
collector. A state change is requested via ``writes.transition`` and applied
through :func:`forge_common.state.apply_transition` in the SAME transaction —
there is no unchecked state-write path, so the transition table and both
HUM-1 gates hold on the bus path too.

Publication: ``drain_outbox`` re-validates each message (ICD-2 at publish)
and publishes unpublished records in enqueue order with the Pub/Sub ordering
key set to the workflow_id (ICD-5). Publishing is at-least-once by design;
consumers dedupe via the inbox. The dead-letter policy (5 attempts) is
Pub/Sub subscription configuration in the deploy config, not application
code.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from forge_common import layout, state
from forge_common.audit import build_audit_event, now_iso
from forge_common.contracts import ContractViolation, validate_message

CYBER_TRUST_IDENTITY = "forge-cyber-trust"


class UntrustedMessageRejected(Exception):
    """DAT-2: a non-TRUSTED message reached a non-Cyber-Trust consumer."""


@dataclasses.dataclass
class TxnWrites:
    """Writes a handler wants committed atomically with the inbox marker.

    ``transition`` is a kwargs dict for :func:`state.apply_transition`
    (everything except workflow_id, which comes from the envelope) — the only
    way a handler changes workflow state, so gates and the transition table
    apply (HUM-1).
    """

    transition: dict[str, Any] | None = None
    audit_events: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    outbox_messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)


def _audit_rejection(
    db: Any, message: dict[str, Any], *, consumer_identity: str, reason_code: str
) -> None:
    """Best-effort AUD-1 record for a rejected message (ICD-2/DAT-2).

    Routed to the workflow's audit trail when the envelope carries a usable
    workflow_id; a message too malformed to route is still rejected, just
    not workflow-attributable.
    """
    envelope = message.get("envelope") or {}
    workflow_id = envelope.get("workflow_id")
    trace_id = envelope.get("trace_id")
    if not isinstance(workflow_id, str) or not workflow_id.startswith("wf-"):
        return
    if not isinstance(trace_id, str) or len(trace_id) != 32:
        trace_id = "0" * 32
    audit = build_audit_event(
        workflow_id=workflow_id,
        trace_id=trace_id,
        agent_identity=consumer_identity,
        event_kind="blocked_action",
        reason_code=reason_code,
        input_obj=message,
        output_obj={"rejected": True},
        effective_at=_clock_time(db),
        detail=f"rejected at subscribe by {consumer_identity}",
    )

    def _write(txn: Any) -> None:
        txn.create(layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]), audit)

    layout.run_in_transaction(db, _write)


def _clock_time(db: Any) -> int:
    snapshot = layout.clock_ref(db).get()
    doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
    return int(doc["logical_time"]) if doc else 0


def process_message(
    db: Any,
    message: dict[str, Any],
    handler: Callable[[dict[str, Any], TxnWrites], None],
    *,
    consumer_identity: str,
) -> bool:
    """Consume one bus message idempotently; True if side effects ran.

    Returns False (writing nothing new) when this consumer already processed
    the idempotency_key (ICD-5/ICD-6). Raises ContractViolation (ICD-2) or
    UntrustedMessageRejected (DAT-2) after writing a blocked_action audit
    event — rejected and audited, never coerced.
    """
    try:
        validate_message(message)
    except ContractViolation:
        _audit_rejection(
            db,
            message,
            consumer_identity=consumer_identity,
            reason_code="CONTRACT_VIOLATION_REJECTED",
        )
        raise
    envelope = message["envelope"]
    if envelope["trust_state"] != "TRUSTED" and consumer_identity != CYBER_TRUST_IDENTITY:
        _audit_rejection(
            db,
            message,
            consumer_identity=consumer_identity,
            reason_code="UNTRUSTED_MESSAGE_REJECTED",
        )
        raise UntrustedMessageRejected(
            f"{consumer_identity} may not consume trust_state={envelope['trust_state']} (DAT-2)"
        )
    workflow_id = envelope["workflow_id"]
    # Consumer-scoped marker: fan-out subscribers dedupe independently.
    marker_id = f"{envelope['idempotency_key']}--{consumer_identity}"

    def _consume(txn: Any) -> bool:
        existing = layout.txn_get_dict(txn, layout.inbox_ref(db, workflow_id, marker_id))
        if existing:
            return False
        writes = TxnWrites()
        handler(message, writes)
        if writes.transition is not None:
            state.apply_transition(txn, db, workflow_id=workflow_id, **writes.transition)
        txn.create(
            layout.inbox_ref(db, workflow_id, marker_id),
            {
                "event_id": envelope["event_id"],
                "consumer_identity": consumer_identity,
                "processed_observed_at": now_iso(),
            },
        )
        for audit in writes.audit_events:
            txn.create(
                layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]),
                audit,
            )
        for out in writes.outbox_messages:
            validate_message(out)
            txn.create(
                layout.outbox_ref(db, workflow_id, out["envelope"]["event_id"]),
                {"message": out, "published": False, "enqueued_at": now_iso()},
            )
        return True

    return layout.run_in_transaction(db, _consume)


def drain_outbox(
    db: Any,
    workflow_id: str,
    publish: Callable[[dict[str, Any], str], None],
) -> int:
    """Publish unpublished outbox records in enqueue order (ICD-2, ICD-5).

    ``publish(message, ordering_key)`` is the transport (Pub/Sub in
    production, a recording stub in tests). Records publish in
    ``(enqueued_at, event_id)`` order — the per-workflow ordering key
    preserves publish order, so publish order must BE enqueue order. Each
    message is re-validated before it reaches the transport (ICD-2 at
    publish). Records are marked published after the transport accepts them;
    a crash in between republishes, and consumers dedupe via the inbox.
    """
    outbox = layout.workflow_ref(db, workflow_id).collection("outbox")
    pending = []
    for snapshot in outbox.stream():
        record = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if not record.get("published"):
            pending.append(record)
    pending.sort(key=lambda r: (r.get("enqueued_at", ""), r["message"]["envelope"]["event_id"]))
    published = 0
    for record in pending:
        message = record["message"]
        validate_message(message)
        publish(message, workflow_id)
        event_id = message["envelope"]["event_id"]
        layout.outbox_ref(db, workflow_id, event_id).set(
            {**record, "published": True, "published_at": now_iso()}
        )
        published += 1
    return published
