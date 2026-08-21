"""Day 2 exit gate: integration against the REAL Firestore and Pub/Sub
emulators (ICD-5, ICD-6, ORC-5, AUD-2, HUM-1).

These tests exercise the real google-cloud clients — including actual
Transaction semantics (generator-returning ``Transaction.get``, retry
behavior) that the in-memory fake can only approximate — plus the ordered
publisher, subscription pull, redelivery dedupe, and the DLQ policy.

Skipped unless FIRESTORE_EMULATOR_HOST and PUBSUB_EMULATOR_HOST are set;
the pr-gate CI job starts both emulators (Java is available on the runner).
Known emulator limit: the Pub/Sub emulator stores but does not enforce
dead-letter forwarding — forwarding itself is verified in Lane 2 against
real infrastructure (CI-6).
"""

import json
import os
import uuid

import pytest

from forge_common import state
from forge_common.bus import drain_outbox, process_message
from forge_common.clock import advance_clock, build_due_event, emit_due_events
from forge_common.messages import build_envelope, deterministic_event_id, deterministic_trace_id
from forge_common.pubsub import OrderedPublisher, ensure_topic_and_subscription

pytestmark = pytest.mark.skipif(
    not (os.environ.get("FIRESTORE_EMULATOR_HOST") and os.environ.get("PUBSUB_EMULATOR_HOST")),
    reason="Firestore/Pub/Sub emulators not running",
)

PROJECT = "demo-forge"


@pytest.fixture()
def db():
    from google.auth.credentials import AnonymousCredentials
    from google.cloud import firestore

    return firestore.Client(project=PROJECT, credentials=AnonymousCredentials())


def unique_wf():
    return f"wf-emu-{uuid.uuid4().hex[:12]}"


def record_approval(db, workflow_id, action_type, approval_id):
    trace = deterministic_trace_id(workflow_id)
    message = {
        "envelope": build_envelope(
            workflow_id=workflow_id,
            schema_version="approval_decision.v2",
            event_id=deterministic_event_id("apr", workflow_id, approval_id),
            trace_id=trace,
            idempotency_key=f"idem-{approval_id}",
        ),
        "payload": {
            "approval_id": approval_id,
            "action_type": action_type,
            "decision": "approved",
            "approver_identity": "approver@example.test",
            "decided_at": "2026-08-21T12:00:00Z",
        },
    }
    return state.record_approval_decision(db, message)


def test_real_transactions_state_gate_dedupe_and_reconstruction(db):
    """The generator-get regression test, on the real client (AUD-2, HUM-1)."""
    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-07", trace_id=trace, logical_time=0
    )
    for target, reason in [("PLANNING", "DECOMPOSED"), ("VALIDATING", "STATE_ADVANCE")]:
        state.transition_workflow(
            db,
            workflow_id=wf,
            target=target,
            agent_identity="forge-orchestrator",
            trace_id=trace,
            reason_code=reason,
        )
    state.transition_workflow(
        db,
        workflow_id=wf,
        target="AWAITING_SCHEDULE_APPROVAL",
        agent_identity="forge-orchestrator",
        trace_id=trace,
        reason_code="STATE_ADVANCE",
    )
    with pytest.raises(state.GateBlocked):
        state.transition_workflow(
            db,
            workflow_id=wf,
            target="SUSPENDED_AWAITING_PART",
            agent_identity="forge-orchestrator",
            trace_id=trace,
            reason_code="SCHEDULE_OVERRIDE",
            due_at=21,
        )
    apr = record_approval(db, wf, "schedule_override", f"apr-so-{uuid.uuid4().hex[:8]}")
    doc = state.transition_workflow(
        db,
        workflow_id=wf,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=trace,
        reason_code="SCHEDULE_OVERRIDE",
        approval_id=apr,
        due_at=21,
    )
    assert doc["status"] == "SUSPENDED_AWAITING_PART"

    # Consumer dedupe on the real client (ICD-5/6).
    message = build_due_event(workflow_id=wf, trace_id=trace, due_at=21)
    ran = []
    assert (
        process_message(
            db, message, lambda m, w: ran.append(1), consumer_identity="forge-orchestrator"
        )
        is True
    )
    assert (
        process_message(
            db, message, lambda m, w: ran.append(1), consumer_identity="forge-orchestrator"
        )
        is False
    )
    assert len(ran) == 1

    trail = state.reconstruct_audit_trail(db, wf)
    states = [e["payload"]["state_after"] for e in trail if "state_after" in e["payload"]]
    assert states[0] == "INTAKE" and states[-1] == "SUSPENDED_AWAITING_PART"
    evidence = json.loads(trail[-1]["payload"]["detail"])["approval"]
    assert evidence["approver_identity"] == "approver@example.test"


