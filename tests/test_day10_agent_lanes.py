"""Day 10: Agent Operations lanes — per-agent 'now' visualization.

The lane endpoint composes ONLY governed state (registry instances, live
bus claims, ASSIGNED work packages, the audit trail) and stays
metadata-only: no prompts, no model text, no audit detail fields. Motion
in the UI is event-driven and derives entirely from this endpoint.
"""

import datetime

import pytest
from fastapi.testclient import TestClient
from services.dashboard.app import _definition_for_identity, create_app
from services.dashboard.ui import PAGE_HTML
from tests.fake_firestore import FakeFirestore

from forge_common import layout, registry, state
from forge_common.audit import now_iso
from forge_common.messages import build_envelope, deterministic_event_id

WF = "wf-day10-001"
TRACE = "feedfacefeedfacefeedfacefeedface"
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


def lanes_by_id(client):
    return {lane["definition_id"]: lane for lane in client.get("/api/agents/now").json()}


def future_iso(seconds=90):
    stamp = datetime.datetime.now(datetime.UTC) + datetime.timedelta(seconds=seconds)
    return stamp.isoformat(timespec="microseconds").replace("+00:00", "Z")


def seed_claim(db, *, consumer, event_id="", lease=None, status="processing"):
    layout.inbox_ref(db, WF, f"marker-{consumer}").set(
        {
            "status": status,
            "claim_token": "t",
            "lease_expires_at": lease or future_iso(),
            "event_id": event_id,
            "idempotency_key": "idem-day10",
            "consumer_identity": consumer,
        }
    )


def seed_assigned(db, *, owner="agent-supply-01", role="supply"):
    wp_id = f"wp-{role}-{WF.removeprefix('wf-')}"
    layout.work_package_ref(db, WF, wp_id).set(
        {
            "work_package_id": wp_id,
            "role": role,
            "owner_instance_id": owner,
            "status": "ASSIGNED",
            "assignment_seq": 1,
            "objective": "Source the approved actuator and report shipment status." + "x" * 200,
            "assigned_observed_at": now_iso(),
        }
    )
    return wp_id


# ------------------------------------------------------------ identity mapping


@pytest.mark.parametrize(
    "identity,expected",
    [
        ("forge-orchestrator", "forge-orchestrator"),
        ("forge-cyber-trust", "forge-cyber-trust"),
        ("agent-maintenance-01", "forge-maintenance"),
        ("agent-cyber_trust-01", "forge-cyber-trust"),
        ("agent-workforce-02", "forge-workforce"),
        ("forge-approval-surface", None),
        ("forge-deployer", None),
        ("duty-approval-officer", None),
    ],
)
def test_definition_for_identity(identity, expected):
    assert _definition_for_identity(identity) == expected


# ------------------------------------------------------------------ lane shapes


def test_idle_fleet_renders_six_lanes_with_instances():
    lanes = lanes_by_id(make_client(ready_db()))
    assert len(lanes) == 6
    for lane in lanes.values():
        assert lane["now"]["kind"] == "idle"
        assert lane["instances"] and all("health" in i for i in lane["instances"])
    workforce = lanes["forge-workforce"]
    assert workforce["acting_instance_id"] == "agent-workforce-01"  # IDLE preferred
    states = {i["instance_id"]: i["state"] for i in workforce["instances"]}
    assert states["agent-workforce-02"] == "RESERVE"


def test_live_claim_renders_processing_with_schema():
    db = ready_db()
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="nmc_event.v2",
            event_id=deterministic_event_id("day10-nmc", WF),
            trace_id=TRACE,
            idempotency_key="idem-day10-nmc",
        ),
        "payload": {
            "equipment_id": "GX12-07",
            "discrepancy_code": "DSC-0042",
            "description": "Failed hydraulic actuator on lift assembly",
            "reported_at": "2026-08-24T09:00:00Z",
        },
    }
    layout.outbox_ref(db, WF, message["envelope"]["event_id"]).set(
        {"message": message, "published": True, "enqueued_at": now_iso()}
    )
    seed_claim(db, consumer="forge-orchestrator", event_id=message["envelope"]["event_id"])
    lane = lanes_by_id(make_client(db))["forge-orchestrator"]
    assert lane["now"]["kind"] == "processing"
    assert "nmc_event.v2" in lane["now"]["text"]
    assert lane["now"]["workflow_id"] == WF


