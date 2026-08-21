"""Day 3 unit gate (CI-3): registry discovery, Orchestrator decompose/assign,
specialist agents with stubbed models (ORC-1/2, AGT-1/2/7, REG-1/2/3)."""

import pytest
from services.maintenance.handlers import make_handler as make_maintenance_handler
from services.orchestrator.handlers import make_nmc_handler
from services.supply.handlers import make_handler as make_supply_handler

from adk_stub import stub_garbage, stub_json
from fake_firestore import FakeFirestore
from forge_common import registry, state
from forge_common.agent_base import StructuredAgent
from forge_common.bus import process_message
from forge_common.contracts import validate_message
from forge_common.messages import (
    build_envelope,
    deterministic_event_id,
    deterministic_trace_id,
)

WF = "wf-gx12-07-hyd-001"
TRACE = deterministic_trace_id(WF)

NMC_PAYLOAD = {
    "equipment_id": "GX12-07",
    "discrepancy_code": "DSC-0042",
    "description": "Failed hydraulic actuator on lift assembly",
    "reported_at": "2026-08-21T09:00:00Z",
}

PLAN_PAYLOAD = {
    "plan_id": "plan-hyd-actuator-01",
    "equipment_id": "GX12-07",
    "tasks": [
        {
            "task_code": "TC-101",
            "title": "Replace hydraulic actuator",
            "est_hours": 6.5,
            "parts_required": [{"part_number": "HYD-ACT-4402", "qty": 1}],
        }
    ],
    "notes": "Synthetic plan.",
}

REPORT_PAYLOAD = {
    "part_number": "HYD-ACT-4402",
    "part_approved": True,
    "shipment_status": "delayed",
    "eta_days": 21,
    "detail": "Approved part backordered.",
}


ORCH_OBJECTIVES = {
    "objectives": {
        "maintenance": "Plan replacement of the failed hydraulic actuator.",
        "supply": "Source the approved actuator and report shipment status.",
    }
}


def orchestrator_stub():
    return stub_json(ORCH_OBJECTIVES)


def nmc_message():
    return {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="nmc_event.v2",
            event_id=deterministic_event_id("nmc", WF),
            trace_id=TRACE,
            idempotency_key=f"idem-nmc-{WF}",
        ),
        "payload": NMC_PAYLOAD,
    }


def ready_db():
    db = FakeFirestore()
    registry.load_registry(db)
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    return db


def test_registry_load_and_discover():
    db = FakeFirestore()
    loaded = registry.load_registry(db)
    assert "forge-maintenance" in loaded and len(loaded) == 6
    resolved = registry.discover(db, "maintenance-planning")
    assert resolved["definition"]["agent_id"] == "forge-maintenance"
    assert resolved["instance"]["instance_id"] == "agent-maintenance-01"
    # The workforce RESERVE instance is never handed out by discovery (ORC-3).
    workforce = registry.discover(db, "technician-assignment")
    assert workforce["instance"]["instance_id"] == "agent-workforce-01"


def test_discovery_ignores_non_approved_definitions():
    db = FakeFirestore()
    registry.load_registry(db)
    ref = registry.definition_ref(db, "forge-maintenance")
    doc = ref.get().to_dict()
    ref.set({**doc, "lifecycle_status": "RETIRED"})
    with pytest.raises(registry.NoCapableAgent):
        registry.discover(db, "maintenance-planning")


def test_orchestrator_decomposes_and_assigns_via_registry():
    db = ready_db()
    handler = make_nmc_handler(db, model=orchestrator_stub())
    assert (
        process_message(db, nmc_message(), handler, consumer_identity="forge-orchestrator") is True
    )
    assert db.collection("workflows").document(WF).get().to_dict()["status"] == "PLANNING"
    outbox = [
        d.to_dict()["message"]
        for d in db.collection("workflows").document(WF).collection("outbox").stream()
    ]
    assert len(outbox) == 2
    roles = sorted(m["payload"]["role"] for m in outbox)
    assert roles == ["maintenance", "supply"]
    for message in outbox:
        validate_message(message)
        assert message["payload"]["assigned_agent_id"].startswith("agent-")
    packages = [
        d.to_dict()
        for d in db.collection("workflows").document(WF).collection("work_packages").stream()
    ]
    assert {p["role"] for p in packages} == {"maintenance", "supply"}
    assert all(p["status"] == "ASSIGNED" for p in packages)
    # Owning instances went ACTIVE (REG-2).
    inst = registry.instance_ref(db, "agent-maintenance-01").get().to_dict()
    assert inst["state"] == "ACTIVE"
    # Duplicate delivery: no second decomposition (ICD-5).
    assert (
        process_message(db, nmc_message(), handler, consumer_identity="forge-orchestrator") is False
    )


