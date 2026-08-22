"""Day 2 unit gate (CI-3): state machine, human gate, idempotent messaging,
Logical Clock double-fire, and audit reconstruction (ORC-5, HUM-1, ICD-5/6,
AUD-1..3, DAT-2)."""

import pytest

from fake_firestore import FakeFirestore
from forge_common import clock, state
from forge_common.audit import build_audit_event
from forge_common.bus import (
    TxnWrites,
    UntrustedMessageRejected,
    drain_outbox,
    process_message,
)
from forge_common.clock import advance_clock, build_due_event, emit_due_events
from forge_common.messages import (
    build_envelope,
    deterministic_event_id,
    deterministic_trace_id,
)

WF = "wf-gx12-07-hyd-001"
TRACE = deterministic_trace_id(WF)


def make_workflow(db, logical_time=0):
    return state.create_workflow(
        db,
        workflow_id=WF,
        equipment_id="GX12-07",
        trace_id=TRACE,
        logical_time=logical_time,
    )


def record_approval(db, action_type, decision="approved", approval_id=None):
    """Record an authoritative approval_decision.v2 (as the IAP surface would)."""
    apr_id = approval_id or f"apr-{action_type.replace('_', '-')}-0001"
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="approval_decision.v2",
            event_id=deterministic_event_id("apr", WF, apr_id),
            trace_id=TRACE,
            idempotency_key=f"idem-{apr_id}",
        ),
        "payload": {
            "approval_id": apr_id,
            "action_type": action_type,
            "decision": decision,
            "approver_identity": "approver@example.test",
            "decided_at": "2026-08-21T12:00:00Z",
        },
    }
    return state.record_approval_decision(db, message)


def advance_to(db, target_chain, **kwargs):
    for target in target_chain:
        state.transition_workflow(
            db,
            workflow_id=WF,
            target=target,
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="STATE_ADVANCE",
            **(kwargs if target == target_chain[-1] else {}),
        )


def test_create_and_transition_writes_state_audit_atomically():
    db = FakeFirestore()
    doc = make_workflow(db)
    assert doc["status"] == "INTAKE"
    state.transition_workflow(
        db,
        workflow_id=WF,
        target="PLANNING",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="DECOMPOSED",
    )
    stored = db.collection("workflows").document(WF).get().to_dict()
    assert stored["status"] == "PLANNING"
    trail = state.reconstruct_audit_trail(db, WF)
    assert [e["payload"]["state_after"] for e in trail] == ["INTAKE", "PLANNING"]


def test_invalid_transition_rejected():
    db = FakeFirestore()
    make_workflow(db)
    with pytest.raises(state.InvalidTransition):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target="RELEASED",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="SKIP",
        )


def test_gated_transition_blocked_then_allowed():
    """CI-4 seed assertion: blocked before approval, succeeds after (HUM-1)."""
    db = FakeFirestore()
    make_workflow(db)
    advance_to(db, ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"])
    with pytest.raises(state.GateBlocked):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target="SUSPENDED_AWAITING_PART",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="SCHEDULE_OVERRIDE",
        )
    with pytest.raises(state.GateBlocked):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target="SUSPENDED_AWAITING_PART",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="SCHEDULE_OVERRIDE",
            approval_id=record_approval(
                db,
                "schedule_override",
                decision="rejected",
                approval_id="apr-schedule-override-rej1",
            ),
        )
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="SCHEDULE_OVERRIDE",
        approval_id=record_approval(db, "schedule_override"),
        due_at=21,
    )
    assert doc["status"] == "SUSPENDED_AWAITING_PART"
    assert doc["due_at"] == 21
    trail = state.reconstruct_audit_trail(db, WF)
    assert trail[-1]["payload"]["event_kind"] == "approval"


def test_release_gate_never_unattended():
    """Second HUM-1 gate: RELEASED requires its own equipment_release approval."""
    db = FakeFirestore()
    make_workflow(db)
    advance_to(db, ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"])
    state.transition_workflow(
        db,
        workflow_id=WF,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="SCHEDULE_OVERRIDE",
        approval_id=record_approval(db, "schedule_override"),
        due_at=21,
    )
    advance_to(db, ["ASSEMBLY_RESUMED", "AWAITING_RELEASE_APPROVAL"])
    with pytest.raises(state.GateBlocked):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target="RELEASED",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="EQUIPMENT_RELEASE",
            approval_id="apr-schedule-override-0001",  # wrong action_type for release
        )
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="RELEASED",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="EQUIPMENT_RELEASE",
        approval_id=record_approval(db, "equipment_release"),
    )
    assert doc["status"] == "RELEASED"


