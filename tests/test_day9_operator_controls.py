"""Day 9: HUM-3 governed operator controls + CANCELLED terminal state.

Covers: operator identity from the authenticated principal only (HUM-3
mirrors HUM-1), start (nmc_event enqueued atomically with the state doc),
cancel (terminal, audited, reachable from every non-terminal state),
Logical Clock advance from the console, poisoned-bulletin relay with
metadata-only responses, audited instance fail/restore (REG-5), the Live
Agent Activity feed (pure AUD-2 projection), and the terminal-state guards
that turn late bus traffic for a finished workflow into audited stale
no-ops instead of 500 -> DLQ loops.
"""

import pytest
from fastapi.testclient import TestClient
from services.dashboard.app import create_app
from services.dashboard.ui import PAGE_HTML
from services.orchestrator.handlers import (
    make_failure_handler,
    make_nmc_handler,
    make_plan_handler,
    make_verdict_handler,
)
from services.orchestrator.monitor import run_monitoring_cycle
from tests.adk_stub import stub_json
from tests.fake_firestore import FakeFirestore

from forge_common import layout, registry, state
from forge_common.audit import now_iso
from forge_common.bus import process_message
from forge_common.clock import CLOCK_AUDIT_WORKFLOW, read_clock
from forge_common.contracts import ContractViolation
from forge_common.messages import build_envelope, deterministic_event_id

WF = "wf-day9-001"
TRACE = "deadbeefdeadbeefdeadbeefdeadbeef"
OPERATOR = "operator@example.com"
AUTHED = {"authorization": "Bearer test-token"}
DECOMPOSITION = {
    "objectives": {
        "maintenance": "Plan replacement of the failed hydraulic actuator.",
        "supply": "Source the approved actuator and report shipment status.",
    }
}
PLAN = {
    "plan_id": "plan-day9-01",
    "equipment_id": "GX12-07",
    "tasks": [
        {
            "task_code": "TC-101",
            "title": "Replace actuator",
            "est_hours": 6.5,
            "parts_required": [{"part_number": "HYD-ACT-4402", "qty": 1}],
        }
    ],
}


def make_client(db, *, publish=None, ingest_forward=None):
    return TestClient(
        create_app(
            db,
            verifier=lambda token: OPERATOR,
            publish=publish,
            ingest_forward=ingest_forward,
        )
    )


def ready_db():
    db = FakeFirestore()
    registry.load_registry(db)
    layout.clock_ref(db).set({"logical_time": 0})
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    return db


def set_status(db, status, *, due_at=None):
    """Test seed: place the workflow directly in a target state (guard tests
    exercise the code that runs AFTER these states are reached)."""
    doc = layout.workflow_ref(db, WF).get().to_dict()
    layout.workflow_ref(db, WF).set(
        layout.validate_state_doc({**doc, "status": status, "due_at": due_at})
    )


def trail_reasons(db, workflow_id=WF):
    return [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db, workflow_id)]


def wf_status(db):
    return layout.workflow_ref(db, WF).get().to_dict()["status"]


def nmc_message():
    return {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="nmc_event.v2",
            event_id=deterministic_event_id("day9-nmc", WF),
            trace_id=TRACE,
            idempotency_key="idem-day9-nmc",
        ),
        "payload": {
            "equipment_id": "GX12-07",
            "discrepancy_code": "DSC-0042",
            "description": "Failed hydraulic actuator on lift assembly",
            "reported_at": "2026-08-23T09:00:00Z",
        },
    }


def plan_message():
    return {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=f"wp-maintenance-{WF.removeprefix('wf-')}",
            schema_version="maintenance_action_plan.v2",
            event_id=deterministic_event_id("day9-plan", WF),
            trace_id=TRACE,
            idempotency_key="idem-day9-plan",
        ),
        "payload": PLAN,
    }


# ---------------------------------------------------------------- state machine


def test_terminal_states_are_derived_from_the_table():
    assert sorted(state.TERMINAL_STATES) == ["CANCELLED", "RELEASED"]


@pytest.mark.parametrize(
    "source",
    [s for s in state.ALLOWED_TRANSITIONS if s not in state.TERMINAL_STATES],
)
def test_cancel_edge_exists_from_every_non_terminal_state(source):
    assert "CANCELLED" in state.ALLOWED_TRANSITIONS[source]