def test_due_event_double_fire_on_real_client(db):
    """ORC-5 exit criterion on the real client: double-fire emits once."""
    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-08", trace_id=trace, logical_time=0
    )
    for target in ["PLANNING", "VALIDATING", "AWAITING_SCHEDULE_APPROVAL"]:
        state.transition_workflow(
            db,
            workflow_id=wf,
            target=target,
            agent_identity="forge-orchestrator",
            trace_id=trace,
            reason_code="STATE_ADVANCE",
        )
    apr = record_approval(db, wf, "schedule_override", f"apr-so-{uuid.uuid4().hex[:8]}")
    state.transition_workflow(
        db,
        workflow_id=wf,
        target="SUSPENDED_AWAITING_PART",
        agent_identity="forge-orchestrator",
        trace_id=trace,
        reason_code="SCHEDULE_OVERRIDE",
        approval_id=apr,
        due_at=21,
    )
    advance_clock(db, 21)
    first = emit_due_events(db, trace_id=trace)
    second = emit_due_events(db, trace_id=trace)
    assert wf in first and wf not in second


def test_ordered_publish_and_pull():
    """ICD-5: ordering key preserves publish order end to end."""
    from google.cloud import pubsub_v1

    topic = f"forge-bus-{uuid.uuid4().hex[:8]}"
    sub = f"{topic}-sub"
    ensure_topic_and_subscription(PROJECT, topic, sub)
    publisher = OrderedPublisher(PROJECT)
    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    for due in (3, 7, 21):
        publisher.publish(topic, build_due_event(workflow_id=wf, trace_id=trace, due_at=due), wf)
    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(PROJECT, sub)
    received = []
    while len(received) < 3:
        response = subscriber.pull(request={"subscription": sub_path, "max_messages": 10})
        for rm in response.received_messages:
            received.append(json.loads(rm.message.data)["payload"]["due_at_logical"])
            subscriber.acknowledge(request={"subscription": sub_path, "ack_ids": [rm.ack_id]})
    assert received == [3, 7, 21]


def test_drain_redeliver_dedupe_end_to_end(db):
    """Outbox -> Pub/Sub -> nack -> redelivery -> inbox dedupe (ICD-5/6)."""
    from google.cloud import pubsub_v1

    topic = f"forge-bus-{uuid.uuid4().hex[:8]}"
    sub = f"{topic}-sub"
    ensure_topic_and_subscription(PROJECT, topic, sub)
    publisher = OrderedPublisher(PROJECT)
    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-09", trace_id=trace, logical_time=0
    )
    message = build_due_event(workflow_id=wf, trace_id=trace, due_at=21)
    state.transition_workflow(
        db,
        workflow_id=wf,
        target="PLANNING",
        agent_identity="forge-orchestrator",
        trace_id=trace,
        reason_code="DECOMPOSED",
        outbox_messages=[message],
    )
    assert drain_outbox(db, wf, lambda m, key: publisher.publish(topic, m, key)) == 1

    subscriber = pubsub_v1.SubscriberClient()
    sub_path = subscriber.subscription_path(PROJECT, sub)
    processed = []

    def deliver_once(ack: bool) -> None:
        response = subscriber.pull(request={"subscription": sub_path, "max_messages": 1})
        for rm in response.received_messages:
            msg = json.loads(rm.message.data)
            processed.append(
                process_message(db, msg, lambda m, w: None, consumer_identity="forge-supply")
            )
            request = {"subscription": sub_path, "ack_ids": [rm.ack_id]}
            if ack:
                subscriber.acknowledge(request=request)
            else:
                subscriber.modify_ack_deadline(request={**request, "ack_deadline_seconds": 0})

    deliver_once(ack=False)  # processed, then nacked -> transport redelivers
    deliver_once(ack=True)  # redelivery -> inbox marker dedupes
    assert processed == [True, False]


def test_subscription_carries_dlq_policy():
    """ICD-5: subscriptions declare the 5-attempt dead-letter policy."""
    from google.cloud import pubsub_v1

    topic = f"forge-bus-{uuid.uuid4().hex[:8]}"
    ensure_topic_and_subscription(PROJECT, topic, f"{topic}-sub", dead_letter_topic=f"{topic}-dlq")
    subscriber = pubsub_v1.SubscriberClient()
    config = subscriber.get_subscription(
        request={"subscription": subscriber.subscription_path(PROJECT, f"{topic}-sub")}
    )
    assert config.dead_letter_policy.max_delivery_attempts == 5
    assert config.dead_letter_policy.dead_letter_topic.endswith(f"{topic}-dlq")
    assert config.enable_message_ordering


