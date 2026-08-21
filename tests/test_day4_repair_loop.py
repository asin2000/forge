"""Day 4 unit gate (CI-3): data-backed Supply/Workforce/Safety boundaries,
the ORC-3 reserve repair loop, and the ORC-4 monitoring cycle."""

import pytest
from services.orchestrator.handlers import make_failure_handler
from services.orchestrator.monitor import run_monitoring_cycle
from services.safety.handlers import make_plan_validation_handler
from services.supply.handlers import make_handler as make_supply_handler
from services.workforce.handlers import make_handler as make_workforce_handler

from adk_stub import stub_json
from fake_firestore import FakeFirestore
from forge_common import layout, registry, state
from forge_common.bus import process_message
from forge_common.messages import (
    build_envelope,
    deterministic_event_id,
    deterministic_trace_id,
)

WF = "wf-gx12-07-hyd-001"
TRACE = deterministic_trace_id(WF)

NMC_INPUTS = {
    "equipment_id": "GX12-07",
    "discrepancy_code": "DSC-0042",
    "description": "Failed hydraulic actuator on lift assembly",
    "reported_at": "2026-08-21T09:00:00Z",
}


def ready_db():
    db = FakeFirestore()
    registry.load_registry(db)
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    return db


def assignment_message(role, *, inputs=None, seq=1, agent=None):
    wp_id = f"wp-{role}-{WF.removeprefix('wf-')}"
    return {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=wp_id,
            schema_version="work_package_assignment.v2",
            event_id=deterministic_event_id("t-assign", WF, wp_id, str(seq)),
            trace_id=TRACE,
            idempotency_key=f"idem-t-assign-{wp_id}-{seq:02d}",
        ),
        "payload": {
            "role": role,
            "objective": f"Test objective for {role}.",
            "assigned_agent_id": agent or f"agent-{role}-01",
            "assignment_seq": seq,
            "inputs": inputs or NMC_INPUTS,
        },
    }


def failure_message(role, agent_id, *, kind="malformed_after_retries", suffix="a"):
    wp_id = f"wp-{role}-{WF.removeprefix('wf-')}"
    event_id = deterministic_event_id("t-fail", WF, wp_id, agent_id, suffix)
    return {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=wp_id,
            schema_version="agent_failure_event.v2",
            event_id=event_id,
            trace_id=TRACE,
            idempotency_key=f"idem-t-fail-{event_id[:16]}",
        ),
        "payload": {
            "role": role,
            "agent_id": agent_id,
            "failure_kind": kind,
            "attempts": 3,
            "detail": "synthetic failure injection",
            "detected_at": "2026-08-21T10:00:00Z",
        },
    }


def claim_workforce_package(db, objective="Staff the maintenance plan."):
    def _claim(txn):
        registry.claim_work_package(
            txn,
            db,
            workflow_id=WF,
            work_package_id=f"wp-workforce-{WF.removeprefix('wf-')}",
            instance_id="agent-workforce-01",
            role="workforce",
            objective=objective,
        )

    layout.run_in_transaction(db, _claim)


def outbox_messages(db):
    return [
        d.to_dict()["message"]
        for d in db.collection("workflows").document(WF).collection("outbox").stream()
    ]


# ---------- Supply: approval is data-backed (AGT-2, rider 3) ----------


def test_supply_cannot_assert_unapproved_part():
    db = ready_db()
    lying = stub_json(
        {
            "part_number": "VND-ACT-9901",  # the vendor substitute — NOT approved
            "part_approved": True,
            "shipment_status": "ordered",
            "eta_days": 2,
        }
    )
    process_message(
        db,
        assignment_message("supply"),
        make_supply_handler(db, lying),
        consumer_identity="forge-supply",
    )
    produced = outbox_messages(db)
    assert produced[0]["envelope"]["schema_version"] == "agent_failure_event.v2"
    assert produced[0]["payload"]["failure_kind"] == "contract_violation"


def test_supply_truthful_reports_publish():
    db = ready_db()
    truthful = stub_json(
        {
            "part_number": "HYD-ACT-4402",
            "part_approved": True,  # matches the registry
            "shipment_status": "delayed",
            "eta_days": 21,
        }
    )
    process_message(
        db,
        assignment_message("supply"),
        make_supply_handler(db, truthful),
        consumer_identity="forge-supply",
    )
    report = outbox_messages(db)[0]
    assert report["envelope"]["schema_version"] == "sourcing_report.v2"
    assert report["payload"]["part_approved"] is True


def test_supply_unapproved_part_reported_honestly_publishes():
    db = ready_db()
    honest = stub_json(
        {
            "part_number": "VND-ACT-9901",
            "part_approved": False,  # honest about the registry
            "shipment_status": "not_ordered",
            "eta_days": 30,
        }
    )
    process_message(
        db,
        assignment_message("supply"),
        make_supply_handler(db, honest),
        consumer_identity="forge-supply",
    )
    report = outbox_messages(db)[0]
    assert report["envelope"]["schema_version"] == "sourcing_report.v2"
    assert report["payload"]["part_approved"] is False


