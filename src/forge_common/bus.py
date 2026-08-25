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

from forge_common import layout, otel, registry, state
from forge_common.audit import build_audit_event, now_iso
from forge_common.contracts import ContractViolation, validate_message
from forge_common.messages import deterministic_event_id, sha256_hex

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


def _rekey_completion(message: dict[str, Any], workflow_id: str, epoch: str) -> dict[str, Any]:
    """Re-keyed copy of a completion verdict for post-resume routing.
    The event id folds the RESUME EPOCH (= the workflow's logical day at
    resume, which equals due_at), so replays dedupe and a second suspension
    produces a fresh id instead of an AlreadyExists crash-loop."""
    subject = message["payload"]["subject_event_id"]
    return {
        "envelope": {
            **message["envelope"],
            "event_id": deterministic_event_id("resume-reverdict", workflow_id, subject, epoch),
            "idempotency_key": f"idem-resume-reverdict-{subject[:8]}-{epoch}",
        },
        "payload": message["payload"],
    }


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
    #: Roster (completion) verdict consumed PRE-resume: {"message": verdict}.
    #: The committing txn RE-READS the workflow doc — if the resume raced in
    #: during the handler->commit window, the evidence is re-keyed onto the
    #: bus instead of parked; otherwise the completion marker doc is set
    #: (latest verdict wins). One half of the release-gate race closure.
    completion_evidence: dict[str, Any] | None = None
    #: Due handler's resume re-key request: {"due_at": int}. The committing
    #: txn READS the completion marker; if evidence exists, a re-keyed copy
    #: (event id folded with the resume epoch — replay- and second-
    #: suspension-safe) is created ATOMICALLY with the resume transition.
    #: The other half of the race closure: whichever side commits second
    #: sees the first side's durable record.
    resume_rekey: dict[str, Any] | None = None
    #: A specialist's ENTIRE package-scoped result under ONE ownership guard:
    #: {work_package_id, expected_owner, status, outbox_messages, audit_events}.
    #: If the package is no longer ASSIGNED under expected_owner at commit
    #: time, NOTHING in the bundle commits — a reassigned worker cannot
    #: publish a stale result, audit, or status.
    owned_effects: dict[str, Any] | None = None
    #: Conditional non-reserve failure disposition: {work_package_id,
    #: failed_instance_id, transition, audit_event}. The package is
    #: transactionally re-read; if completed/stale the event is consumed with
    #: zero effects, otherwise package+instance are marked FAILED, the
    #: workflow blocks, and the escalation audit lands — one transaction.
    failure_disposition: dict[str, Any] | None = None


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
    transport_attributes: dict[str, str] | None = None,
) -> bool:
    """Consume one bus message idempotently; True if side effects ran.

    Returns False (writing nothing new) when this consumer already processed
    the idempotency_key (ICD-5/ICD-6). Raises ContractViolation (ICD-2) or
    UntrustedMessageRejected (DAT-2) after writing a blocked_action audit
    event — rejected and audited, never coerced.

    ``transport_attributes`` are the Pub/Sub message attributes: the W3C
    ``traceparent`` there is the authoritative trace context (OBS-1); the
    whole consumption runs under one metadata-only span (OBS-2), which ADK's
    own agent/model spans nest beneath.
    """
    envelope_for_span = message.get("envelope", {}) if isinstance(message, dict) else {}
    if not isinstance(envelope_for_span, dict) or "trace_id" not in envelope_for_span:
        return _process_message_inner(
            db,
            message,
            handler,
            consumer_identity=consumer_identity,
            lease_seconds=lease_seconds,
        )
    with otel.consume_span(consumer_identity, envelope_for_span, transport_attributes) as span:
        result = _process_message_inner(
            db,
            message,
            handler,
            consumer_identity=consumer_identity,
            lease_seconds=lease_seconds,
        )
        span.set_attribute("forge.outcome", "processed" if result else "deduplicated")
        return result