def test_concurrent_claim_contention_real_client(db):
    """Two workers contend for one delivery on REAL Firestore: the second
    sees the live claim and must NACK; the handler runs exactly once."""
    import threading

    from forge_common.bus import DeliveryInProgress

    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-10", trace_id=trace, logical_time=0
    )
    message = build_due_event(workflow_id=wf, trace_id=trace, due_at=21)
    entered, release = threading.Event(), threading.Event()
    runs, results = [], {}

    def slow_handler(msg, writes):
        runs.append("A")
        entered.set()
        assert release.wait(timeout=30)

    def worker_a():
        results["a"] = process_message(db, message, slow_handler, consumer_identity="forge-supply")

    thread = threading.Thread(target=worker_a)
    thread.start()
    assert entered.wait(timeout=30)
    # Worker B arrives while A's handler is mid-flight and A's lease is live.
    with pytest.raises(DeliveryInProgress):
        process_message(
            db, message, lambda m, w: runs.append("B"), consumer_identity="forge-supply"
        )
    release.set()
    thread.join(timeout=30)
    assert results["a"] is True
    assert runs == ["A"]
    # After completion the marker is done: duplicates return False.
    assert (
        process_message(db, message, lambda m, w: None, consumer_identity="forge-supply") is False
    )


def test_expired_lease_takeover_discards_stale_plan_real_client(db):
    """On REAL Firestore: a worker whose lease expired mid-handler loses the
    claim; the takeover worker's plan commits, the stale plan is discarded."""
    import threading

    from forge_common.audit import build_audit_event

    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-11", trace_id=trace, logical_time=0
    )
    message = build_due_event(workflow_id=wf, trace_id=trace, due_at=21)
    a_entered, a_release = threading.Event(), threading.Event()
    results = {}

    def audit_for(reason):
        return build_audit_event(
            workflow_id=wf,
            trace_id=trace,
            agent_identity="forge-supply",
            event_kind="decision",
            reason_code=reason,
            input_obj={},
            output_obj={},
            effective_at=0,
        )

    def stale_handler(msg, writes):
        a_entered.set()
        assert a_release.wait(timeout=30)
        writes.audit_events.append(audit_for("STALE_PLAN"))

    def worker_a():
        # Zero-second lease: expired the moment it is granted.
        results["a"] = process_message(
            db, message, stale_handler, consumer_identity="forge-supply", lease_seconds=0
        )

    thread = threading.Thread(target=worker_a)
    thread.start()
    assert a_entered.wait(timeout=30)
    # Worker B takes over the expired claim and completes while A is blocked.
    results["b"] = process_message(
        db,
        message,
        lambda m, w: w.audit_events.append(audit_for("FRESH_PLAN")),
        consumer_identity="forge-supply",
    )
    a_release.set()
    thread.join(timeout=30)
    assert results["b"] is True
    assert results["a"] is False  # stale plan discarded at commit
    reasons = [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db, wf)]
    assert "FRESH_PLAN" in reasons and "STALE_PLAN" not in reasons


def test_day3_exit_assign_and_produce_real_client(db):
    """Day 3 exit criterion: Orchestrator assigns via registry discovery and
    both specialists return contract-valid outputs — on real Firestore."""

    from services.maintenance.handlers import make_handler as make_maintenance
    from services.orchestrator.handlers import make_nmc_handler
    from services.supply.handlers import make_handler as make_supply

    from forge_common import registry
    from forge_common.contracts import validate_message
    from forge_common.messages import build_envelope, deterministic_event_id

    registry.load_registry(db)
    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-07", trace_id=trace, logical_time=0
    )
    nmc = {
        "envelope": build_envelope(
            workflow_id=wf,
            schema_version="nmc_event.v2",
            event_id=deterministic_event_id("nmc", wf),
            trace_id=trace,
            idempotency_key=f"idem-nmc-{wf}",
        ),
        "payload": {
            "equipment_id": "GX12-07",
            "discrepancy_code": "DSC-0042",
            "description": "Failed hydraulic actuator on lift assembly",
            "reported_at": "2026-08-21T09:00:00Z",
        },
    }
    from adk_stub import stub_json

    orchestrator = make_nmc_handler(
        db,
        model=stub_json(
            {
                "objectives": {
                    "maintenance": "Plan replacement of the failed actuator.",
                    "supply": "Source the approved actuator and report status.",
                }
            }
        ),
    )
    assert process_message(db, nmc, orchestrator, consumer_identity="forge-orchestrator")
    outbox_ref = db.collection("workflows").document(wf).collection("outbox")
    assignments = [
        d.to_dict()["message"]
        for d in outbox_ref.stream()
        if d.to_dict()["message"]["envelope"]["schema_version"] == "work_package_assignment.v2"
    ]
    assert len(assignments) == 2
    plan_payload = {
        "plan_id": "plan-emu-01",
        "equipment_id": "GX12-07",
        "tasks": [{"task_code": "TC-101", "title": "Replace actuator", "est_hours": 6.5}],
    }
    report_payload = {
        "part_number": "HYD-ACT-4402",
        "part_approved": True,
        "shipment_status": "delayed",
        "eta_days": 21,
    }
    for assignment in assignments:
        role = assignment["payload"]["role"]
        handler = (
            make_maintenance(db, stub_json(plan_payload))
            if role == "maintenance"
            else make_supply(db, stub_json(report_payload))
        )
        assert process_message(db, assignment, handler, consumer_identity=f"forge-{role}")
    produced = [d.to_dict()["message"] for d in outbox_ref.stream()]
    schemas = sorted(m["envelope"]["schema_version"] for m in produced)
    assert schemas.count("maintenance_action_plan.v2") == 1
    assert schemas.count("sourcing_report.v2") == 1
    for message in produced:
        validate_message(message)