@pytest.mark.parametrize("target", ["PLANNING", "RELEASED", "INTAKE"])
def test_cancelled_is_terminal(target):
    db = ready_db()
    set_status(db, "CANCELLED")
    with pytest.raises(state.InvalidTransition):
        state.transition_workflow(
            db,
            workflow_id=WF,
            target=target,
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="TEST_ESCAPE",
        )


@pytest.mark.parametrize(
    "status,due_at",
    [
        ("INTAKE", None),
        ("PLANNING", None),
        ("VALIDATING", None),
        ("AWAITING_SCHEDULE_APPROVAL", None),
        ("SUSPENDED_AWAITING_PART", 21),
        ("ASSEMBLY_RESUMED", None),
        ("AWAITING_RELEASE_APPROVAL", None),
        ("BLOCKED_AGENT_FAILURE", None),
    ],
)
def test_cancel_transition_applies_from_each_state(status, due_at):
    db = ready_db()
    set_status(db, status, due_at=due_at)
    doc = state.transition_workflow(
        db,
        workflow_id=WF,
        target="CANCELLED",
        agent_identity="forge-approval-surface",
        trace_id=TRACE,
        reason_code="WORKFLOW_CANCELLED",
    )
    assert doc["status"] == "CANCELLED"
    assert doc["due_at"] is None  # a suspended cancel clears the due day


# ------------------------------------------------------------ cancel endpoint


def test_cancel_endpoint_cancels_and_audits_the_operator():
    db = ready_db()
    response = make_client(db).post(f"/api/workflows/{WF}/cancel", headers=AUTHED, json={})
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert wf_status(db) == "CANCELLED"
    trail = state.reconstruct_audit_trail(db, WF)
    cancel = [e for e in trail if e["payload"]["reason_code"] == "WORKFLOW_CANCELLED"]
    assert len(cancel) == 1
    assert cancel[0]["payload"]["event_kind"] == "state_change"
    assert OPERATOR in cancel[0]["payload"]["detail"]
    assert cancel[0]["envelope"]["trace_id"] == TRACE  # OBS-1: workflow trace


def test_cancel_unauthenticated_writes_nothing():
    db = ready_db()
    response = make_client(db).post(f"/api/workflows/{WF}/cancel", json={})
    assert response.status_code == 401
    assert wf_status(db) == "INTAKE"
    assert "WORKFLOW_CANCELLED" not in trail_reasons(db)


def test_cancel_unknown_workflow_404():
    response = make_client(ready_db()).post(
        "/api/workflows/wf-nope-000/cancel", headers=AUTHED, json={}
    )
    assert response.status_code == 404


@pytest.mark.parametrize("status", ["RELEASED", "CANCELLED"])
def test_cancel_terminal_workflow_409(status):
    db = ready_db()
    set_status(db, status)
    response = make_client(db).post(f"/api/workflows/{WF}/cancel", headers=AUTHED, json={})
    assert response.status_code == 409
    assert status in response.text


def test_cancel_rejects_client_supplied_identity():
    db = ready_db()
    response = make_client(db).post(
        f"/api/workflows/{WF}/cancel",
        headers=AUTHED,
        json={"operator_identity": "attacker@evil.example"},
    )
    assert response.status_code == 400
    assert wf_status(db) == "INTAKE"


# ------------------------------------------------------------- start endpoint


def start_body(**overrides):
    return {
        "equipment_id": "GX12-03",
        "discrepancy_code": "DSC-0042",
        "description": "Console-reported hydraulic fault",
        **overrides,
    }


def test_start_creates_workflow_with_atomic_nmc_outbox():
    db = ready_db()
    response = make_client(db).post("/api/workflows", headers=AUTHED, json=start_body())
    assert response.status_code == 200
    workflow_id = response.json()["workflow_id"]
    assert response.json()["operator"] == OPERATOR
    doc = layout.workflow_ref(db, workflow_id).get().to_dict()
    assert doc["status"] == "INTAKE" and doc["equipment_id"] == "GX12-03"
    created = [
        e
        for e in state.reconstruct_audit_trail(db, workflow_id)
        if e["payload"]["reason_code"] == "WORKFLOW_CREATED"
    ]
    assert len(created) == 1 and OPERATOR in created[0]["payload"]["detail"]
    outbox = [
        s.to_dict()["message"]
        for s in layout.workflow_ref(db, workflow_id).collection("outbox").stream()
    ]
    assert [m["envelope"]["schema_version"] for m in outbox] == ["nmc_event.v2"]
    assert outbox[0]["envelope"]["trace_id"] == doc["trace_id"]
    assert outbox[0]["payload"]["equipment_id"] == "GX12-03"


