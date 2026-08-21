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


def claim_package(db, role, instance=None, inputs=None):
    """Specialist outputs are ownership-guarded: tests must hold a real claim."""

    def _claim(txn):
        registry.claim_work_package(
            txn,
            db,
            workflow_id=WF,
            work_package_id=f"wp-{role}-{WF.removeprefix('wf-')}",
            instance_id=instance or f"agent-{role}-01",
            role=role,
            objective=f"Test objective for {role}.",
            inputs=inputs or NMC_INPUTS,
        )

    layout.run_in_transaction(db, _claim)


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
    claim_package(db, "supply")
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
    claim_package(db, "supply")
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
    assert report["envelope"]["schema_version"] == "sourcing_report.v3"
    assert report["payload"]["part_approved"] is True


def test_supply_unapproved_part_reported_honestly_publishes():
    db = ready_db()
    claim_package(db, "supply")
    honest = stub_json(
        {
            "part_number": "VND-ACT-9901",
            "part_approved": False,  # honest about the registry
            "shipment_status": "not_ordered",
            "eta_days": 0,
        }
    )
    process_message(
        db,
        assignment_message("supply"),
        make_supply_handler(db, honest),
        consumer_identity="forge-supply",
    )
    report = outbox_messages(db)[0]
    assert report["envelope"]["schema_version"] == "sourcing_report.v3"
    assert report["payload"]["part_approved"] is False


# ---------- Workforce: no waivers (AGT-3) ----------


def test_workforce_qualified_roster_publishes():
    db = ready_db()
    claim_package(db, "workforce", inputs={**NMC_INPUTS, "task_codes": ["TC-101"]})
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
    claim_package(db, "workforce", inputs={**NMC_INPUTS, "task_codes": ["TC-101"]})
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
    assert trail[-1]["payload"]["reason_code"] == "ACTION_APPROVED"


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
    assert veto["event_kind"] == "veto" and veto["reason_code"] == "ACTION_VETOED"


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


# ---------- Day 4 closeout regressions (entrant review) ----------


def wp_doc(db, role="workforce"):
    return (
        db.collection("workflows")
        .document(WF)
        .collection("work_packages")
        .document(f"wp-{role}-{WF.removeprefix('wf-')}")
        .get()
        .to_dict()
    )


