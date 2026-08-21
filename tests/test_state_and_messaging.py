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
from forge_common.messages import deterministic_trace_id

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


def approval(action_type, decision="approved", approval_id=None):
    return {
        "payload": {
            "approval_id": approval_id or f"apr-{action_type.replace('_', '-')}-0001",
            "action_type": action_type,
            "decision": decision,
            "approver_identity": "approver@example.test",
            "decided_at": "2026-08-21T12:00:00Z",
        }
    }


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
            approval=approval("schedule_override", decision="rejected"),
        )
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="SCHEDULE_OVERRIDE",
        approval=approval("schedule_override"),
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
        approval=approval("schedule_override"),
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
            approval=approval("schedule_override"),  # wrong action_type
        )
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="RELEASED",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="EQUIPMENT_RELEASE",
        approval=approval("equipment_release"),
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
        approval=approval("schedule_override"),
        due_at=21,
    )
    new_time = advance_clock(db, 21)
    assert new_time == 21
    first = emit_due_events(db, logical_time=new_time, trace_id=TRACE)
    second = emit_due_events(db, logical_time=new_time, trace_id=TRACE)
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
    assert db.store == before


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
        approval=approval("schedule_override"),
        due_at=21,
    )
    trail = state.reconstruct_audit_trail(db, WF)
    assert [e["payload"]["state_after"] for e in trail] == [
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
        approval=approval("schedule_override"),
        due_at=due_at,
    )


def test_post_skip_events_carry_advanced_effective_at():
    """AUD-3/ORC-5: after the 21-day skip, resume events stamp day 21."""
    db = FakeFirestore()
    make_workflow(db)
    suspend(db, due_at=21)
    assert advance_clock(db, 21) == 21
    emit_due_events(db, logical_time=21, trace_id=TRACE)
    advance_to(db, ["ASSEMBLY_RESUMED", "AWAITING_RELEASE_APPROVAL"])
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="RELEASED",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="EQUIPMENT_RELEASE",
        approval=approval("equipment_release"),
    )
    assert doc["logical_time"] == 21
    trail = state.reconstruct_audit_trail(db, WF)
    by_state = {e["payload"]["state_after"]: e["payload"]["effective_at"] for e in trail}
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
            approval=approval("schedule_override"),  # same approval_id as before
            due_at=30,
        )
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=TRACE,
        reason_code="SCHEDULE_OVERRIDE",
        approval=approval("schedule_override", approval_id="apr-schedule-override-0002"),
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
            approval=approval("schedule_override"),
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