def test_start_drains_the_nmc_to_the_bus_with_workflow_ordering_key():
    db = ready_db()
    published = []
    client = make_client(db, publish=lambda message, key: published.append((message, key)))
    workflow_id = client.post("/api/workflows", headers=AUTHED, json=start_body()).json()[
        "workflow_id"
    ]
    nmc = [m for m, _ in published if m["envelope"]["schema_version"] == "nmc_event.v2"]
    assert len(nmc) == 1 and published[0][1] == workflow_id


def test_start_unauthenticated_creates_nothing():
    db = ready_db()
    before = len(list(db.collection("workflows").stream()))
    response = make_client(db).post("/api/workflows", json=start_body())
    assert response.status_code == 401
    assert len(list(db.collection("workflows").stream())) == before


def test_start_invalid_equipment_is_a_contract_400():
    db = ready_db()
    before = len(list(db.collection("workflows").stream()))
    response = make_client(db).post(
        "/api/workflows", headers=AUTHED, json=start_body(equipment_id="GX13-01")
    )
    assert response.status_code == 400
    assert len(list(db.collection("workflows").stream())) == before


def test_start_missing_description_400():
    response = make_client(ready_db()).post(
        "/api/workflows", headers=AUTHED, json=start_body(description="")
    )
    assert response.status_code == 400


def test_start_rejects_client_supplied_identity():
    response = make_client(ready_db()).post(
        "/api/workflows",
        headers=AUTHED,
        json=start_body(approver_identity="attacker@evil.example"),
    )
    assert response.status_code == 400


# -------------------------------------------------------------- clock control


def test_clock_read_endpoint():
    db = ready_db()
    assert make_client(db).get("/api/clock").json() == {"logical_time": 0}


def test_clock_advance_audits_operator_and_drains_due_workflows():
    db = ready_db()
    set_status(db, "SUSPENDED_AWAITING_PART", due_at=21)
    published = []
    client = make_client(db, publish=lambda message, key: published.append((message, key)))
    response = client.post("/api/clock/advance", headers=AUTHED, json={"days": 21})
    assert response.status_code == 200
    assert response.json()["logical_time"] == 21
    assert response.json()["due_events"] == 1
    assert read_clock(db) == 21
    clock_trail = state.reconstruct_audit_trail(db, CLOCK_AUDIT_WORKFLOW)
    advanced = [e for e in clock_trail if e["payload"]["reason_code"] == "CLOCK_ADVANCED"]
    assert advanced and OPERATOR in (advanced[-1]["payload"].get("detail") or "")
    due = [m for m, _ in published if m["envelope"]["schema_version"] == "due_event.v2"]
    assert len(due) == 1 and due[0]["payload"]["due_at_logical"] == 21


@pytest.mark.parametrize("days", [0, -3, 366, "21", True, None])
def test_clock_advance_rejects_bad_days(days):
    db = ready_db()
    response = make_client(db).post("/api/clock/advance", headers=AUTHED, json={"days": days})
    assert response.status_code == 400
    assert read_clock(db) == 0


def test_clock_advance_unauthenticated_leaves_clock():
    db = ready_db()
    assert make_client(db).post("/api/clock/advance", json={"days": 21}).status_code == 401
    assert read_clock(db) == 0


# -------------------------------------------------------------- activity feed