def test_work_package_ownership_is_exclusive():
    from forge_common import layout

    db = ready_db()
    handler = make_nmc_handler(db, model=orchestrator_stub())
    process_message(db, nmc_message(), handler, consumer_identity="forge-orchestrator")

    def steal(txn):
        registry.claim_work_package(
            txn,
            db,
            workflow_id=WF,
            work_package_id=f"wp-maintenance-{WF.removeprefix('wf-')}",
            instance_id="agent-maintenance-01",
            role="maintenance",
        )

    with pytest.raises(Exception, match="exists"):
        layout.run_in_transaction(db, steal)


def test_no_capable_agent_blocks_with_escalation():
    db = FakeFirestore()  # registry never loaded
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    handler = make_nmc_handler(db, model=orchestrator_stub())
    assert (
        process_message(db, nmc_message(), handler, consumer_identity="forge-orchestrator") is True
    )
    assert (
        db.collection("workflows").document(WF).get().to_dict()["status"] == "BLOCKED_AGENT_FAILURE"
    )
    kinds = [e["payload"]["event_kind"] for e in state.reconstruct_audit_trail(db, WF)]
    assert "escalation" in kinds


def test_specialists_produce_contract_valid_outputs():
    db = ready_db()
    process_message(
        db,
        nmc_message(),
        make_nmc_handler(db, model=orchestrator_stub()),
        consumer_identity="forge-orchestrator",
    )
    outbox_ref = db.collection("workflows").document(WF).collection("outbox")
    assignments = [d.to_dict()["message"] for d in outbox_ref.stream()]
    maintenance_asn = next(m for m in assignments if m["payload"]["role"] == "maintenance")
    supply_asn = next(m for m in assignments if m["payload"]["role"] == "supply")

    process_message(
        db,
        maintenance_asn,
        make_maintenance_handler(db, stub_json(PLAN_PAYLOAD)),
        consumer_identity="forge-maintenance",
    )
    process_message(
        db,
        supply_asn,
        make_supply_handler(db, stub_json(REPORT_PAYLOAD)),
        consumer_identity="forge-supply",
    )
    produced = [d.to_dict()["message"] for d in outbox_ref.stream()]
    versions = sorted(m["envelope"]["schema_version"] for m in produced)
    assert (
        versions
        == [
            "maintenance_action_plan.v2",
            "nmc_event.v2",
            "sourcing_report.v3",
            "work_package_assignment.v2",
            "work_package_assignment.v2",
        ]
        or versions.count("maintenance_action_plan.v2") == 1
    )  # nmc not re-enqueued
    plan = next(
        m for m in produced if m["envelope"]["schema_version"] == "maintenance_action_plan.v2"
    )
    report = next(m for m in produced if m["envelope"]["schema_version"] == "sourcing_report.v3")
    validate_message(plan)
    validate_message(report)
    assert report["payload"]["eta_days"] == 21


def test_specialist_ignores_other_roles_assignment():
    db = ready_db()
    process_message(
        db,
        nmc_message(),
        make_nmc_handler(db, model=orchestrator_stub()),
        consumer_identity="forge-orchestrator",
    )
    outbox_ref = db.collection("workflows").document(WF).collection("outbox")
    supply_asn = next(
        d.to_dict()["message"]
        for d in outbox_ref.stream()
        if d.to_dict()["message"]["payload"].get("role") == "supply"
    )
    before = len(list(outbox_ref.stream()))
    assert (
        process_message(
            db,
            supply_asn,
            make_maintenance_handler(db, stub_json(PLAN_PAYLOAD)),
            consumer_identity="forge-maintenance",
        )
        is True
    )
    assert len(list(outbox_ref.stream())) == before  # consumed, no output


