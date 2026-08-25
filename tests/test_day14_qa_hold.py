"""Day 14: entrant QA-HOLD regressions (audit of 2026-08-25, commit 829e510).

P1-1: an instance failed MID-WORK must not commit a success afterwards —
the transactional ownership guard now also refuses results from a FAILED
owner, audited, leaving the package ASSIGNED for the monitor's normal
failure disposition.
P1-2: the console accepts at most ONE active recovery per vehicle — the
per-equipment marker is claimed in the same transaction as the workflow
document, so simultaneous starts cannot both commit.
P2: the orchestrator /tick health probe is injectable — unit suites never
touch the network even though the registry carries deployed endpoints.
"""

import pytest
from fastapi.testclient import TestClient
from services.dashboard.app import create_app
from tests.adk_stub import stub_json
from tests.fake_firestore import FakeFirestore

from forge_common import layout, registry, state
from forge_common.audit import now_iso
from forge_common.bus import process_message
from forge_common.messages import build_envelope, deterministic_event_id

WF = "wf-day14-001"
TRACE = "cafef00dcafef00dcafef00dcafef00d"
OPERATOR = "operator@example.com"
AUTHED = {"authorization": "Bearer test-token"}


def make_client(db):
    return TestClient(create_app(db, verifier=lambda token: OPERATOR))


def ready_db():
    db = FakeFirestore()
    registry.load_registry(db)
    layout.clock_ref(db).set({"logical_time": 0})
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    return db


def seed_assigned_supply(db):
    wp_id = f"wp-supply-{WF.removeprefix('wf-')}"
    layout.work_package_ref(db, WF, wp_id).set(
        {
            "work_package_id": wp_id,
            "role": "supply",
            "owner_instance_id": "agent-supply-01",
            "status": "ASSIGNED",
            "assignment_seq": 1,
            "inputs": {"equipment_id": "GX12-07", "discrepancy_code": "DSC-0042"},
            "assigned_observed_at": now_iso(),
        }
    )
    return wp_id


def supply_assignment(wp_id):
    return {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=wp_id,
            schema_version="work_package_assignment.v2",
            event_id=deterministic_event_id("day14-assign", WF),
            trace_id=TRACE,
            idempotency_key="idem-day14-assign",
        ),
        "payload": {
            "role": "supply",
            "objective": "Source the approved actuator and report shipment status.",
            "assigned_agent_id": "agent-supply-01",
            "assignment_seq": 1,
            "inputs": {"equipment_id": "GX12-07", "discrepancy_code": "DSC-0042"},
        },
    }


SOURCING = {
    "part_number": "HYD-ACT-4402",
    "part_approved": True,
    "shipment_status": "delayed",
    "eta_days": 21,
}


# ------------------------------------------------- P1-1: mid-work failure race


def test_failed_owner_cannot_commit_a_success():
    """Entrant repro: owner_state=FAILED yet the package result still
    landed COMPLETED. The result must be REFUSED, audited, and the package
    left ASSIGNED for the monitor's failure disposition."""
    from services.supply.handlers import make_handler as make_supply

    db = ready_db()
    wp_id = seed_assigned_supply(db)
    registry.instance_ref(db, "agent-supply-01").set({"state": "ACTIVE"}, merge=True)
    # the operator injects the failure while the agent is mid-work (the
    # commit-time transactional read is what must catch it)
    registry.operator_set_instance_state(
        db, instance_id="agent-supply-01", action="fail", operator=OPERATOR
    )
    assert process_message(
        db,
        supply_assignment(wp_id),
        make_supply(db, stub_json(SOURCING)),
        consumer_identity="forge-supply",
    )
    wp = layout.work_package_ref(db, WF, wp_id).get().to_dict()
    assert wp["status"] == "ASSIGNED"  # NOT completed
    outbox_schemas = [
        s.to_dict()["message"]["envelope"]["schema_version"]
        for s in layout.workflow_ref(db, WF).collection("outbox").stream()
    ]
    assert "sourcing_report.v3" not in outbox_schemas  # no late success on the bus
    refusals = [
        e
        for e in state.reconstruct_audit_trail(db, WF)
        if e["payload"]["reason_code"] == "RESULT_REFUSED_OWNER_FAILED"
    ]
    assert len(refusals) == 1
    assert refusals[0]["payload"]["event_kind"] == "blocked_action"
    assert refusals[0]["envelope"]["trace_id"] == TRACE
    assert registry.instance_ref(db, "agent-supply-01").get().to_dict()["state"] == "FAILED"


def test_healthy_owner_still_completes_normally():
    from services.supply.handlers import make_handler as make_supply

    db = ready_db()
    wp_id = seed_assigned_supply(db)
    registry.instance_ref(db, "agent-supply-01").set({"state": "ACTIVE"}, merge=True)
    assert process_message(
        db,
        supply_assignment(wp_id),
        make_supply(db, stub_json(SOURCING)),
        consumer_identity="forge-supply",
    )
    assert layout.work_package_ref(db, WF, wp_id).get().to_dict()["status"] == "COMPLETED"
    reasons = [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db, WF)]
    assert "RESULT_REFUSED_OWNER_FAILED" not in reasons