def test_duplicate_delivery_no_duplicate_side_effects():
    """ICD-5/ICD-6: same idempotency_key processed at most once."""
    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)
    calls = []

    def handler(msg, writes: TxnWrites):
        calls.append(msg["envelope"]["event_id"])
        writes.audit_events.append(
            build_audit_event(
                workflow_id=WF,
                trace_id=TRACE,
                agent_identity="forge-orchestrator",
                event_kind="decision",
                reason_code="DUE_PROCESSED",
                input_obj=msg["payload"],
                output_obj={"ok": True},
                effective_at=msg["payload"]["due_at_logical"],
            )
        )

    first = process_message(db, message, handler, consumer_identity="forge-orchestrator")
    second = process_message(db, message, handler, consumer_identity="forge-orchestrator")
    assert first is True and second is False
    assert len(calls) == 1
    audit_docs = list(db.collection("workflows").document(WF).collection("audit").stream())
    kinds = [d.to_dict()["payload"]["event_kind"] for d in audit_docs]
    assert kinds.count("decision") == 1


def test_untrusted_message_rejected_for_non_cyber_trust():
    """DAT-2: only Cyber Trust may consume non-TRUSTED messages."""
    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)
    message["envelope"]["trust_state"] = "UNSCREENED"
    with pytest.raises(UntrustedMessageRejected):
        process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")
    assert (
        process_message(db, message, lambda m, w: None, consumer_identity="forge-cyber-trust")
        is True
    )


def test_clock_double_fire_single_due_event():
    """ORC-5: double-fired advance emits exactly one due event."""
    db = FakeFirestore()
    make_workflow(db)
    advance_to(db, ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"])
    state.transition_workflow(
        db,
        workflow_id=WF,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="SCHEDULE_OVERRIDE",
        approval_id=record_approval(db, "schedule_override"),
        due_at=21,
    )
    new_time = advance_clock(db, 21)
    assert new_time == 21
    first = emit_due_events(db)
    second = emit_due_events(db)
    assert first == [WF] and second == []
    outbox = list(db.collection("workflows").document(WF).collection("outbox").stream())
    assert len(outbox) == 1
    assert clock.read_clock(db) == 21


def test_drain_outbox_orders_by_workflow_and_marks_published():
    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)
    state.transition_workflow(
        db,
        workflow_id=WF,
        target="PLANNING",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="DECOMPOSED",
        outbox_messages=[message],
    )
    sent = []
    count = drain_outbox(db, WF, lambda msg, key: sent.append((key, msg)))
    assert count == 1 and sent[0][0] == WF
    assert drain_outbox(db, WF, lambda msg, key: sent.append((key, msg))) == 0


def test_handler_failure_leaves_no_partial_writes():
    """AUD-2: state + audit + inbox commit together or not at all."""
    db = FakeFirestore()
    make_workflow(db)
    before = dict(db.store)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)

    def broken(msg, writes: TxnWrites):
        writes.audit_events.append(
            build_audit_event(
                workflow_id=WF,
                trace_id=TRACE,
                agent_identity="forge-orchestrator",
                event_kind="decision",
                reason_code="WILL_FAIL",
                input_obj={},
                output_obj={},
                effective_at=0,
            )
        )
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        process_message(db, message, broken, consumer_identity="forge-orchestrator")
    changed = {k: v for k, v in db.store.items() if db.store.get(k) != before.get(k)}
    assert all(path[-2] == "inbox" for path in changed), changed  # only the claim
    audits = list(db.collection("workflows").document(WF).collection("audit").stream())
    assert len(audits) == 1  # only WORKFLOW_CREATED
    # The released claim does not block redelivery:
    assert (
        process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")
        is True
    )