def test_expired_lease_and_done_markers_do_not_render_as_processing():
    db = ready_db()
    seed_claim(db, consumer="forge-orchestrator", lease="2020-01-01T00:00:00.000000Z")
    seed_claim(db, consumer="forge-safety", status="done")
    lanes = lanes_by_id(make_client(db))
    assert lanes["forge-orchestrator"]["now"]["kind"] == "idle"
    assert lanes["forge-safety"]["now"]["kind"] == "idle"


def test_claims_on_terminal_workflows_are_ignored():
    db = ready_db()
    seed_claim(db, consumer="forge-orchestrator")
    doc = layout.workflow_ref(db, WF).get().to_dict()
    layout.workflow_ref(db, WF).set(
        layout.validate_state_doc({**doc, "status": "CANCELLED", "due_at": None})
    )
    assert lanes_by_id(make_client(db))["forge-orchestrator"]["now"]["kind"] == "idle"


def test_assigned_package_renders_executing_with_bounded_objective():
    db = ready_db()
    wp_id = seed_assigned(db)
    lane = lanes_by_id(make_client(db))["forge-supply"]
    assert lane["now"]["kind"] == "executing"
    assert wp_id in lane["now"]["text"]
    assert len(lane["now"]["text"]) < 200  # objective truncated at 140
    assert lane["now"]["workflow_id"] == WF


def test_all_failed_definition_renders_failed_and_reserve_stays_reserve():
    db = ready_db()
    registry.operator_set_instance_state(
        db, instance_id="agent-supply-01", action="fail", operator=OPERATOR
    )
    lanes = lanes_by_id(make_client(db))
    assert lanes["forge-supply"]["now"]["kind"] == "failed"
    # workforce with only the reserve left standing reports reserve, not idle
    registry.operator_set_instance_state(
        db, instance_id="agent-workforce-01", action="fail", operator=OPERATOR
    )
    lanes = lanes_by_id(make_client(db))
    workforce = lanes["forge-workforce"]
    assert workforce["acting_instance_id"] == "agent-workforce-02"
    assert workforce["now"]["kind"] == "reserve"


def test_recent_ticker_is_newest_first_capped_and_detail_free():
    db = ready_db()
    client = make_client(db)
    client.post(f"/api/workflows/{WF}/cancel", headers=AUTHED, json={})
    response = client.get("/api/agents/now")
    assert "detail" not in response.text  # metadata-only: no detail fields ever
    assert OPERATOR not in response.text  # operator emails stay out of the lanes
    orchestrator = {lane["definition_id"]: lane for lane in response.json()}["forge-orchestrator"]
    assert 1 <= len(orchestrator["recent"]) <= 3
    stamps = [e["observed_at"] for e in orchestrator["recent"]]
    assert stamps == sorted(stamps, reverse=True)


# ------------------------------------------------------------- feed filtering


def test_activity_agent_filter_returns_only_that_definition():
    db = ready_db()
    client = make_client(db)
    client.post("/api/clock/advance", headers=AUTHED, json={"days": 1})
    everything = client.get("/api/activity").json()
    assert {e["agent_identity"] for e in everything} >= {"forge-orchestrator"}
    filtered = client.get("/api/activity?agent=forge-orchestrator").json()
    assert filtered  # WORKFLOW_CREATED rides forge-approval-surface? no: op-start only
    assert all(
        _definition_for_identity(e["agent_identity"]) == "forge-orchestrator" for e in filtered
    )
    assert client.get("/api/activity?agent=forge-supply").json() == []


# ------------------------------------------------------------------ page wiring


def test_page_carries_the_agent_lanes():
    for marker in (
        "Agent Operations",
        "/api/agents/now",
        "laneFilter",
        "prefers-reduced-motion",
        "function esc(",
        "lanepulse",
        "laneflash",
        'class="dock"',
    ):
        assert marker in PAGE_HTML