def test_activity_feed_is_a_newest_first_aud2_projection():
    db = ready_db()
    client = make_client(db)
    client.post("/api/clock/advance", headers=AUTHED, json={"days": 1})
    client.post(f"/api/workflows/{WF}/cancel", headers=AUTHED, json={})
    feed = client.get("/api/activity").json()
    reasons = [e["reason_code"] for e in feed]
    assert "WORKFLOW_CANCELLED" in reasons  # workflow events
    assert "CLOCK_ADVANCED" in reasons  # system-workflow events surface too
    observed = [e["observed_at"] for e in feed]
    assert observed == sorted(observed, reverse=True)
    for event in feed:  # DAT-2 labels ride every row
        assert event["data_origin"] == "SYNTHETIC" and event["trust_state"] == "TRUSTED"
    assert len(client.get("/api/activity?limit=2").json()) == 2


# ------------------------------------------------------------------- bulletin


def test_bulletin_relays_to_cyber_trust_and_stays_metadata_only():
    db = ready_db()
    forwarded = []

    def forward(payload):
        forwarded.append(payload)
        return {"doc_id": payload["doc_id"], "workflow_id": WF, "verdict_published": True}

    client = make_client(db, ingest_forward=forward)
    response = client.post("/api/anomalies/bulletin", headers=AUTHED, json={"workflow_id": WF})
    assert response.status_code == 200
    assert response.json()["verdict_published"] is True
    assert len(forwarded) == 1
    assert forwarded[0]["source"] == "operator-console"
    assert "SYSTEM OVERRIDE" in forwarded[0]["raw_text"]  # the real poisoned text
    # SEC-4 at this surface: the response must never echo document content
    assert "SYSTEM OVERRIDE" not in response.text
    assert "VND-ACT-9901" not in response.text
    injected = [
        e
        for e in state.reconstruct_audit_trail(db, WF)
        if e["payload"]["reason_code"] == "OPERATOR_BULLETIN_INJECTED"
    ]
    assert len(injected) == 1 and OPERATOR in injected[0]["payload"]["detail"]


def test_bulletin_unauthenticated_never_forwards():
    forwarded = []
    client = make_client(ready_db(), ingest_forward=lambda p: forwarded.append(p))
    assert client.post("/api/anomalies/bulletin", json={"workflow_id": WF}).status_code == 401
    assert forwarded == []


def test_bulletin_unknown_workflow_404_and_terminal_409():
    db = ready_db()
    forwarded = []
    client = make_client(db, ingest_forward=lambda p: forwarded.append(p))
    assert (
        client.post(
            "/api/anomalies/bulletin", headers=AUTHED, json={"workflow_id": "wf-nope-000"}
        ).status_code
        == 404
    )
    set_status(db, "CANCELLED")
    assert (
        client.post("/api/anomalies/bulletin", headers=AUTHED, json={"workflow_id": WF}).status_code
        == 409
    )
    assert forwarded == []


def test_bulletin_unconfigured_forwarding_503():
    response = make_client(ready_db()).post(
        "/api/anomalies/bulletin", headers=AUTHED, json={"workflow_id": WF}
    )
    assert response.status_code == 503


def test_bulletin_forwarder_failure_is_a_metadata_only_502():
    def broken(payload):
        raise RuntimeError("SYSTEM OVERRIDE leaked? never echo upstream bodies")

    response = make_client(ready_db(), ingest_forward=broken).post(
        "/api/anomalies/bulletin", headers=AUTHED, json={"workflow_id": WF}
    )
    assert response.status_code == 502
    assert "SYSTEM OVERRIDE" not in response.text
    assert "RuntimeError" in response.text  # type name only


# ---------------------------------------------------------- instance controls


def instance_state(db, instance_id):
    return registry.instance_ref(db, instance_id).get().to_dict()["state"]


def test_instance_fail_and_restore_are_audited_reg5_transitions():
    db = ready_db()
    client = make_client(db)
    response = client.post(
        "/api/catalog/instances/agent-workforce-01", headers=AUTHED, json={"action": "fail"}
    )
    assert response.status_code == 200
    assert instance_state(db, "agent-workforce-01") == "FAILED"
    response = client.post(
        "/api/catalog/instances/agent-workforce-01",
        headers=AUTHED,
        json={"action": "restore"},
    )
    assert response.status_code == 200
    assert instance_state(db, "agent-workforce-01") == "IDLE"
    reasons = trail_reasons(db, registry.REGISTRY_AUDIT_WORKFLOW)
    assert "OPERATOR_INSTANCE_FAILED" in reasons
    assert "OPERATOR_INSTANCE_RESTORED" in reasons
    trail = state.reconstruct_audit_trail(db, registry.REGISTRY_AUDIT_WORKFLOW)
    operator_events = [
        e for e in trail if e["payload"]["reason_code"].startswith("OPERATOR_INSTANCE")
    ]
    assert all(OPERATOR in e["payload"]["detail"] for e in operator_events)