def test_refused_package_is_timed_out_by_the_monitor():
    """The refusal hands the package to the NORMAL failure machinery: the
    monitor times out the still-ASSIGNED package for disposition."""
    from services.orchestrator.monitor import run_monitoring_cycle
    from services.supply.handlers import make_handler as make_supply

    db = ready_db()
    wp_id = seed_assigned_supply(db)
    registry.operator_set_instance_state(
        db, instance_id="agent-supply-01", action="fail", operator=OPERATOR
    )
    process_message(
        db,
        supply_assignment(wp_id),
        make_supply(db, stub_json(SOURCING)),
        consumer_identity="forge-supply",
    )
    assert run_monitoring_cycle(db, now="2100-01-01T00:00:00.000000Z") == [wp_id]


# --------------------------------------------- P1-2: one recovery per vehicle


def start_body(equipment="GX12-03"):
    return {
        "equipment_id": equipment,
        "discrepancy_code": "DSC-0042",
        "description": "Duplicate-start regression fixture",
    }


def test_console_refuses_a_duplicate_recovery_for_the_same_vehicle():
    """Entrant repro: two simultaneous live workflows for GX12-03."""
    db = ready_db()
    client = make_client(db)
    first = client.post("/api/workflows", headers=AUTHED, json=start_body())
    assert first.status_code == 200
    second = client.post("/api/workflows", headers=AUTHED, json=start_body())
    assert second.status_code == 409
    assert "GX12-03 already has an active recovery" in second.text
    live = [
        w
        for w in client.get("/api/workflows").json()
        if w["equipment_id"] == "GX12-03" and w["status"] not in state.TERMINAL_STATES
    ]
    assert len(live) == 1


def test_cancelling_frees_the_vehicle_for_a_new_recovery():
    db = ready_db()
    client = make_client(db)
    first = client.post("/api/workflows", headers=AUTHED, json=start_body()).json()
    client.post(f"/api/workflows/{first['workflow_id']}/cancel", headers=AUTHED, json={})
    again = client.post("/api/workflows", headers=AUTHED, json=start_body())
    assert again.status_code == 200


def test_stale_marker_for_a_deleted_workflow_does_not_block():
    db = ready_db()
    client = make_client(db)
    first = client.post("/api/workflows", headers=AUTHED, json=start_body()).json()
    layout.workflow_ref(db, first["workflow_id"]).delete()  # driver-style cleanup
    again = client.post("/api/workflows", headers=AUTHED, json=start_body())
    assert again.status_code == 200


def test_direct_exclusive_create_raises_and_non_exclusive_is_unchanged():
    db = ready_db()
    state.create_workflow(
        db,
        workflow_id="wf-day14-a01",
        equipment_id="GX12-04",
        trace_id="a" * 32,
        logical_time=0,
        exclusive=True,
    )
    with pytest.raises(state.InvalidTransition):
        state.create_workflow(
            db,
            workflow_id="wf-day14-a02",
            equipment_id="GX12-04",
            trace_id="b" * 32,
            logical_time=0,
            exclusive=True,
        )
    # harness/driver creations (non-exclusive) stay permissive by design
    state.create_workflow(
        db,
        workflow_id="wf-day14-a03",
        equipment_id="GX12-04",
        trace_id="c" * 32,
        logical_time=0,
    )


def test_rejected_duplicate_start_is_audited_on_the_active_recovery():
    """Entrant cleanup item 3: HUM-3/AUD-1 — the REFUSED operator action
    must appear in the trail, not vanish into a bare 409."""
    db = ready_db()
    client = make_client(db)
    first = client.post("/api/workflows", headers=AUTHED, json=start_body()).json()
    second = client.post("/api/workflows", headers=AUTHED, json=start_body())
    assert second.status_code == 409
    rejections = [
        e
        for e in state.reconstruct_audit_trail(db, first["workflow_id"])
        if e["payload"]["reason_code"] == "OPERATOR_START_REJECTED"
    ]
    assert len(rejections) == 1
    assert rejections[0]["payload"]["event_kind"] == "blocked_action"
    assert OPERATOR in rejections[0]["payload"]["detail"]


def test_duplicate_recovery_exception_carries_the_existing_workflow():
    db = ready_db()
    state.create_workflow(
        db,
        workflow_id="wf-day14-b01",
        equipment_id="GX12-05",
        trace_id="d" * 32,
        logical_time=0,
        exclusive=True,
    )
    with pytest.raises(state.DuplicateRecovery) as excinfo:
        state.create_workflow(
            db,
            workflow_id="wf-day14-b02",
            equipment_id="GX12-05",
            trace_id="e" * 32,
            logical_time=0,
            exclusive=True,
        )
    assert excinfo.value.existing_workflow_id == "wf-day14-b01"
    assert excinfo.value.equipment_id == "GX12-05"