def test_audit_trail_reconstructs_across_time_skip():
    """AUD-2/AUD-3: full trail from Firestore alone, dual timestamps ordered."""
    db = FakeFirestore()
    make_workflow(db)
    advance_to(db, ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"])
    state.transition_workflow(
        db,
        workflow_id=WF,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="SCHEDULE_OVERRIDE",
        approval_id=record_approval(db, "schedule_override"),
        due_at=21,
    )
    trail = state.reconstruct_audit_trail(db, WF)
    states = [e["payload"]["state_after"] for e in trail if "state_after" in e["payload"]]
    assert states == [
        "INTAKE",
        "PLANNING",
        "VALIDATING",
        "AWAITING_SCHEDULE_APPROVAL",
        "SUSPENDED_AWAITING_PART",
    ]
    for event in trail:
        assert event["payload"]["observed_at"].endswith("Z")
        assert isinstance(event["payload"]["effective_at"], int)
        assert event["envelope"]["data_origin"] == "SYNTHETIC"


def suspend(db, due_at=21):
    advance_to(db, ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"])
    return state.transition_workflow(
        db,
        workflow_id=WF,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="SCHEDULE_OVERRIDE",
        approval_id=record_approval(db, "schedule_override"),
        due_at=due_at,
    )


def test_post_skip_events_carry_advanced_effective_at():
    """AUD-3/ORC-5: after the 21-day skip, resume events stamp day 21."""
    db = FakeFirestore()
    make_workflow(db)
    suspend(db, due_at=21)
    assert advance_clock(db, 21) == 21
    emit_due_events(db)
    advance_to(db, ["ASSEMBLY_RESUMED", "AWAITING_RELEASE_APPROVAL"])
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="RELEASED",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="EQUIPMENT_RELEASE",
        approval_id=record_approval(db, "equipment_release"),
    )
    assert doc["logical_time"] == 21
    trail = state.reconstruct_audit_trail(db, WF)
    by_state = {
        e["payload"]["state_after"]: e["payload"]["effective_at"]
        for e in trail
        if "state_after" in e["payload"]
    }
    assert by_state["SUSPENDED_AWAITING_PART"] == 0
    assert by_state["ASSEMBLY_RESUMED"] == 21
    assert by_state["RELEASED"] == 21
    assert trail[-1]["payload"]["state_after"] == "RELEASED"


def test_approval_evidence_reconstructs_from_firestore(tmp_path=None):
    """HUM-1: approver_identity, action, decision recoverable from the trail."""
    import json as _json

    db = FakeFirestore()
    make_workflow(db)
    suspend(db, due_at=21)
    trail = state.reconstruct_audit_trail(db, WF)
    evt = trail[-1]
    assert evt["payload"]["event_kind"] == "approval"
    evidence = _json.loads(evt["payload"]["detail"])["approval"]
    assert evidence["approver_identity"] == "approver@example.test"
    assert evidence["action_type"] == "schedule_override"
    assert evidence["decision"] == "approved"
    assert evidence["approval_id"].startswith("apr-")
    assert evidence["decided_at"]


def test_replayed_approval_cannot_authorize_second_transition():
    """HUM-1: an approval is consumed once; replay raises GateBlocked."""
    db = FakeFirestore()
    make_workflow(db)
    suspend(db, due_at=21)
    advance_to(db, ["ASSEMBLY_RESUMED", "BLOCKED_AGENT_FAILURE"])
    advance_to(db, ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"])
    with pytest.raises(state.GateBlocked, match="already consumed"):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target="SUSPENDED_AWAITING_PART",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="SCHEDULE_OVERRIDE",
            approval_id="apr-schedule-override-0001",  # already recorded AND consumed
            due_at=30,
        )
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="SCHEDULE_OVERRIDE",
        approval_id=record_approval(
            db, "schedule_override", approval_id="apr-schedule-override-0002"
        ),
        due_at=30,
    )
    assert doc["due_at"] == 30


def test_suspension_requires_due_at_and_rejects_stray_due_at():
    """ORC-5: no unscheduled suspensions; due_at only valid when suspending."""
    db = FakeFirestore()
    make_workflow(db)
    advance_to(db, ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"])
    with pytest.raises(state.InvalidTransition, match="due_at"):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target="SUSPENDED_AWAITING_PART",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="SCHEDULE_OVERRIDE",
            approval_id=record_approval(db, "schedule_override"),
        )
    with pytest.raises(state.InvalidTransition, match="due_at"):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target="PLANNING",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="STATE_ADVANCE",
            due_at=5,
        )