def test_reg2_race_rejected_on_real_client(db):
    """Rider 1: the negative REG-2 race on REAL Firestore — a definition
    retired between discovery and commit is refused, audited, and the
    delivery stays reprocessable."""
    from forge_common import registry
    from forge_common.bus import TxnWrites
    from forge_common.registry import IneligibleAssignment

    registry.load_registry(db)
    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-07", trace_id=trace, logical_time=0
    )
    message = build_due_event(workflow_id=wf, trace_id=trace, due_at=21)

    def stale_plan(msg, writes: TxnWrites):
        ref = registry.definition_ref(db, "forge-maintenance")
        ref.set({**ref.get().to_dict(), "lifecycle_status": "RETIRED"})
        writes.work_package_claims.append(
            {
                "work_package_id": f"wp-maintenance-{wf.removeprefix('wf-')}",
                "instance_id": "agent-maintenance-01",
                "role": "maintenance",
            }
        )

    with pytest.raises(IneligibleAssignment):
        process_message(db, message, stale_plan, consumer_identity="forge-orchestrator")
    workflow = db.collection("workflows").document(wf)
    assert list(workflow.collection("work_packages").stream()) == []
    reasons = [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db, wf)]
    assert "ASSIGNMENT_INELIGIBLE" in reasons
    ref = registry.definition_ref(db, "forge-maintenance")
    ref.set({**ref.get().to_dict(), "lifecycle_status": "APPROVED"})
    assert process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")


def test_day4_exit_repair_loop_real_client(db):
    """Day 4 exit criterion: injected Workforce failure detected by the
    monitor and reassigned to the reserve exactly once — on real Firestore."""
    from services.orchestrator.handlers import make_failure_handler
    from services.orchestrator.monitor import run_monitoring_cycle

    from forge_common import layout, registry

    registry.load_registry(db)
    wf = unique_wf()
    trace = deterministic_trace_id(wf)
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-07", trace_id=trace, logical_time=0
    )
    wp_id = f"wp-workforce-{wf.removeprefix('wf-')}"

    def _claim(txn):
        registry.claim_work_package(
            txn,
            db,
            workflow_id=wf,
            work_package_id=wp_id,
            instance_id="agent-workforce-01",
            role="workforce",
            objective="Staff the plan.",
        )

    layout.run_in_transaction(db, _claim)
    wp_ref = db.collection("workflows").document(wf).collection("work_packages").document(wp_id)
    wp_ref.set({**wp_ref.get().to_dict(), "assigned_observed_at": "2026-08-21T09:00:00.000000Z"})

    now = "2026-08-21T09:10:00.000000Z"
    first = run_monitoring_cycle(db, trace_id_for=lambda w: trace, now=now)
    second = run_monitoring_cycle(db, trace_id_for=lambda w: trace, now=now)
    assert wp_id in first and second == []

    outbox = db.collection("workflows").document(wf).collection("outbox")
    timeout_event = next(
        d.to_dict()["message"]
        for d in outbox.stream()
        if d.to_dict()["message"]["envelope"]["schema_version"] == "agent_failure_event.v2"
    )
    handler = make_failure_handler(db)
    assert process_message(db, timeout_event, handler, consumer_identity="forge-orchestrator")
    assert (
        process_message(db, timeout_event, handler, consumer_identity="forge-orchestrator") is False
    )
    wp = wp_ref.get().to_dict()
    assert wp["owner_instance_id"] == "agent-workforce-02"
    assert wp["reassigned_from"] == "agent-workforce-01"
    assert wp["assignment_seq"] == 2
    kinds = [e["payload"]["event_kind"] for e in state.reconstruct_audit_trail(db, wf)]
    assert kinds.count("reassignment") == 1