def test_instance_restore_to_reserve():
    db = ready_db()
    client = make_client(db)
    client.post(
        "/api/catalog/instances/agent-workforce-02", headers=AUTHED, json={"action": "fail"}
    )
    response = client.post(
        "/api/catalog/instances/agent-workforce-02",
        headers=AUTHED,
        json={"action": "restore", "target_state": "RESERVE"},
    )
    assert response.status_code == 200
    assert instance_state(db, "agent-workforce-02") == "RESERVE"


def test_instance_state_rule_violations_409():
    db = ready_db()
    client = make_client(db)
    assert (
        client.post(
            "/api/catalog/instances/agent-supply-01",
            headers=AUTHED,
            json={"action": "restore"},
        ).status_code
        == 409
    )  # not FAILED
    client.post("/api/catalog/instances/agent-supply-01", headers=AUTHED, json={"action": "fail"})
    assert (
        client.post(
            "/api/catalog/instances/agent-supply-01", headers=AUTHED, json={"action": "fail"}
        ).status_code
        == 409
    )  # already FAILED
    assert (
        client.post(
            "/api/catalog/instances/agent-supply-01",
            headers=AUTHED,
            json={"action": "restore", "target_state": "ACTIVE"},
        ).status_code
        == 409
    )  # ACTIVE is not an operator-restorable target


def test_instance_unknown_bad_action_and_auth():
    db = ready_db()
    client = make_client(db)
    assert (
        client.post(
            "/api/catalog/instances/agent-nope-01", headers=AUTHED, json={"action": "fail"}
        ).status_code
        == 404
    )
    assert (
        client.post(
            "/api/catalog/instances/agent-supply-01", headers=AUTHED, json={"action": "zap"}
        ).status_code
        == 400
    )
    assert (
        client.post("/api/catalog/instances/agent-supply-01", json={"action": "fail"}).status_code
        == 401
    )
    assert instance_state(db, "agent-supply-01") == "IDLE"


def test_failed_workforce_pool_yields_no_capable_agent():
    db = ready_db()
    make_client(db).post(
        "/api/catalog/instances/agent-workforce-01", headers=AUTHED, json={"action": "fail"}
    )
    with pytest.raises(registry.NoCapableAgent):
        registry.discover(db, "technician-assignment")  # 02 is RESERVE, 01 FAILED


# ------------------------------------------------------ terminal-state guards


@pytest.mark.parametrize("status", ["CANCELLED", "RELEASED"])
def test_nmc_for_finished_workflow_is_an_audited_no_op_before_the_model(status):
    db = ready_db()
    set_status(db, status)
    model = stub_json(DECOMPOSITION)
    assert process_message(
        db, nmc_message(), make_nmc_handler(db, model=model), consumer_identity="forge-orchestrator"
    )
    assert model.call_count == 0  # guard runs BEFORE the reasoning call
    assert wf_status(db) == status
    assert "NMC_EVENT_STALE" in trail_reasons(db)


def test_plan_for_cancelled_workflow_claims_nothing():
    db = ready_db()
    set_status(db, "CANCELLED")
    assert process_message(
        db, plan_message(), make_plan_handler(db), consumer_identity="forge-orchestrator"
    )
    assert "PLAN_STALE" in trail_reasons(db)
    packages = list(layout.workflow_ref(db, WF).collection("work_packages").stream())
    assert packages == []  # no claim, no assignment into a finished workflow
    outbox = [
        s.to_dict()["message"]["envelope"]["schema_version"]
        for s in layout.workflow_ref(db, WF).collection("outbox").stream()
    ]
    assert "work_package_assignment.v2" not in outbox