def test_successful_specialist_marks_completed_and_never_times_out():
    """Gap 1: a valid output flips the package to COMPLETED atomically, so
    the monitor cannot falsely time it out afterwards."""
    from forge_common.messages import deterministic_trace_id as _trace

    db = ready_db()

    def _claim(txn):
        registry.claim_work_package(
            txn,
            db,
            workflow_id=WF,
            work_package_id=f"wp-supply-{WF.removeprefix('wf-')}",
            instance_id="agent-supply-01",
            role="supply",
            objective="Source the actuator.",
            inputs=NMC_INPUTS,
        )

    layout.run_in_transaction(db, _claim)
    truthful = stub_json(
        {
            "part_number": "HYD-ACT-4402",
            "part_approved": True,
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
    assert wp_doc(db, "supply")["status"] == "COMPLETED"
    # Even with an ancient assignment timestamp, COMPLETED is exempt.
    ref = (
        db.collection("workflows")
        .document(WF)
        .collection("work_packages")
        .document(f"wp-supply-{WF.removeprefix('wf-')}")
    )
    ref.set({**ref.get().to_dict(), "assigned_observed_at": "2026-08-21T00:00:00.000000Z"})
    flagged = run_monitoring_cycle(
        db, trace_id_for=lambda wf: _trace(wf), now="2026-08-21T09:00:00.000000Z"
    )
    assert flagged == []


def test_source_failure_marks_failed_pending_repair():
    db = ready_db()

    def _claim(txn):
        registry.claim_work_package(
            txn,
            db,
            workflow_id=WF,
            work_package_id=f"wp-supply-{WF.removeprefix('wf-')}",
            instance_id="agent-supply-01",
            role="supply",
            objective="Source the actuator.",
        )

    layout.run_in_transaction(db, _claim)
    lying = stub_json(
        {
            "part_number": "VND-ACT-9901",
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
    assert wp_doc(db, "supply")["status"] == "FAILED_PENDING_REPAIR"


def test_plan_handler_creates_workforce_assignment_from_real_flow():
    """Gap 2: the Workforce package is created by consuming the ACTUAL
    maintenance plan, not by hand."""
    from services.orchestrator.handlers import make_plan_handler

    db = ready_db()
    plan = plan_message(["HYD-ACT-4402"])
    assert process_message(db, plan, make_plan_handler(db), consumer_identity="forge-orchestrator")
    wp = wp_doc(db, "workforce")
    assert wp["owner_instance_id"] == "agent-workforce-01"
    assert wp["inputs"]["task_codes"] == ["TC-101"]
    assignment = next(
        m
        for m in outbox_messages(db)
        if m["envelope"]["schema_version"] == "work_package_assignment.v2"
    )
    assert assignment["payload"]["role"] == "workforce"
    assert assignment["payload"]["inputs"]["task_codes"] == ["TC-101"]


def test_reassignment_preserves_inputs():
    """Gap 3: the reserve receives the full work order, not an empty one."""
    from services.orchestrator.handlers import make_plan_handler

    db = ready_db()
    plan = plan_message(["HYD-ACT-4402"])
    process_message(db, plan, make_plan_handler(db), consumer_identity="forge-orchestrator")
    assert process_message(
        db,
        failure_message("workforce", "agent-workforce-01"),
        make_failure_handler(db),
        consumer_identity="forge-orchestrator",
    )
    reassignment = next(
        m
        for m in outbox_messages(db)
        if m["envelope"]["schema_version"] == "work_package_assignment.v2"
        and m["payload"].get("reassigned_from")
    )
    assert reassignment["payload"]["assigned_agent_id"] == "agent-workforce-02"
    assert reassignment["payload"]["inputs"]["task_codes"] == ["TC-101"]
    assert wp_doc(db, "workforce")["inputs"]["task_codes"] == ["TC-101"]


def test_stale_second_failure_is_total_noop():
    """Gap 4: after a successful repair, a distinct stale failure for the
    old owner changes NOTHING — no block, no audit, no output."""
    db = ready_db()
    claim_workforce_package(db)
    handler = make_failure_handler(db)
    process_message(
        db,
        failure_message("workforce", "agent-workforce-01"),
        handler,
        consumer_identity="forge-orchestrator",
    )
    status_before = db.collection("workflows").document(WF).get().to_dict()["status"]
    trail_before = len(state.reconstruct_audit_trail(db, WF))
    outbox_before = len(outbox_messages(db))
    stale = failure_message("workforce", "agent-workforce-01", suffix="stale2")
    assert process_message(db, stale, handler, consumer_identity="forge-orchestrator")
    assert db.collection("workflows").document(WF).get().to_dict()["status"] == status_before
    assert len(state.reconstruct_audit_trail(db, WF)) == trail_before
    assert len(outbox_messages(db)) == outbox_before
    assert wp_doc(db)["owner_instance_id"] == "agent-workforce-02"
    assert registry.instance_ref(db, "agent-workforce-02").get().to_dict()["state"] == "ACTIVE"


def test_supply_approval_is_discrepancy_specific():
    """An approved ELECTRICAL part is NOT approved for the hydraulic
    discrepancy."""
    db = ready_db()
    claim_package(db, "supply")
    cross_sell = stub_json(
        {
            "part_number": "ELEC-HARN-2210",  # approved, but only for DSC-0311
            "part_approved": True,
            "shipment_status": "ordered",
            "eta_days": 7,
        }
    )
    process_message(
        db,
        assignment_message("supply"),  # inputs carry DSC-0042 (hydraulic)
        make_supply_handler(db, cross_sell),
        consumer_identity="forge-supply",
    )
    out = outbox_messages(db)[0]
    assert out["envelope"]["schema_version"] == "agent_failure_event.v2"


def test_supply_cannot_invent_shipment_status_or_eta():
    db = ready_db()
    claim_package(db, "supply")
    optimistic = stub_json(
        {
            "part_number": "HYD-ACT-4402",
            "part_approved": True,
            "shipment_status": "in_transit",  # data says delayed
            "eta_days": 2,  # data says 21
        }
    )
    process_message(
        db,
        assignment_message("supply"),
        make_supply_handler(db, optimistic),
        consumer_identity="forge-supply",
    )
    out = outbox_messages(db)[0]
    assert out["envelope"]["schema_version"] == "agent_failure_event.v2"


def test_safety_validates_sourcing_and_rosters():
    """AGT-4: every proposed action is validated, not only plans."""
    from services.safety.handlers import make_validation_handler

    db = ready_db()
    report = {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=f"wp-supply-{WF.removeprefix('wf-')}",
            schema_version="sourcing_report.v3",
            event_id=deterministic_event_id("t-report", WF),
            trace_id=TRACE,
            idempotency_key="idem-t-report-01",
        ),
        "payload": {
            "part_number": "VND-ACT-9901",
            "part_approved": True,  # violation: unregistered part
            "shipment_status": "ordered",
            "eta_days": 2,
        },
    }
    verdict_stub = stub_json(
        {
            "subject_event_id": report["envelope"]["event_id"],
            "verdict": "vetoed",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["Unregistered part claimed as approved."],
        }
    )
    process_message(
        db, report, make_validation_handler(db, verdict_stub), consumer_identity="forge-safety"
    )
    verdict = outbox_messages(db)[0]
    assert verdict["envelope"]["schema_version"] == "validation_verdict.v2"
    assert verdict["payload"]["verdict"] == "vetoed"


# ---------- Race blockers (entrant review round 2) ----------


def test_stale_worker_entire_bundle_dropped():
    """Blocker 1 (fake): a worker reassigned mid-flight commits NOTHING —
    no output, no audit, no status."""
    db = ready_db()
    claim_package(db, "supply")
    inner = make_supply_handler(
        db,
        stub_json(
            {
                "part_number": "HYD-ACT-4402",
                "part_approved": True,
                "shipment_status": "delayed",
                "eta_days": 21,
            }
        ),
    )

    def usurped(msg, writes):
        inner(msg, writes)  # bundle prepared by the real handler
        ref = (
            db.collection("workflows")
            .document(WF)
            .collection("work_packages")
            .document(f"wp-supply-{WF.removeprefix('wf-')}")
        )
        ref.set({**ref.get().to_dict(), "owner_instance_id": "agent-supply-99"})

    assert process_message(
        db, assignment_message("supply"), usurped, consumer_identity="forge-supply"
    )
    assert outbox_messages(db) == []
    trail = state.reconstruct_audit_trail(db, WF)
    assert all(e["payload"]["reason_code"] != "DOMAIN_OUTPUT_PRODUCED" for e in trail)
    wp = wp_doc(db, "supply")
    assert wp["owner_instance_id"] == "agent-supply-99"
    assert wp["status"] == "ASSIGNED"


def test_completion_between_preread_and_commit_prevents_block():
    """Blocker 2 (fake): the package completes after the failure handler's
    pre-read — the commit-time recheck consumes the event with zero effects."""
    db = ready_db()
    claim_package(db, "supply")
    inner = make_failure_handler(db)

    def raced(msg, writes):
        inner(msg, writes)  # pre-read saw ASSIGNED; disposition prepared
        ref = (
            db.collection("workflows")
            .document(WF)
            .collection("work_packages")
            .document(f"wp-supply-{WF.removeprefix('wf-')}")
        )
        ref.set({**ref.get().to_dict(), "status": "COMPLETED"})

    trail_before = len(state.reconstruct_audit_trail(db, WF))
    assert process_message(
        db,
        failure_message("supply", "agent-supply-01"),
        raced,
        consumer_identity="forge-orchestrator",
    )
    assert db.collection("workflows").document(WF).get().to_dict()["status"] == "INTAKE"
    assert len(state.reconstruct_audit_trail(db, WF)) == trail_before
    assert wp_doc(db, "supply")["status"] == "COMPLETED"
    assert registry.instance_ref(db, "agent-supply-01").get().to_dict()["state"] != "FAILED"


def test_still_assigned_failure_marks_package_and_instance_failed():
    """Blocker 2 (fake): the genuine path — package still assigned to the
    failed owner — blocks, audits, and marks package + instance FAILED
    atomically."""
    db = ready_db()
    claim_package(db, "supply")
    assert process_message(
        db,
        failure_message("supply", "agent-supply-01"),
        make_failure_handler(db),
        consumer_identity="forge-orchestrator",
    )
    assert (
        db.collection("workflows").document(WF).get().to_dict()["status"] == "BLOCKED_AGENT_FAILURE"
    )
    assert wp_doc(db, "supply")["status"] == "FAILED"
    assert registry.instance_ref(db, "agent-supply-01").get().to_dict()["state"] == "FAILED"
    reasons = [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db, WF)]
    assert "SPECIALIST_FAILURE_NO_RESERVE" in reasons


# ---------- Safety rider: discrepancy alignment + roster verdicts ----------


def test_safety_rejects_wrong_discrepancy_part_in_plan():
    """A part approved for the ELECTRICAL discrepancy cannot pass Safety in
    the hydraulic workflow."""
    db = ready_db()
    claim_package(db, "maintenance")  # wp carries inputs.discrepancy_code=DSC-0042
    plan = plan_message(["ELEC-HARN-2210"])
    vetoing = stub_json(
        {
            "subject_event_id": plan["envelope"]["event_id"],
            "verdict": "vetoed",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["ELEC-HARN-2210 is not approved for DSC-0042."],
        }
    )
    process_message(
        db, plan, make_plan_validation_handler(db, vetoing), consumer_identity="forge-safety"
    )
    verdicts = [
        m for m in outbox_messages(db) if m["envelope"]["schema_version"] == "validation_verdict.v2"
    ]
    assert verdicts and verdicts[0]["payload"]["verdict"] == "vetoed"
    # And a stub trying to APPROVE it is refused (contradicts the engine).
    db2 = ready_db()
    claim_package(db2, "maintenance")
    approving = stub_json(
        {
            "subject_event_id": plan["envelope"]["event_id"],
            "verdict": "approved",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["Part is registered."],
        }
    )
    process_message(
        db2, plan, make_plan_validation_handler(db2, approving), consumer_identity="forge-safety"
    )
    out = outbox_messages(db2)[0]
    assert out["envelope"]["schema_version"] == "agent_failure_event.v2"


def test_safety_roster_verdicts():
    """AGT-4 rider: an actual roster is validated — veto on unqualified."""
    from services.safety.handlers import make_validation_handler

    db = ready_db()
    claim_package(db, "workforce")

    def roster_msg(technician, qual):
        return {
            "envelope": build_envelope(
                workflow_id=WF,
                work_package_id=f"wp-workforce-{WF.removeprefix('wf-')}",
                schema_version="roster_assignment.v2",
                event_id=deterministic_event_id("t-roster", WF, technician),
                trace_id=TRACE,
                idempotency_key=f"idem-t-roster-{technician.lower()}",
            ),
            "payload": {
                "assignments": [
                    {
                        "task_code": "TC-101",
                        "technician_id": technician,
                        "qualification_id": qual,
                    }
                ]
            },
        }

    bad = roster_msg("T-2001", "Q-ELE-201")  # unqualified for TC-101
    vetoing = stub_json(
        {
            "subject_event_id": bad["envelope"]["event_id"],
            "verdict": "vetoed",
            "rule_refs": ["SP-QUAL-001"],
            "reasons": ["T-2001 holds no TC-101 qualification."],
        }
    )
    process_message(db, bad, make_validation_handler(db, vetoing), consumer_identity="forge-safety")
    verdict = next(
        m for m in outbox_messages(db) if m["envelope"]["schema_version"] == "validation_verdict.v2"
    )
    assert verdict["payload"]["verdict"] == "vetoed"
    assert "SP-QUAL-001" in verdict["payload"]["rule_refs"]

    good = roster_msg("T-1001", "Q-HYD-101")
    approving = stub_json(
        {
            "subject_event_id": good["envelope"]["event_id"],
            "verdict": "approved",
            "rule_refs": ["SP-QUAL-001"],
            "reasons": ["All technicians qualified."],
        }
    )
    process_message(
        db, good, make_validation_handler(db, approving), consumer_identity="forge-safety"
    )
    approved = [
        m
        for m in outbox_messages(db)
        if m["envelope"]["schema_version"] == "validation_verdict.v2"
        and m["payload"]["verdict"] == "approved"
    ]
    assert len(approved) == 1


def test_second_failure_in_blocked_workflow_no_nack_loop():
    """Round-3 item 2 (fake): a second non-Workforce failure in an already
    BLOCKED workflow consumes cleanly — package+instance FAILED and audited,
    no illegal BLOCKED->BLOCKED transition, no redelivery loop."""
    db = ready_db()
    claim_package(db, "supply")
    claim_package(db, "maintenance")
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
    # Second, distinct failure for a different role — previously an
    # InvalidTransition NACK loop.
    assert process_message(
        db,
        failure_message("maintenance", "agent-maintenance-01"),
        handler,
        consumer_identity="forge-orchestrator",
    )
    assert wp_doc(db, "maintenance")["status"] == "FAILED"
    assert registry.instance_ref(db, "agent-maintenance-01").get().to_dict()["state"] == "FAILED"
    escalations = [
        e
        for e in state.reconstruct_audit_trail(db, WF)
        if e["payload"]["event_kind"] == "escalation"
        and e["payload"]["reason_code"] == "SPECIALIST_FAILURE_NO_RESERVE"
    ]
    assert len(escalations) == 2  # one per failed specialist
    states = [
        e["payload"].get("state_after")
        for e in state.reconstruct_audit_trail(db, WF)
        if "state_after" in e["payload"]
    ]
    assert states.count("BLOCKED_AGENT_FAILURE") == 1  # transitioned exactly once


def test_reserve_unavailable_raced_completion_no_block():
    """Round-3 item 3 (fake): Workforce completes between the no-reserve
    pre-read and commit — the disposition consumes with zero effects."""
    db = ready_db()
    claim_package(db, "workforce", inputs={**NMC_INPUTS, "task_codes": ["TC-101"]})
    # Exhaust the reserve so the RESERVE_UNAVAILABLE branch runs.
    registry.instance_ref(db, "agent-workforce-02").set({"state": "ACTIVE"}, merge=True)
    inner = make_failure_handler(db)

    def raced(msg, writes):
        inner(msg, writes)
        ref = (
            db.collection("workflows")
            .document(WF)
            .collection("work_packages")
            .document(f"wp-workforce-{WF.removeprefix('wf-')}")
        )
        ref.set({**ref.get().to_dict(), "status": "COMPLETED"})

    trail_before = len(state.reconstruct_audit_trail(db, WF))
    assert process_message(
        db,
        failure_message("workforce", "agent-workforce-01"),
        raced,
        consumer_identity="forge-orchestrator",
    )
    assert db.collection("workflows").document(WF).get().to_dict()["status"] == "INTAKE"
    assert len(state.reconstruct_audit_trail(db, WF)) == trail_before
    assert wp_doc(db)["status"] == "COMPLETED"
    assert registry.instance_ref(db, "agent-workforce-01").get().to_dict()["state"] != "FAILED"
    # Genuine no-reserve failure still blocks with the escalation.
    db2 = ready_db()
    claim_package(db2, "workforce", inputs={**NMC_INPUTS, "task_codes": ["TC-101"]})
    registry.instance_ref(db2, "agent-workforce-02").set({"state": "ACTIVE"}, merge=True)
    assert process_message(
        db2,
        failure_message("workforce", "agent-workforce-01"),
        make_failure_handler(db2),
        consumer_identity="forge-orchestrator",
    )
    assert (
        db2.collection("workflows").document(WF).get().to_dict()["status"]
        == "BLOCKED_AGENT_FAILURE"
    )
    reasons = [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db2, WF)]
    assert "RESERVE_UNAVAILABLE" in reasons