def _process_message_inner(
    db: Any,
    message: dict[str, Any],
    handler: Callable[[dict[str, Any], TxnWrites], None],
    *,
    consumer_identity: str,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> bool:
    try:
        validate_message(message)
    except ContractViolation as exc:
        _audit_rejection(
            db,
            message,
            consumer_identity=consumer_identity,
            reason_code="CONTRACT_VIOLATION_REJECTED",
        )
        # the worker acks (200) ONLY violations that were audited here at
        # the INPUT boundary; an output-side violation carries no marker and
        # falls through to 500 -> DLQ (visible, never silent loss)
        exc.audited_input_rejection = True
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
        # Validate EVERY nested message — including those inside the
        # conditional bundles — before the transaction (ICD-2).
        bundled_audits = list(writes.audit_events)
        bundled_outbox = list(writes.outbox_messages)
        if writes.owned_effects is not None:
            bundled_audits += writes.owned_effects.get("audit_events", [])
            bundled_outbox += writes.owned_effects.get("outbox_messages", [])
        if writes.failure_disposition is not None:
            bundled_audits.append(writes.failure_disposition["audit_event"])
        for reassignment in writes.reassignments:
            if reassignment.get("audit_event"):
                bundled_audits.append(reassignment["audit_event"])
            if reassignment.get("assignment_message"):
                bundled_outbox.append(reassignment["assignment_message"])
        for audit in bundled_audits:
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
        for out in bundled_outbox:
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
            # Release-gate race closure READS (before any write):
            evidence_wf_doc = None
            if writes.completion_evidence is not None:
                evidence_wf_doc = layout.txn_get_dict(txn, layout.workflow_ref(db, workflow_id))
            rekey_plan = None
            if writes.resume_rekey is not None:
                held = layout.txn_get_dict(txn, layout.completion_evidence_ref(db, workflow_id))
                if held is not None:
                    candidate = _rekey_completion(
                        held["message"], workflow_id, str(writes.resume_rekey["due_at"])
                    )
                    existing = layout.txn_get_dict(
                        txn,
                        layout.outbox_ref(db, workflow_id, candidate["envelope"]["event_id"]),
                    )
                    if existing is None:
                        rekey_plan = candidate
            raced_rekey_plan = None
            if (
                writes.completion_evidence is not None
                and evidence_wf_doc is not None
                and evidence_wf_doc.get("status") == "ASSEMBLY_RESUMED"
            ):
                candidate = _rekey_completion(
                    writes.completion_evidence["message"],
                    workflow_id,
                    str(evidence_wf_doc.get("logical_time", 0)),
                )
                existing = layout.txn_get_dict(
                    txn, layout.outbox_ref(db, workflow_id, candidate["envelope"]["event_id"])
                )
                if existing is None:
                    raced_rekey_plan = candidate
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
            owned_plan = None
            if writes.owned_effects is not None:
                owned_plan = registry.check_wp_status_update(
                    txn,
                    db,
                    workflow_id=workflow_id,
                    work_package_id=writes.owned_effects["work_package_id"],
                    expected_owner=writes.owned_effects["expected_owner"],
                )
            disposition_plan = None
            if writes.failure_disposition is not None:
                if writes.transition is not None:
                    raise ValueError("transition and failure_disposition are exclusive")
                disposition_plan = registry.check_failure_disposition(
                    txn, db, workflow_id=workflow_id, **writes.failure_disposition
                )
                if not disposition_plan["skip"] and not disposition_plan.get(
                    "workflow_already_blocked"
                ):
                    # The disposition's workflow block applies here, before
                    # any other write, preserving reads-before-writes. An
                    # already-blocked workflow skips the redundant (illegal)
                    # transition but still gets the FAILED marks + audit.
                    state.apply_transition(
                        txn,
                        db,
                        workflow_id=workflow_id,
                        **writes.failure_disposition["transition"],
                    )
            if writes.transition is not None:
                state.apply_transition(txn, db, workflow_id=workflow_id, **writes.transition)
            if writes.completion_evidence is not None:
                if raced_rekey_plan is not None:
                    # the resume committed during the handler->commit window:
                    # the evidence goes straight back onto the bus
                    txn.create(
                        layout.outbox_ref(
                            db, workflow_id, raced_rekey_plan["envelope"]["event_id"]
                        ),
                        {
                            "message": raced_rekey_plan,
                            "published": False,
                            "enqueued_at": now_iso(),
                        },
                    )
                else:
                    txn.set(
                        layout.completion_evidence_ref(db, workflow_id),
                        {
                            "message": writes.completion_evidence["message"],
                            "recorded_at": now_iso(),
                        },
                    )
            if rekey_plan is not None:
                txn.create(
                    layout.outbox_ref(db, workflow_id, rekey_plan["envelope"]["event_id"]),
                    {"message": rekey_plan, "published": False, "enqueued_at": now_iso()},
                )
            for claim in writes.work_package_claims:
                registry.claim_work_package(
                    txn,
                    db,
                    workflow_id=workflow_id,
                    trace_id=envelope["trace_id"],
                    **claim,
                )
            for reassignment, plan in zip(writes.reassignments, plans, strict=True):
                registry.apply_reassignment(
                    txn, db, workflow_id=workflow_id, **reassignment, plan=plan
                )
            for update, plan in zip(writes.work_package_status_updates, status_plans, strict=True):
                registry.apply_wp_status_update(
                    txn,
                    db,
                    workflow_id=workflow_id,
                    **update,
                    plan=plan,
                    trace_id=envelope["trace_id"],
                )
            if (
                writes.owned_effects is not None
                and owned_plan is not None
                and owned_plan.get("reason") == "owner_failed"
            ):
                # AUD-1: the refusal is visible in the trail — a FAILED
                # owner's late success must neither land nor vanish silently
                refusal = build_audit_event(
                    workflow_id=workflow_id,
                    trace_id=envelope["trace_id"],
                    agent_identity=owned_plan["owner"],
                    event_kind="blocked_action",
                    reason_code="RESULT_REFUSED_OWNER_FAILED",
                    input_obj={
                        "work_package_id": writes.owned_effects["work_package_id"],
                        "instance_id": owned_plan["owner"],
                    },
                    output_obj={"refused": True},
                    effective_at=int(owned_plan.get("logical_now", 0)),
                )
                txn.create(
                    layout.audit_ref(db, workflow_id, refusal["envelope"]["event_id"]), refusal
                )
            if (
                writes.owned_effects is not None
                and owned_plan is not None
                and not owned_plan["skip"]
            ):
                registry.apply_wp_status_update(
                    txn,
                    db,
                    workflow_id=workflow_id,
                    work_package_id=writes.owned_effects["work_package_id"],
                    status=writes.owned_effects["status"],
                    plan=owned_plan,
                    trace_id=envelope["trace_id"],
                )
                for audit in writes.owned_effects.get("audit_events", []):
                    txn.create(
                        layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]),
                        audit,
                    )
                for out in writes.owned_effects.get("outbox_messages", []):
                    txn.create(
                        layout.outbox_ref(db, workflow_id, out["envelope"]["event_id"]),
                        {"message": out, "published": False, "enqueued_at": now_iso()},
                    )
            if writes.failure_disposition is not None and disposition_plan is not None:
                registry.apply_failure_disposition(
                    txn,
                    db,
                    workflow_id=workflow_id,
                    **writes.failure_disposition,
                    plan=disposition_plan,
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
