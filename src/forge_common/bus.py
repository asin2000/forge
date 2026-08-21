"""Transactional inbox/outbox message processing (ICD-2, ICD-5, ICD-6, DAT-2).

Consumption: ``process_message`` validates the contract (ICD-2), rejects
non-TRUSTED messages for every consumer except Cyber Trust (DAT-2), then runs
the handler inside one transaction with an inbox marker keyed by
``idempotency_key`` — duplicate delivery produces no duplicate side effects
(ICD-5/ICD-6). Handlers are pure: they receive the message and a
:class:`TxnWrites` collector and perform no I/O of their own.

Publication: ``drain_outbox`` publishes unpublished outbox records with the
Pub/Sub ordering key set to the workflow_id (ICD-5). Publishing is
at-least-once by design; consumers dedupe via the inbox. The dead-letter
policy (5 attempts) is Pub/Sub subscription configuration in the deploy
config, not application code.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Any

from forge_common import layout
from forge_common.audit import now_iso
from forge_common.contracts import validate_message

CYBER_TRUST_IDENTITY = "forge-cyber-trust"


class UntrustedMessageRejected(Exception):
    """DAT-2: a non-TRUSTED message reached a non-Cyber-Trust consumer."""


@dataclasses.dataclass
class TxnWrites:
    """Writes a handler wants committed atomically with the inbox marker."""

    state_doc: dict[str, Any] | None = None
    audit_events: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    outbox_messages: list[dict[str, Any]] = dataclasses.field(default_factory=list)


def process_message(
    db: Any,
    message: dict[str, Any],
    handler: Callable[[dict[str, Any], TxnWrites], None],
    *,
    consumer_identity: str,
) -> bool:
    """Consume one bus message idempotently; True if side effects ran.

    Returns False (and writes nothing) when the idempotency_key was already
    processed (ICD-5/ICD-6). Raises ContractViolation on schema nonconformance
    (ICD-2) and UntrustedMessageRejected on a DAT-2 violation — the caller
    audits and never coerces.
    """
    validate_message(message)
    envelope = message["envelope"]
    if envelope["trust_state"] != "TRUSTED" and consumer_identity != CYBER_TRUST_IDENTITY:
        raise UntrustedMessageRejected(
            f"{consumer_identity} may not consume trust_state={envelope['trust_state']} (DAT-2)"
        )
    workflow_id = envelope["workflow_id"]
    idem_key = envelope["idempotency_key"]

    def _consume(txn: Any) -> bool:
        marker = txn.get(layout.inbox_ref(db, workflow_id, idem_key))
        existing = marker.to_dict() if hasattr(marker, "to_dict") else marker
        if existing:
            return False
        writes = TxnWrites()
        handler(message, writes)
        txn.create(
            layout.inbox_ref(db, workflow_id, idem_key),
            {
                "event_id": envelope["event_id"],
                "consumer_identity": consumer_identity,
                "processed_observed_at": now_iso(),
            },
        )
        if writes.state_doc is not None:
            txn.set(
                layout.workflow_ref(db, workflow_id),
                layout.validate_state_doc(writes.state_doc),
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
    """Publish unpublished outbox records; ordering key = workflow_id (ICD-5).

    ``publish(message, ordering_key)`` is the transport (Pub/Sub in
    production, a recording stub in tests). Records are marked published
    after the transport accepts them; a crash in between republishes, and
    consumers dedupe via the inbox marker.
    """
    published = 0
    outbox = layout.workflow_ref(db, workflow_id).collection("outbox")
    for snapshot in outbox.stream():
        record = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if record.get("published"):
            continue
        message = record["message"]
        publish(message, workflow_id)
        event_id = message["envelope"]["event_id"]
        layout.outbox_ref(db, workflow_id, event_id).set(
            {**record, "published": True, "published_at": now_iso()}
        )
        published += 1
    return published