def test_rejected_release_has_rework_exit():
    """HUM-1 reject path: AWAITING_RELEASE_APPROVAL -> ASSEMBLY_RESUMED."""
    db = FakeFirestore()
    make_workflow(db)
    suspend(db, due_at=21)
    advance_to(db, ["ASSEMBLY_RESUMED", "AWAITING_RELEASE_APPROVAL"])
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="ASSEMBLY_RESUMED",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="RELEASE_REJECTED_REWORK",
    )
    assert doc["status"] == "ASSEMBLY_RESUMED"


def test_fanout_consumers_each_process_once():
    """ICD-5: inbox markers are consumer-scoped — no cross-consumer lockout."""
    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)
    ran = []
    handler = lambda m, w: ran.append(1)  # noqa: E731
    assert process_message(db, message, handler, consumer_identity="forge-orchestrator")
    assert process_message(db, message, handler, consumer_identity="forge-safety")
    assert process_message(db, message, handler, consumer_identity="forge-orchestrator") is False
    assert len(ran) == 2


def test_bus_transition_path_enforces_gates():
    """SPINE-3 regression: handlers cannot bypass the table or HUM-1 gates."""
    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)

    def illegal(msg, writes: TxnWrites):
        writes.transition = {
            "target": "RELEASED",
            "agent_identity": "forge-orchestrator",
            "trace_id": TRACE,
            "reason_code": "SNEAKY_RELEASE",
        }

    with pytest.raises(state.InvalidTransition):
        process_message(db, message, illegal, consumer_identity="forge-orchestrator")
    stored = db.collection("workflows").document(WF).get().to_dict()
    assert stored["status"] == "INTAKE"

    def legal(msg, writes: TxnWrites):
        writes.transition = {
            "target": "PLANNING",
            "agent_identity": "forge-orchestrator",
            "trace_id": TRACE,
            "reason_code": "DECOMPOSED",
        }

    assert process_message(db, message, legal, consumer_identity="forge-orchestrator")
    assert db.collection("workflows").document(WF).get().to_dict()["status"] == "PLANNING"
    trail = state.reconstruct_audit_trail(db, WF)
    assert trail[-1]["payload"]["state_after"] == "PLANNING"


def test_untrusted_rejection_is_audited():
    """ICD-2/DAT-2: rejection writes a blocked_action audit event."""
    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)
    message["envelope"]["trust_state"] = "UNSCREENED"
    with pytest.raises(UntrustedMessageRejected):
        process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")
    trail = state.reconstruct_audit_trail(db, WF)
    blocked = [e for e in trail if e["payload"]["event_kind"] == "blocked_action"]
    assert len(blocked) == 1
    assert blocked[0]["payload"]["reason_code"] == "UNTRUSTED_MESSAGE_REJECTED"


def test_drain_outbox_publishes_in_enqueue_order():
    """ICD-5: publish order is enqueue order, not document-ID order."""
    db = FakeFirestore()
    make_workflow(db)
    first = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=7)
    second = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)
    state.transition_workflow(
        db,
        workflow_id=WF,
        target="PLANNING",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="DECOMPOSED",
        outbox_messages=[first],
    )
    state.transition_workflow(
        db,
        workflow_id=WF,
        target="VALIDATING",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="STATE_ADVANCE",
        outbox_messages=[second],
    )
    order = []
    drain_outbox(db, WF, lambda msg, key: order.append(msg["payload"]["due_at_logical"]))
    assert order == [7, 21]


def test_unroutable_malformed_message_still_audited():
    """ICD-2: a message with no usable workflow_id is rejected AND audited."""
    from forge_common.bus import UNROUTABLE_AUDIT_WORKFLOW
    from forge_common.contracts import ContractViolation

    db = FakeFirestore()
    with pytest.raises(ContractViolation):
        process_message(db, {"garbage": True}, lambda m, w: None, consumer_identity="forge-safety")
    docs = list(
        db.collection("workflows").document(UNROUTABLE_AUDIT_WORKFLOW).collection("audit").stream()
    )
    assert len(docs) == 1
    assert docs[0].to_dict()["payload"]["reason_code"] == "CONTRACT_VIOLATION_REJECTED"


