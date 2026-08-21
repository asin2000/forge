"""Transactional inbox/outbox message processing (ICD-2, ICD-5, ICD-6, DAT-2).

Consumption: ``process_message`` validates the contract (ICD-2), rejects
non-TRUSTED messages for every consumer except Cyber Trust (DAT-2) — both
rejection paths emit an AUD-1 ``blocked_action`` audit event ("rejected and
audited", ICD-2) — then runs the handler inside one transaction with an inbox
marker keyed by ``(idempotency_key, consumer_identity)``: duplicate delivery
produces no duplicate side effects, and fan-out subscribers never suppress
each other (ICD-5/ICD-6).

Handlers run OUTSIDE the transaction callback: Firestore reruns transaction
callbacks on contention, so model, tool, or any other side-effectful call
must never live inside one. A transactional CLAIM with a lease is taken
before the handler runs, so two workers holding the same delivery cannot
both execute the handler (no concurrent duplicate Gemini/tool calls); a
holder that crashes lets the lease expire, after which redelivery may rerun
the handler — that residual is inherent to at-least-once delivery. The
handler produces a precomputed, deterministic effect plan
(:class:`TxnWrites`) and only that plan is applied inside the committing
transaction, which verifies the claim is still held. A state change is
requested via ``writes.transition`` and applied through
:func:`forge_common.state.apply_transition` in the SAME transaction as the
inbox marker — there is no unchecked state-write path, so the transition
table and both HUM-1 gates hold on the bus path too.

Publication: ``drain_outbox`` re-validates each message (ICD-2 at publish)
and publishes unpublished records in enqueue order with the Pub/Sub ordering
key set to the workflow_id (ICD-5). Publishing is at-least-once by design;
consumers dedupe via the inbox. The dead-letter policy (5 attempts) is
Pub/Sub subscription configuration in the deploy config, not application
code.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
import re
import uuid
from collections.abc import Callable
from typing import Any

from forge_common import layout, registry, state
from forge_common.audit import build_audit_event, now_iso
from forge_common.contracts import ContractViolation, validate_message
from forge_common.messages import sha256_hex

CYBER_TRUST_IDENTITY = "forge-cyber-trust"

#: Reserved workflow ID collecting rejection audits for messages too
#: malformed to route (no valid workflow_id in the envelope).
UNROUTABLE_AUDIT_WORKFLOW = "wf-security-unroutable"

_WORKFLOW_ID_RE = re.compile(r"^wf-[a-z0-9][a-z0-9-]{2,62}$")
_TRACE_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class UntrustedMessageRejected(Exception):
    """DAT-2: a non-TRUSTED message reached a non-Cyber-Trust consumer."""


class DeliveryInProgress(Exception):
    """Another consumer instance holds an unexpired claim on this delivery.

    The caller must NACK (not ack) so the transport redelivers later."""


#: How long a handler may run before its claim can be taken over.
DEFAULT_LEASE_SECONDS = 120


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
    #: exclusive work-package ownership claims (ORC-1) — kwargs for
    #: registry.claim_work_package minus db/workflow_id; applied in the
    #: committing transaction, colliding if the package already has an owner.
    work_package_claims: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    #: atomic ownership transfers to a held reserve (ORC-3) — kwargs for
    #: registry.check_reassignment/apply_reassignment minus db/workflow_id,
    #: optionally bundling audit_event + assignment_message which commit ONLY
    #: when the transfer applies. A stale transfer is a transactional no-op.
    reassignments: list[dict[str, Any]] = dataclasses.field(default_factory=list)
    #: work-package status transitions (COMPLETED / FAILED_PENDING_REPAIR),
    #: applied atomically with the rest of the plan; skipped when the package
    #: is no longer ASSIGNED under the expected owner.
    work_package_status_updates: list[dict[str, Any]] = dataclasses.field(default_factory=list)


def _audit_rejection(
    db: Any, message: dict[str, Any], *, consumer_identity: str, reason_code: str
) -> None:
    """Best-effort AUD-1 record for a rejected message (ICD-2/DAT-2).

    Routed to the workflow's audit trail when the envelope carries a usable
    workflow_id; a message too malformed to route is still rejected, just
    not workflow-attributable.
    """
    envelope = message.get("envelope") if isinstance(message, dict) else None
    envelope = envelope if isinstance(envelope, dict) else {}
    workflow_id = envelope.get("workflow_id")
    trace_id = envelope.get("trace_id")
    # Fallbacks must themselves satisfy the envelope patterns, or the
    # rejection-audit message would fail its own contract validation: a
    # workflow_id merely starting with "wf-" (or a 32-char non-hex trace)
    # is NOT usable.
    if not isinstance(workflow_id, str) or not _WORKFLOW_ID_RE.fullmatch(workflow_id):
        # Unroutable malformed input still gets audited (ICD-2 "rejected and
        # audited" has no exemption) — recorded under a reserved security
        # workflow so nothing is silently dropped.
        workflow_id = UNROUTABLE_AUDIT_WORKFLOW
    if not isinstance(trace_id, str) or not _TRACE_ID_RE.fullmatch(trace_id):
        trace_id = "0" * 32
    try:
        json.dumps(message, sort_keys=True)
        input_obj: Any = message
    except TypeError:  # not JSON-serializable — audit its repr instead
        input_obj = {"unserializable": repr(message)[:2000]}
    audit = build_audit_event(
        workflow_id=workflow_id,
        trace_id=trace_id,
        agent_identity=consumer_identity,
        event_kind="blocked_action",
        reason_code=reason_code,
        input_obj=input_obj,
        output_obj={"rejected": True},
        effective_at=_clock_time(db),
        detail=f"rejected at subscribe by {consumer_identity}",
    )

    def _write(txn: Any) -> None:
        ref = layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"])
        if layout.txn_get_dict(txn, ref):  # leading read: lazy-begin + idempotent
            return
        txn.create(ref, audit)

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
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
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
    # Consumer-scoped marker, hashed: contract-valid idempotency keys may
    # contain characters (e.g. '/') that are illegal in Firestore document
    # IDs. Raw values are kept in the marker document for inspection.
    marker_id = sha256_hex([envelope["idempotency_key"], consumer_identity])
    marker_ref = layout.inbox_ref(db, workflow_id, marker_id)

    # CLAIM with lease before the handler runs: two workers holding the same
    # delivery cannot both execute the handler (model/tool calls, Day 3+).
    claim_token = uuid.uuid4().hex
    now = now_iso()
    lease_expiry = (
        (datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=lease_seconds))
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )

    def _claim(txn: Any) -> str:
        existing = layout.txn_get_dict(txn, marker_ref)
        if existing:
            if existing.get("status") == "done":
                return "done"
            if existing.get("lease_expires_at", "") > now:
                return "in_progress"
            # Expired lease: the previous holder crashed — take over.
        txn.set(
            marker_ref,
            {
                "status": "processing",
                "claim_token": claim_token,
                "lease_expires_at": lease_expiry,
                "event_id": envelope["event_id"],
                "idempotency_key": envelope["idempotency_key"],
                "consumer_identity": consumer_identity,
            },
        )
        return "claimed"

    outcome = layout.run_in_transaction(db, _claim)
    if outcome == "done":
        return False
    if outcome == "in_progress":
        raise DeliveryInProgress(
            f"delivery {envelope['idempotency_key']} is being processed by another "
            f"{consumer_identity} instance — NACK and retry after the lease"
        )

    def _release_claim() -> None:
        """Best-effort: expire our claim so redelivery is not lease-delayed."""

        def _release(txn: Any) -> None:
            current = layout.txn_get_dict(txn, marker_ref)
            if current and current.get("claim_token") == claim_token:
                txn.set(marker_ref, {**current, "lease_expires_at": now})

        layout.run_in_transaction(db, _release)

    try:
        writes = TxnWrites()
        handler(message, writes)
        for audit in writes.audit_events:
            # Handler-supplied audit events are full bus messages: validate
            # the contract, pin them to the consumed message's workflow (ICD-2).
            if validate_message(audit) != "audit_event.v2":
                raise ContractViolation(
                    audit["envelope"]["schema_version"],
                    ["only audit_event.v2 may be written to the audit trail"],
                )
            if audit["envelope"]["workflow_id"] != workflow_id:
                raise ContractViolation(
                    "audit_event.v2",
                    [f"audit event workflow_id != consumed message workflow {workflow_id}"],
                )
        for out in writes.outbox_messages:
            validate_message(out)
            if out["envelope"]["workflow_id"] != workflow_id:
                raise ContractViolation(
                    out["envelope"]["schema_version"],
                    [f"outbox message workflow_id != consumed message workflow {workflow_id}"],
                )

        def _commit(txn: Any) -> bool:
            current = layout.txn_get_dict(txn, marker_ref)
            if (
                not current
                or current.get("claim_token") != claim_token
                or current.get("status") == "done"
            ):
                return False  # claim lost to a takeover — the holder will commit
            # All claim-eligibility and reassignment READS precede any
            # write (REG-2/ORC-3; real Firestore requires reads first).
            for claim in writes.work_package_claims:
                registry.check_claim_eligibility(txn, db, **claim)
            plans = [
                registry.check_reassignment(txn, db, workflow_id=workflow_id, **r)
                for r in writes.reassignments
            ]
            status_plans = [
                registry.check_wp_status_update(txn, db, workflow_id=workflow_id, **u)
                for u in writes.work_package_status_updates
            ]
            if writes.transition is not None:
                state.apply_transition(txn, db, workflow_id=workflow_id, **writes.transition)
            for claim in writes.work_package_claims:
                registry.claim_work_package(txn, db, workflow_id=workflow_id, **claim)
            for reassignment, plan in zip(writes.reassignments, plans, strict=True):
                registry.apply_reassignment(
                    txn, db, workflow_id=workflow_id, **reassignment, plan=plan
                )
            for update, plan in zip(writes.work_package_status_updates, status_plans, strict=True):
                registry.apply_wp_status_update(
                    txn, db, workflow_id=workflow_id, **update, plan=plan
                )
            txn.set(
                marker_ref,
                {**current, "status": "done", "processed_observed_at": now_iso()},
            )
            for audit in writes.audit_events:
                txn.create(
                    layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]),
                    audit,
                )
            for out in writes.outbox_messages:
                txn.create(
                    layout.outbox_ref(db, workflow_id, out["envelope"]["event_id"]),
                    {"message": out, "published": False, "enqueued_at": now_iso()},
                )
            return True

        try:
            return layout.run_in_transaction(db, _commit)
        except registry.IneligibleAssignment:
            # REG-2: rejected AND audited — then re-raised so the delivery
            # NACKs and the redelivery re-discovers against fresh state.
            _audit_rejection(
                db,
                message,
                consumer_identity=consumer_identity,
                reason_code="ASSIGNMENT_INELIGIBLE",
            )
            raise
    except Exception:
        _release_claim()
        raise


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