# ---------- Workforce: no waivers (AGT-3) ----------


def test_workforce_qualified_roster_publishes():
    db = ready_db()
    roster = stub_json(
        {
            "assignments": [
                {
                    "task_code": "TC-101",
                    "technician_id": "T-1001",
                    "qualification_id": "Q-HYD-101",
                    "shift": "day",
                }
            ]
        }
    )
    process_message(
        db,
        assignment_message("workforce", inputs={**NMC_INPUTS, "task_codes": ["TC-101"]}),
        make_workforce_handler(db, roster),
        consumer_identity="forge-workforce",
    )
    out = outbox_messages(db)[0]
    assert out["envelope"]["schema_version"] == "roster_assignment.v2"


@pytest.mark.parametrize(
    ("technician", "qualification"),
    [
        ("T-2001", "Q-ELE-201"),  # holds no TC-101 qualification at all
        ("T-1001", "Q-ELE-201"),  # qualified, but cites the wrong record
    ],
)
def test_workforce_waiver_attempts_fail(technician, qualification):
    db = ready_db()
    waiver = stub_json(
        {
            "assignments": [
                {
                    "task_code": "TC-101",
                    "technician_id": technician,
                    "qualification_id": qualification,
                }
            ]
        }
    )
    process_message(
        db,
        assignment_message("workforce", inputs={**NMC_INPUTS, "task_codes": ["TC-101"]}),
        make_workforce_handler(db, waiver),
        consumer_identity="forge-workforce",
    )
    out = outbox_messages(db)[0]
    assert out["envelope"]["schema_version"] == "agent_failure_event.v2"
    assert out["payload"]["failure_kind"] == "contract_violation"


# ---------- Safety: data-backed verdicts (AGT-4) ----------


def plan_message(parts):
    payload = {
        "plan_id": "plan-hyd-01",
        "equipment_id": "GX12-07",
        "tasks": [
            {
                "task_code": "TC-101",
                "title": "Replace actuator",
                "est_hours": 6.5,
                "parts_required": [{"part_number": p, "qty": 1} for p in parts],
            }
        ],
    }
    return {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=f"wp-maintenance-{WF.removeprefix('wf-')}",
            schema_version="maintenance_action_plan.v2",
            event_id=deterministic_event_id("t-plan", WF, *parts),
            trace_id=TRACE,
            idempotency_key=f"idem-t-plan-{'-'.join(parts).lower()[:40]}",
        ),
        "payload": payload,
    }


def test_safety_approves_compliant_plan():
    db = ready_db()
    plan = plan_message(["HYD-ACT-4402"])
    verdict_stub = stub_json(
        {
            "subject_event_id": plan["envelope"]["event_id"],
            "verdict": "approved",
            "rule_refs": ["SP-PART-001", "SP-HRS-002"],
            "reasons": ["All parts approved; hours within bounds."],
        }
    )
    process_message(
        db, plan, make_plan_validation_handler(db, verdict_stub), consumer_identity="forge-safety"
    )
    out = outbox_messages(db)[0]
    assert out["envelope"]["schema_version"] == "validation_verdict.v2"
    assert out["payload"]["verdict"] == "approved"
    trail = state.reconstruct_audit_trail(db, WF)
    assert trail[-1]["payload"]["reason_code"] == "PLAN_APPROVED"


def test_safety_cannot_approve_unapproved_part():
    db = ready_db()
    plan = plan_message(["VND-ACT-9901"])
    lying = stub_json(
        {
            "subject_event_id": plan["envelope"]["event_id"],
            "verdict": "approved",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["Looks fine."],
        }
    )
    process_message(
        db, plan, make_plan_validation_handler(db, lying), consumer_identity="forge-safety"
    )
    out = outbox_messages(db)[0]
    assert out["envelope"]["schema_version"] == "agent_failure_event.v2"


def test_safety_vetoes_with_cited_rules():
    db = ready_db()
    plan = plan_message(["VND-ACT-9901"])
    vetoing = stub_json(
        {
            "subject_event_id": plan["envelope"]["event_id"],
            "verdict": "vetoed",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["VND-ACT-9901 is not in the approved-parts registry."],
        }
    )
    process_message(
        db, plan, make_plan_validation_handler(db, vetoing), consumer_identity="forge-safety"
    )
    out = outbox_messages(db)[0]
    assert out["payload"]["verdict"] == "vetoed"
    trail = state.reconstruct_audit_trail(db, WF)
    veto = trail[-1]["payload"]
    assert veto["event_kind"] == "veto" and veto["reason_code"] == "PLAN_VETOED"


# ---------- Repair loop (ORC-3) ----------