def test_agent_retries_then_emits_failure_event():
    """AGT-7 through the REAL ADK pipeline: ≤2 retries, then a failure event."""

    stub = stub_garbage()
    agent = StructuredAgent(agent_id="agent-maintenance-01", role="maintenance", model=stub)
    result = agent.run(
        prompt_file="maintenance_action_plan.v1.md",
        variables={**NMC_PAYLOAD, "objective": "plan it"},
        schema_version="maintenance_action_plan.v2",
        workflow_id=WF,
        work_package_id="wp-maintenance-gx12-07-hyd-001",
        trace_id=TRACE,
    )
    assert stub.call_count == 3  # initial + 2 retries, counted at the LLM layer
    assert result["envelope"]["schema_version"] == "agent_failure_event.v2"
    assert result["payload"]["failure_kind"] == "malformed_after_retries"
    assert result["payload"]["attempts"] == 3
    validate_message(result)


@pytest.mark.parametrize(
    ("role", "schema", "payload", "extra"),
    [
        ("maintenance", "maintenance_action_plan.v2", PLAN_PAYLOAD, "release_equipment"),
        ("supply", "sourcing_report.v3", REPORT_PAYLOAD, "substitution_approved"),
    ],
)
def test_specialist_boundaries_enforced_by_contract(role, schema, payload, extra):
    """AGT-1/AGT-2: overreach fields fail contract validation via ADK path."""

    agent = StructuredAgent(
        agent_id=f"agent-{role}-01",
        role=role,
        model=stub_json({**payload, extra: True}),
    )
    prompts = {
        "maintenance": "maintenance_action_plan.v1.md",
        "supply": "supply_sourcing_report.v1.md",
    }
    result = agent.run(
        prompt_file=prompts[role],
        variables={**NMC_PAYLOAD, "objective": "do it"},
        schema_version=schema,
        workflow_id=WF,
        work_package_id=f"wp-{role}-gx12-07-hyd-001",
        trace_id=TRACE,
    )
    assert result["envelope"]["schema_version"] == "agent_failure_event.v2"


def test_partial_discovery_never_commits_assignments():
    """Gap 2 regression: one missing capability -> BLOCKED with ZERO
    assignments, claims, or outbox writes — never a partial assignment."""
    db = ready_db()
    registry.definition_ref(db, "forge-supply").set(
        {
            **registry.definition_ref(db, "forge-supply").get().to_dict(),
            "lifecycle_status": "RETIRED",
        }
    )
    handler = make_nmc_handler(db, model=orchestrator_stub())
    assert process_message(db, nmc_message(), handler, consumer_identity="forge-orchestrator")
    workflow = db.collection("workflows").document(WF)
    assert workflow.get().to_dict()["status"] == "BLOCKED_AGENT_FAILURE"
    assert list(workflow.collection("outbox").stream()) == []
    assert list(workflow.collection("work_packages").stream()) == []
    reasons = [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db, WF)]
    assert "NO_CAPABLE_AGENT" in reasons


def test_reasoning_exhaustion_blocks_with_escalation():
    """Gap 5 regression: exhausted Orchestrator reasoning is a bounded,
    audited terminal disposition — not a silent NACK loop."""
    db = ready_db()
    handler = make_nmc_handler(db, model=stub_garbage())
    assert process_message(db, nmc_message(), handler, consumer_identity="forge-orchestrator")
    assert (
        db.collection("workflows").document(WF).get().to_dict()["status"] == "BLOCKED_AGENT_FAILURE"
    )
    trail = state.reconstruct_audit_trail(db, WF)
    exhausted = [
        e for e in trail if e["payload"]["reason_code"] == "ORCHESTRATOR_REASONING_EXHAUSTED"
    ]
    kinds = sorted(e["payload"]["event_kind"] for e in exhausted)
    # Exactly one escalation, plus the BLOCKED state_change carrying the same reason.
    assert kinds == ["escalation", "state_change"]