def test_handler_audit_events_are_validated_and_pinned():
    """Handler-supplied audits must be contract-valid and match the workflow."""
    from forge_common.contracts import ContractViolation

    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)

    def wrong_workflow(msg, writes: TxnWrites):
        writes.audit_events.append(
            build_audit_event(
                workflow_id="wf-other-workflow-999",
                trace_id=TRACE,
                agent_identity="forge-orchestrator",
                event_kind="decision",
                reason_code="MISROUTED",
                input_obj={},
                output_obj={},
                effective_at=0,
            )
        )

    before = dict(db.store)
    with pytest.raises(ContractViolation, match="workflow"):
        process_message(db, message, wrong_workflow, consumer_identity="forge-safety")
    changed = {k for k in db.store if db.store.get(k) != before.get(k)}
    assert all(path[-2] == "inbox" for path in changed)  # only the released claim
    assert not any(path[-2] == "audit" and path[1] != WF for path in db.store)
    audits = list(db.collection("workflows").document(WF).collection("audit").stream())
    assert len(audits) == 1  # only WORKFLOW_CREATED — the misrouted audit never landed


def test_gate_requires_recorded_approval():
    """HUM-1: an approval_id with no recorded decision does not pass."""
    db = FakeFirestore()
    make_workflow(db)
    advance_to(db, ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"])
    with pytest.raises(state.GateBlocked, match="no recorded approval"):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target="SUSPENDED_AWAITING_PART",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="SCHEDULE_OVERRIDE",
            approval_id="apr-never-recorded-0001",
            due_at=21,
        )


def test_clock_advance_is_audited():
    """AUD-1: the Logical Clock jump itself is reconstructable from Firestore."""
    from forge_common.clock import CLOCK_AUDIT_WORKFLOW

    db = FakeFirestore()
    advance_clock(db, 21)
    trail = state.reconstruct_audit_trail(db, CLOCK_AUDIT_WORKFLOW)
    assert len(trail) == 1
    assert trail[0]["payload"]["reason_code"] == "CLOCK_ADVANCED"
    assert trail[0]["payload"]["effective_at"] == 21


def test_due_events_recheck_state_transactionally():
    """A workflow that left suspension between scan and enqueue emits nothing."""
    db = FakeFirestore()
    make_workflow(db)
    suspend(db, due_at=5)
    advance_clock(db, 5)
    # Legitimate resume before the emitter runs: due event no longer valid.
    advance_to(db, ["ASSEMBLY_RESUMED"])
    assert emit_due_events(db) == []


def test_concurrent_delivery_single_handler_execution():
    """Claim/lease: a second worker cannot run the handler concurrently."""
    from forge_common.bus import DeliveryInProgress

    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)
    runs = []

    def worker_b_interleaved(msg, writes: TxnWrites):
        # Worker A's handler is mid-flight; worker B sees the live claim.
        runs.append("A")
        with pytest.raises(DeliveryInProgress):
            process_message(
                db, msg, lambda m, w: runs.append("B"), consumer_identity="forge-supply"
            )

    assert (
        process_message(db, message, worker_b_interleaved, consumer_identity="forge-supply") is True
    )
    assert runs == ["A"]


def test_expired_lease_is_taken_over():
    """A crashed holder's expired claim is claimed by the next delivery."""
    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)
    ran = []
    # Simulate a crashed worker: claim exists, lease already expired.
    with pytest.raises(RuntimeError):
        process_message(
            db,
            message,
            lambda m, w: (_ for _ in ()).throw(RuntimeError("crash")),
            consumer_identity="forge-supply",
        )
    assert (
        process_message(db, message, lambda m, w: ran.append(1), consumer_identity="forge-supply")
        is True
    )
    assert ran == [1]