def test_workforce_failure_reassigns_to_reserve_exactly_once():
    db = ready_db()
    claim_workforce_package(db)
    handler = make_failure_handler(db)
    message = failure_message("workforce", "agent-workforce-01")
    assert process_message(db, message, handler, consumer_identity="forge-orchestrator")

    wp = (
        db.collection("workflows")
        .document(WF)
        .collection("work_packages")
        .document(f"wp-workforce-{WF.removeprefix('wf-')}")
        .get()
        .to_dict()
    )
    assert wp["owner_instance_id"] == "agent-workforce-02"
    assert wp["reassigned_from"] == "agent-workforce-01"
    assert wp["assignment_seq"] == 2
    assert registry.instance_ref(db, "agent-workforce-01").get().to_dict()["state"] == "FAILED"
    assert registry.instance_ref(db, "agent-workforce-02").get().to_dict()["state"] == "ACTIVE"
    reassignments = [
        m
        for m in outbox_messages(db)
        if m["envelope"]["schema_version"] == "work_package_assignment.v2"
    ]
    assert len(reassignments) == 1
    assert reassignments[0]["payload"]["assigned_agent_id"] == "agent-workforce-02"
    assert reassignments[0]["payload"]["reassigned_from"] == "agent-workforce-01"
    assert reassignments[0]["payload"]["assignment_seq"] == 2
    kinds = [e["payload"]["event_kind"] for e in state.reconstruct_audit_trail(db, WF)]
    assert kinds.count("reassignment") == 1
    # Duplicate delivery of the same failure event: inbox dedupe, no changes.
    assert process_message(db, message, handler, consumer_identity="forge-orchestrator") is False
    # A second, distinct failure event for the ALREADY-TRANSFERRED owner:
    # the ownership guard skips the transfer (exactly once).
    second = failure_message("workforce", "agent-workforce-01", suffix="b")
    process_message(db, second, handler, consumer_identity="forge-orchestrator")
    wp_after = (
        db.collection("workflows")
        .document(WF)
        .collection("work_packages")
        .document(f"wp-workforce-{WF.removeprefix('wf-')}")
        .get()
        .to_dict()
    )
    assert wp_after["owner_instance_id"] == "agent-workforce-02"
    assert wp_after["assignment_seq"] == 2


def test_non_workforce_failure_blocks_with_escalation():
    db = ready_db()
    handler = make_failure_handler(db)
    assert process_message(
        db,
        failure_message("supply", "agent-supply-01"),
        handler,
        consumer_identity="forge-orchestrator",
    )
    assert (
        db.collection("workflows").document(WF).get().to_dict()["status"] == "BLOCKED_AGENT_FAILURE"
    )
    reasons = [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db, WF)]
    assert "SPECIALIST_FAILURE_NO_RESERVE" in reasons


# ---------- Monitoring cycle (ORC-4) ----------


def test_monitor_flags_timed_out_assignment_once():
    db = ready_db()
    claim_workforce_package(db)
    wp_ref = (
        db.collection("workflows")
        .document(WF)
        .collection("work_packages")
        .document(f"wp-workforce-{WF.removeprefix('wf-')}")
    )
    wp_ref.set({**wp_ref.get().to_dict(), "assigned_observed_at": "2026-08-21T09:00:00.000000Z"})
    now = "2026-08-21T09:10:00.000000Z"  # 10 min later; timeout is 30s
    first = run_monitoring_cycle(db, trace_id_for=lambda wf: TRACE, now=now)
    second = run_monitoring_cycle(db, trace_id_for=lambda wf: TRACE, now=now)
    assert first == [f"wp-workforce-{WF.removeprefix('wf-')}"]
    assert second == []
    timeouts = [
        m
        for m in outbox_messages(db)
        if m["envelope"]["schema_version"] == "agent_failure_event.v2"
    ]
    assert len(timeouts) == 1
    assert timeouts[0]["payload"]["failure_kind"] == "timeout"


def test_monitor_ignores_fresh_assignments():
    db = ready_db()
    claim_workforce_package(db)
    flagged = run_monitoring_cycle(db, trace_id_for=lambda wf: TRACE)
    assert flagged == []


def test_timeout_detection_flows_into_repair():
    """ORC-4 -> ORC-3 end to end: stale assignment -> timeout event ->
    reserve deployed."""
    db = ready_db()
    claim_workforce_package(db)
    wp_id = f"wp-workforce-{WF.removeprefix('wf-')}"
    wp_ref = db.collection("workflows").document(WF).collection("work_packages").document(wp_id)
    wp_ref.set({**wp_ref.get().to_dict(), "assigned_observed_at": "2026-08-21T09:00:00.000000Z"})
    run_monitoring_cycle(db, trace_id_for=lambda wf: TRACE, now="2026-08-21T09:10:00.000000Z")
    timeout_event = next(
        m
        for m in outbox_messages(db)
        if m["envelope"]["schema_version"] == "agent_failure_event.v2"
    )
    assert process_message(
        db, timeout_event, make_failure_handler(db), consumer_identity="forge-orchestrator"
    )
    assert wp_ref.get().to_dict()["owner_instance_id"] == "agent-workforce-02"