def test_roster_verdict_for_cancelled_workflow_is_stale_not_completion_evidence():
    db = ready_db()
    set_status(db, "CANCELLED")
    roster = {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=f"wp-workforce-{WF.removeprefix('wf-')}",
            schema_version="roster_assignment.v2",
            event_id=deterministic_event_id("day9-roster", WF),
            trace_id=TRACE,
            idempotency_key="idem-day9-roster",
        ),
        "payload": {
            "assignments": [
                {
                    "task_code": "TC-101",
                    "technician_id": "T-1001",
                    "qualification_id": "Q-HYD-101",
                    "shift": "day",
                }
            ]
        },
    }
    layout.outbox_ref(db, WF, roster["envelope"]["event_id"]).set(
        {"message": roster, "published": True, "enqueued_at": now_iso()}
    )
    verdict = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="validation_verdict.v2",
            event_id=deterministic_event_id("day9-verdict", WF),
            trace_id=TRACE,
            idempotency_key="idem-day9-verdict",
        ),
        "payload": {
            "subject_event_id": roster["envelope"]["event_id"],
            "verdict": "approved",
            "rule_refs": ["SP-QUAL-001"],
            "reasons": ["roster staffed with current qualifications"],
        },
    }
    assert process_message(
        db, verdict, make_verdict_handler(db), consumer_identity="forge-orchestrator"
    )
    reasons = trail_reasons(db)
    assert "VERDICT_STALE" in reasons
    assert "ROSTER_VERDICT_RECORDED" not in reasons


def test_failure_event_for_cancelled_workflow_is_inert():
    db = ready_db()
    set_status(db, "CANCELLED")
    wp_id = f"wp-supply-{WF.removeprefix('wf-')}"
    layout.work_package_ref(db, WF, wp_id).set(
        {
            "work_package_id": wp_id,
            "role": "supply",
            "owner_instance_id": "agent-supply-01",
            "status": "ASSIGNED",
            "assignment_seq": 1,
            "assigned_observed_at": "2026-01-01T00:00:00Z",
        }
    )
    failure = {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=wp_id,
            schema_version="agent_failure_event.v2",
            event_id=deterministic_event_id("day9-failure", WF),
            trace_id=TRACE,
            idempotency_key="idem-day9-failure",
        ),
        "payload": {
            "role": "supply",
            "agent_id": "agent-supply-01",
            "failure_kind": "timeout",
            "attempts": 1,
            "detected_at": "2026-01-01T00:05:00Z",
        },
    }
    before = trail_reasons(db)
    assert process_message(
        db, failure, make_failure_handler(db), consumer_identity="forge-orchestrator"
    )
    assert wf_status(db) == "CANCELLED"
    assert trail_reasons(db) == before  # consumed with zero writes


def test_monitor_skips_terminal_workflows():
    db = ready_db()
    wp_id = f"wp-supply-{WF.removeprefix('wf-')}"
    layout.work_package_ref(db, WF, wp_id).set(
        {
            "work_package_id": wp_id,
            "role": "supply",
            "owner_instance_id": "agent-supply-01",
            "status": "ASSIGNED",
            "assignment_seq": 1,
            "assigned_observed_at": "2026-01-01T00:00:00Z",
        }
    )
    set_status(db, "CANCELLED")
    assert run_monitoring_cycle(db) == []
    set_status(db, "PLANNING")
    assert run_monitoring_cycle(db) == [wp_id]  # control: same package flags


# --------------------------------------------------- create_workflow atomicity


def test_create_workflow_validates_outbox_messages_before_writing():
    db = FakeFirestore()
    layout.clock_ref(db).set({"logical_time": 0})
    bad = nmc_message()
    bad["payload"]["equipment_id"] = "NOT-EQUIPMENT"
    with pytest.raises(ContractViolation):
        state.create_workflow(
            db,
            workflow_id=WF,
            equipment_id="GX12-07",
            trace_id=TRACE,
            logical_time=0,
            outbox_messages=[bad],
        )
    assert layout.workflow_ref(db, WF).get().to_dict() is None


# ------------------------------------------------------------------ page wiring


def test_page_carries_the_operator_console():
    for marker in (
        "Operator Controls (HUM-3)",
        "Live Agent Activity",
        "/api/anomalies/bulletin",
        "/api/clock/advance",
        ".tag.CANCELLED",
        "Cancel workflow",
        "Report NMC",
    ):
        assert marker in PAGE_HTML