def test_lost_claim_discards_effect_plan():
    """A holder whose claim was taken over must not commit its stale plan."""
    db = FakeFirestore()
    make_workflow(db)
    message = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=21)

    def usurped(msg, writes: TxnWrites):
        # While this handler runs, its lease "expires" and another worker
        # takes over and completes. Simulate by aging the lease, then letting
        # a second worker finish first.
        for path in list(db.store):
            if path[-2] == "inbox":
                db.store[path]["lease_expires_at"] = "2000-01-01T00:00:00.000000Z"
        assert process_message(db, msg, lambda m, w: None, consumer_identity="forge-supply") is True
        writes.audit_events.append(
            build_audit_event(
                workflow_id=WF,
                trace_id=TRACE,
                agent_identity="forge-supply",
                event_kind="decision",
                reason_code="STALE_PLAN",
                input_obj={},
                output_obj={},
                effective_at=0,
            )
        )

    assert process_message(db, message, usurped, consumer_identity="forge-supply") is False
    audits = list(db.collection("workflows").document(WF).collection("audit").stream())
    kinds = [d.to_dict()["payload"]["reason_code"] for d in audits]
    assert "STALE_PLAN" not in kinds


def test_rejected_approval_recording_is_audited():
    """AUD-1: a rejected decision appears in the trail even with no transition."""
    import json as _json

    db = FakeFirestore()
    make_workflow(db)
    record_approval(db, "schedule_override", decision="rejected", approval_id="apr-rej-0001")
    trail = state.reconstruct_audit_trail(db, WF)
    recorded = [e for e in trail if e["payload"]["reason_code"] == "APPROVAL_RECORDED"]
    assert len(recorded) == 1
    evidence = _json.loads(recorded[0]["payload"]["detail"])["approval"]
    assert evidence["decision"] == "rejected"
    assert evidence["approver_identity"] == "approver@example.test"


def test_untrusted_approval_record_rejected():
    """DAT-2: the approval writer refuses non-TRUSTED records."""
    db = FakeFirestore()
    make_workflow(db)
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="approval_decision.v2",
            event_id=deterministic_event_id("apr", WF, "apr-bad-0001"),
            trace_id=TRACE,
            idempotency_key="idem-apr-bad-0001",
            trust_state="UNSCREENED",
        ),
        "payload": {
            "approval_id": "apr-bad-0001",
            "action_type": "schedule_override",
            "decision": "approved",
            "approver_identity": "approver@example.test",
            "decided_at": "2026-08-21T12:00:00Z",
        },
    }
    with pytest.raises(state.GateBlocked, match="TRUSTED"):
        state.record_approval_decision(db, message)


def test_rejection_audit_fallbacks_are_pattern_safe():
    """A wf- prefixed but invalid workflow_id and a 32-char non-hex trace
    must not break the rejection audit itself."""
    from forge_common.bus import UNROUTABLE_AUDIT_WORKFLOW
    from forge_common.contracts import ContractViolation

    db = FakeFirestore()
    bad = {
        "envelope": {
            "workflow_id": "wf-UPPER_INVALID!!",
            "trace_id": "Z" * 32,  # 32 chars, not hex
        }
    }
    with pytest.raises(ContractViolation):
        process_message(db, bad, lambda m, w: None, consumer_identity="forge-safety")
    docs = list(
        db.collection("workflows").document(UNROUTABLE_AUDIT_WORKFLOW).collection("audit").stream()
    )
    assert len(docs) == 1
    assert docs[0].to_dict()["envelope"]["trace_id"] == "0" * 32


def test_max_length_approval_comment_fits_audit_detail():
    """A 1000-char comment must not overflow the audit detail cap."""
    import json as _json

    db = FakeFirestore()
    make_workflow(db)
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="approval_decision.v2",
            event_id=deterministic_event_id("apr", WF, "apr-long-0001"),
            trace_id=TRACE,
            idempotency_key="idem-apr-long-0001",
        ),
        "payload": {
            "approval_id": "apr-long-0001",
            "action_type": "schedule_override",
            "decision": "approved",
            "approver_identity": "approver@example.test",
            "decided_at": "2026-08-21T12:00:00Z",
            "comment": "x" * 1000,
        },
    }
    state.record_approval_decision(db, message)  # validates the audit it writes
    trail = state.reconstruct_audit_trail(db, WF)
    recorded = [e for e in trail if e["payload"]["reason_code"] == "APPROVAL_RECORDED"]
    detail = _json.loads(recorded[0]["payload"]["detail"])
    assert len(recorded[0]["payload"]["detail"]) <= 1000
    assert detail["approval"]["comment_omitted"] is True
    assert detail["approval"]["approver_identity"] == "approver@example.test"