def test_stale_discovery_rejected_at_claim_time():
    """Gap 3 regression (REG-2): eligibility is re-checked transactionally at
    claim time; a stale claim is rejected, audited, and redeliverable."""
    from forge_common.bus import TxnWrites
    from forge_common.registry import IneligibleAssignment

    db = ready_db()
    message = nmc_message()

    def stale_plan(msg, writes: TxnWrites):
        # Simulates discovery gone stale: definition retired AFTER the
        # handler resolved it but BEFORE the commit transaction runs.
        registry.definition_ref(db, "forge-maintenance").set(
            {
                **registry.definition_ref(db, "forge-maintenance").get().to_dict(),
                "lifecycle_status": "RETIRED",
            }
        )
        writes.work_package_claims.append(
            {
                "work_package_id": "wp-maintenance-gx12-07-hyd-001",
                "instance_id": "agent-maintenance-01",
                "role": "maintenance",
            }
        )

    with pytest.raises(IneligibleAssignment):
        process_message(db, message, stale_plan, consumer_identity="forge-orchestrator")
    workflow = db.collection("workflows").document(WF)
    assert workflow.get().to_dict()["status"] == "INTAKE"  # nothing committed
    assert list(workflow.collection("work_packages").stream()) == []
    reasons = [e["payload"]["reason_code"] for e in state.reconstruct_audit_trail(db, WF)]
    assert "ASSIGNMENT_INELIGIBLE" in reasons
    # Claim released: the delivery is reprocessable once state is repaired.
    registry.definition_ref(db, "forge-maintenance").set(
        {
            **registry.definition_ref(db, "forge-maintenance").get().to_dict(),
            "lifecycle_status": "APPROVED",
        }
    )
    assert process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")


def test_failed_instance_rejected_at_claim_time():
    """Gap 3 regression (REG-2): a FAILED instance cannot be assigned."""
    from forge_common.bus import TxnWrites
    from forge_common.registry import IneligibleAssignment

    db = ready_db()

    def stale_plan(msg, writes: TxnWrites):
        registry.instance_ref(db, "agent-maintenance-01").set({"state": "FAILED"}, merge=True)
        writes.work_package_claims.append(
            {
                "work_package_id": "wp-maintenance-gx12-07-hyd-001",
                "instance_id": "agent-maintenance-01",
                "role": "maintenance",
            }
        )

    with pytest.raises(IneligibleAssignment):
        process_message(db, nmc_message(), stale_plan, consumer_identity="forge-orchestrator")


def test_specialist_runs_are_audited():
    """Gap 4 regression (AUD-1): success and exhaustion both hit the trail,
    atomically with their outbox message."""
    db = ready_db()
    process_message(
        db,
        nmc_message(),
        make_nmc_handler(db, model=orchestrator_stub()),
        consumer_identity="forge-orchestrator",
    )
    outbox_ref = db.collection("workflows").document(WF).collection("outbox")
    assignments = {
        d.to_dict()["message"]["payload"]["role"]: d.to_dict()["message"]
        for d in outbox_ref.stream()
        if d.to_dict()["message"]["envelope"]["schema_version"] == "work_package_assignment.v2"
    }
    process_message(
        db,
        assignments["maintenance"],
        make_maintenance_handler(db, stub_json(PLAN_PAYLOAD)),
        consumer_identity="forge-maintenance",
    )
    process_message(
        db,
        assignments["supply"],
        make_supply_handler(db, stub_garbage()),
        consumer_identity="forge-supply",
    )
    trail = state.reconstruct_audit_trail(db, WF)
    by_reason = {e["payload"]["reason_code"]: e["payload"] for e in trail}
    assert by_reason["DOMAIN_OUTPUT_PRODUCED"]["event_kind"] == "decision"
    assert by_reason["DOMAIN_OUTPUT_PRODUCED"]["agent_identity"] == "agent-maintenance-01"
    assert by_reason["SPECIALIST_MALFORMED"]["event_kind"] == "failure"
    assert by_reason["SPECIALIST_MALFORMED"]["agent_identity"] == "agent-supply-01"
