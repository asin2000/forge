"""Day 7: the FULL operational spine, NMC event to RELEASED (§4 scenario).

One workflow, one trace, every hop through the production handlers:
decompose -> plan -> VALIDATING -> sourcing evidence -> safety verdict ->
schedule gate (HUM-2 record + human decision) -> 21-day suspension ->
Logical Clock due event (trace derived from the WORKFLOW state doc, never a
caller) -> resume -> roster -> safety verdict -> release gate -> RELEASED.
"""

import json

import pytest
from services.orchestrator.handlers import (
    make_decision_handler,
    make_due_handler,
    make_nmc_handler,
    make_plan_handler,
    make_verdict_handler,
)
from services.orchestrator.monitor import run_monitoring_cycle
from services.safety.handlers import make_validation_handler
from tests.adk_stub import stub_json
from tests.fake_firestore import FakeFirestore
from tests.test_day6_dashboard import AUTHED, make_client

from forge_common import layout, registry, state
from forge_common.bus import process_message
from forge_common.clock import advance_clock, emit_due_events
from forge_common.messages import build_envelope, deterministic_event_id

WF = "wf-spine-001"
TRACE = "feedc0defeedc0defeedc0defeedc0de"

DECOMPOSITION = {
    "objectives": {
        "maintenance": "Plan replacement of the failed hydraulic actuator.",
        "supply": "Source the approved actuator and report shipment status.",
    }
}
PLAN = {
    "plan_id": "plan-spine-01",
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
SOURCING = {
    "part_number": "HYD-ACT-4402",
    "part_approved": True,
    "shipment_status": "delayed",
    "eta_days": 21,
}
ROSTER = {
    "assignments": [
        {
            "task_code": "TC-101",
            "technician_id": "T-1001",
            "qualification_id": "Q-HYD-101",
            "shift": "day",
        }
    ]
}


def nmc_message():
    return {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="nmc_event.v2",
            event_id=deterministic_event_id("spine-nmc", WF),
            trace_id=TRACE,
            idempotency_key="idem-spine-nmc",
        ),
        "payload": {
            "equipment_id": "GX12-07",
            "discrepancy_code": "DSC-0042",
            "description": "Failed hydraulic actuator on lift assembly",
            "reported_at": "2026-08-21T09:00:00Z",
        },
    }


def outbox_messages(db, schema=None):
    out = []
    for snapshot in layout.workflow_ref(db, WF).collection("outbox").stream():
        message = snapshot.to_dict()["message"]
        if schema is None or message["envelope"]["schema_version"] == schema:
            out.append(message)
    return out


def wf_status(db):
    return layout.workflow_ref(db, WF).get().to_dict()["status"]


def verdict_stub(subject_event_id):
    return stub_json(
        {
            "subject_event_id": subject_event_id,
            "verdict": "approved",
            "rule_refs": ["SP-PART-001", "SP-HRS-002"],
            "reasons": ["All parts approved for DSC-0042; hours within bounds."],
        }
    )


def spine_db():
    db = FakeFirestore()
    registry.load_registry(db)
    layout.clock_ref(db).set({"logical_time": 0})
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    return db


def run_spine_until_suspension(db):
    """NMC -> ... -> SUSPENDED_AWAITING_PART, returning the workforce
    assignment (held for the post-resume scene)."""
    from services.maintenance.handlers import make_handler as make_maintenance
    from services.supply.handlers import make_handler as make_supply

    orch = "forge-orchestrator"
    process_message(
        db,
        nmc_message(),
        make_nmc_handler(db, model=stub_json(DECOMPOSITION)),
        consumer_identity=orch,
    )
    assert wf_status(db) == "PLANNING"
    assignments = {
        m["payload"]["role"]: m for m in outbox_messages(db, "work_package_assignment.v2")
    }
    assert set(assignments) == {"maintenance", "supply"}

    process_message(
        db,
        assignments["maintenance"],
        make_maintenance(db, stub_json(PLAN)),
        consumer_identity="forge-maintenance",
    )
    (plan_msg,) = outbox_messages(db, "maintenance_action_plan.v2")
    process_message(db, plan_msg, make_plan_handler(db), consumer_identity=orch)
    assert wf_status(db) == "VALIDATING"
    workforce_assignment = next(
        m
        for m in outbox_messages(db, "work_package_assignment.v2")
        if m["payload"]["role"] == "workforce"
    )

    process_message(
        db,
        assignments["supply"],
        make_supply(db, stub_json(SOURCING)),
        consumer_identity="forge-supply",
    )
    assert outbox_messages(db, "sourcing_report.v3")

    process_message(
        db,
        plan_msg,
        make_validation_handler(db, verdict_stub(plan_msg["envelope"]["event_id"])),
        consumer_identity="forge-safety",
    )
    (verdict_msg,) = outbox_messages(db, "validation_verdict.v2")
    process_message(db, verdict_msg, make_verdict_handler(db), consumer_identity=orch)
    assert wf_status(db) == "AWAITING_SCHEDULE_APPROVAL"

    client = make_client(db)
    (pending,) = client.get(f"/api/workflows/{WF}").json()["pending_approvals"]
    assert pending["action_type"] == "schedule_override"
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=AUTHED,
            json={"approval_id": pending["approval_id"], "decision": "approved"},
        ).status_code
        == 200
    )
    decision = next(
        m
        for m in outbox_messages(db, "approval_decision.v2")
        if m["payload"]["approval_id"] == pending["approval_id"]
    )
    process_message(db, decision, make_decision_handler(db), consumer_identity=orch)
    doc = layout.workflow_ref(db, WF).get().to_dict()
    assert (doc["status"], doc["due_at"]) == ("SUSPENDED_AWAITING_PART", 21)
    return workforce_assignment


def test_full_spine_nmc_to_released():
    from services.workforce.handlers import make_handler as make_workforce

    db = spine_db()
    orch = "forge-orchestrator"
    workforce_assignment = run_spine_until_suspension(db)

    # 21 simulated days pass; the Logical Clock emits the due event with the
    # WORKFLOW's root trace read from the state doc — no caller supplies one.
    advance_clock(db, 21)
    assert emit_due_events(db) == [WF]
    (due_msg,) = outbox_messages(db, "due_event.v2")
    assert due_msg["envelope"]["trace_id"] == TRACE
    process_message(db, due_msg, make_due_handler(db), consumer_identity=orch)
    assert wf_status(db) == "ASSEMBLY_RESUMED"

    # the repair executes post-resume: roster -> safety verdict -> release gate
    process_message(
        db,
        workforce_assignment,
        make_workforce(db, stub_json(ROSTER)),
        consumer_identity="forge-workforce",
    )
    (roster_msg,) = outbox_messages(db, "roster_assignment.v2")
    process_message(
        db,
        roster_msg,
        make_validation_handler(db, verdict_stub(roster_msg["envelope"]["event_id"])),
        consumer_identity="forge-safety",
    )
    release_verdict = next(
        m
        for m in outbox_messages(db, "validation_verdict.v2")
        if m["payload"]["subject_event_id"] == roster_msg["envelope"]["event_id"]
    )
    process_message(db, release_verdict, make_verdict_handler(db), consumer_identity=orch)
    assert wf_status(db) == "AWAITING_RELEASE_APPROVAL"

    client = make_client(db)
    (pending,) = client.get(f"/api/workflows/{WF}").json()["pending_approvals"]
    assert pending["action_type"] == "equipment_release"
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=AUTHED,
            json={"approval_id": pending["approval_id"], "decision": "approved"},
        ).status_code
        == 200
    )
    release_decision = next(
        m
        for m in outbox_messages(db, "approval_decision.v2")
        if m["payload"]["approval_id"] == pending["approval_id"]
    )
    process_message(db, release_decision, make_decision_handler(db), consumer_identity=orch)
    assert wf_status(db) == "RELEASED"

    # AUD-2: the trail alone reconstructs the whole spine, in order
    trail = state.reconstruct_audit_trail(db, WF)
    states = [e["payload"]["state_after"] for e in trail if e["payload"].get("state_after")]
    assert states == [
        "INTAKE",
        "PLANNING",
        "VALIDATING",
        "AWAITING_SCHEDULE_APPROVAL",
        "SUSPENDED_AWAITING_PART",
        "ASSEMBLY_RESUMED",
        "AWAITING_RELEASE_APPROVAL",
        "RELEASED",
    ]
    # ORC-5: the suspension really spanned 21 logical days
    suspended = next(
        e for e in trail if e["payload"].get("state_after") == "SUSPENDED_AWAITING_PART"
    )
    resumed = next(e for e in trail if e["payload"].get("state_after") == "ASSEMBLY_RESUMED")
    assert resumed["payload"]["effective_at"] - suspended["payload"]["effective_at"] == 21
    # OBS-1/ICD-4: ONE trace across every bus message and every audit event
    message_traces = {m["envelope"]["trace_id"] for m in outbox_messages(db)}
    audit_traces = {
        s.to_dict()["envelope"]["trace_id"]
        for s in layout.workflow_ref(db, WF).collection("audit").stream()
    }
    assert message_traces == audit_traces == {TRACE}
    # HUM-1: both gates consumed their approvals exactly once
    consumed = [
        s.to_dict() for s in layout.workflow_ref(db, WF).collection("approvals_consumed").stream()
    ]
    assert len(consumed) == 2


def test_due_event_for_resumed_workflow_is_audited_noop():
    db = spine_db()
    run_spine_until_suspension(db)
    advance_clock(db, 21)
    (due_msg,) = (
        [m for m in outbox_messages(db, "due_event.v2")]
        if emit_due_events(db) == [WF]
        else pytest.fail("due event not emitted")
    )
    process_message(db, due_msg, make_due_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "ASSEMBLY_RESUMED"
    # replay under a fresh event id: stale, audited, state holds
    replay = {
        "envelope": {
            **due_msg["envelope"],
            "event_id": deterministic_event_id("spine-due-replay", WF),
            "idempotency_key": "idem-spine-due-replay",
        },
        "payload": due_msg["payload"],
    }
    process_message(db, replay, make_due_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "ASSEMBLY_RESUMED"
    reasons = [
        s.to_dict()["payload"]["reason_code"]
        for s in layout.workflow_ref(db, WF).collection("audit").stream()
    ]
    assert "DUE_EVENT_STALE" in reasons


def test_replayed_plan_is_idempotent_and_state_holds():
    """A redelivered plan (deterministic event id) short-circuits on the
    consumer inbox marker: nothing reruns, the state holds."""
    db = spine_db()
    run_spine_until_suspension(db)
    (plan_msg,) = outbox_messages(db, "maintenance_action_plan.v2")
    assert (
        process_message(db, plan_msg, make_plan_handler(db), consumer_identity="forge-orchestrator")
        is False
    )
    assert wf_status(db) == "SUSPENDED_AWAITING_PART"


def test_vetoed_plan_rework_loop_revalidates():
    """The veto path is a LOOP, not a dead end: VALIDATING -> PLANNING on
    veto, a REVISED plan re-enters VALIDATING (no re-claim of the standing
    workforce package — replans revalidate, ORC-3 reassignment re-staffs),
    and an approving verdict then opens the schedule gate."""
    from services.maintenance.handlers import make_handler as make_maintenance

    db = spine_db()
    orch = "forge-orchestrator"
    process_message(
        db,
        nmc_message(),
        make_nmc_handler(db, model=stub_json(DECOMPOSITION)),
        consumer_identity=orch,
    )
    assignments = {
        m["payload"]["role"]: m for m in outbox_messages(db, "work_package_assignment.v2")
    }
    process_message(
        db,
        assignments["maintenance"],
        make_maintenance(db, stub_json(PLAN)),
        consumer_identity="forge-maintenance",
    )
    (plan_msg,) = outbox_messages(db, "maintenance_action_plan.v2")
    process_message(db, plan_msg, make_plan_handler(db), consumer_identity=orch)
    assert wf_status(db) == "VALIDATING"

    # Safety vetoes the first plan -> rework
    veto = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="validation_verdict.v2",
            event_id=deterministic_event_id("spine-veto", WF),
            trace_id=TRACE,
            idempotency_key="idem-spine-veto",
        ),
        "payload": {
            "subject_event_id": plan_msg["envelope"]["event_id"],
            "verdict": "vetoed",
            "rule_refs": ["SP-HRS-002"],
            "reasons": ["estimated hours exceed the shift bound"],
        },
    }
    process_message(db, veto, make_verdict_handler(db), consumer_identity=orch)
    assert wf_status(db) == "PLANNING"

    # the REVISED plan re-enters VALIDATING without re-claiming the package
    revised = {
        "envelope": {
            **plan_msg["envelope"],
            "event_id": deterministic_event_id("spine-plan-rev2", WF),
            "idempotency_key": "idem-spine-plan-rev2",
        },
        "payload": {**PLAN, "plan_id": "plan-spine-02"},
    }
    process_message(db, revised, make_plan_handler(db), consumer_identity=orch)
    assert wf_status(db) == "VALIDATING"
    packages = [
        s.to_dict()
        for s in layout.workflow_ref(db, WF).collection("work_packages").stream()
        if s.to_dict()["role"] == "workforce"
    ]
    assert len(packages) == 1, "replan must not create or re-claim staffing packages"
    reasons = [
        s.to_dict()["payload"]["reason_code"]
        for s in layout.workflow_ref(db, WF).collection("audit").stream()
    ]
    assert "PLAN_REVISED" in reasons


def test_monitor_derives_trace_from_workflow_doc():
    """ORC-4 rider: with no injected trace_id_for, timeout events ride the
    workflow's ROOT trace from the state doc."""
    db = spine_db()
    process_message(
        db,
        nmc_message(),
        make_nmc_handler(db, model=stub_json(DECOMPOSITION)),
        consumer_identity="forge-orchestrator",
    )
    # far-future injected now: the wall clock must never catch up and make
    # real assignment timestamps newer than the cutoff (bit us on 08-23)
    flagged = run_monitoring_cycle(db, now="2100-01-01T00:00:00.000000Z")
    assert flagged, "assignments past the timeout must flag"
    failures = outbox_messages(db, "agent_failure_event.v2")
    assert failures and all(m["envelope"]["trace_id"] == TRACE for m in failures)


def test_late_non_roster_veto_after_resume_does_not_block():
    """Live finding (run 1 vs run 2): a vetoed verdict on the SOURCING
    report arriving after resume is stale context — only vetoed COMPLETION
    (roster) evidence blocks the release path."""
    db = spine_db()
    workforce_assignment = run_spine_until_suspension(db)
    del workforce_assignment
    advance_clock(db, 21)
    assert emit_due_events(db) == [WF]
    (due_msg,) = outbox_messages(db, "due_event.v2")
    process_message(db, due_msg, make_due_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "ASSEMBLY_RESUMED"
    (sourcing_msg,) = outbox_messages(db, "sourcing_report.v3")
    late_veto = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="validation_verdict.v2",
            event_id=deterministic_event_id("late-veto", WF),
            trace_id=TRACE,
            idempotency_key="idem-late-veto",
        ),
        "payload": {
            "subject_event_id": sourcing_msg["envelope"]["event_id"],
            "verdict": "vetoed",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["shipment delayed beyond threshold"],
        },
    }
    process_message(db, late_veto, make_verdict_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "ASSEMBLY_RESUMED", "a non-roster veto must never block post-resume"


def test_ungrounded_plan_task_codes_fail_at_source_and_at_safety():
    """Live finding (runs 1&3): plans must schedule STAFFABLE work only.
    Belt: the maintenance payload check rejects invented task codes (the
    model retries with the catalog in its prompt; exhaustion -> failure
    event, never a poisoned plan). Suspenders: safety's data engine vetoes
    any unstaffable plan that reaches it."""
    from services.maintenance.handlers import make_handler as make_maintenance

    from forge_common import synthetic_data

    db = spine_db()
    process_message(
        db,
        nmc_message(),
        make_nmc_handler(db, model=stub_json(DECOMPOSITION)),
        consumer_identity="forge-orchestrator",
    )
    assignments = {
        m["payload"]["role"]: m for m in outbox_messages(db, "work_package_assignment.v2")
    }
    invented = {**PLAN, "tasks": [{**PLAN["tasks"][0], "task_code": "TC-999"}]}
    process_message(
        db,
        assignments["maintenance"],
        make_maintenance(db, stub_json(invented)),
        consumer_identity="forge-maintenance",
    )
    assert outbox_messages(db, "maintenance_action_plan.v2") == []
    (failure,) = outbox_messages(db, "agent_failure_event.v2")
    assert "staffable" in json.dumps(failure["payload"])

    # suspenders: the safety engine flags the same defect independently
    violations = synthetic_data.plan_violations(invented, discrepancy_code="DSC-0042")
    assert any(rule == "SP-QUAL-001" and "staffable" in why for rule, why in violations)


def audit_reasons(db):
    return [
        s.to_dict()["payload"]["reason_code"]
        for s in layout.workflow_ref(db, WF).collection("audit").stream()
    ]


def _transition(db, target, **kwargs):
    def _apply(txn):
        return state.apply_transition(
            txn,
            db,
            workflow_id=WF,
            target=target,
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="TEST_WALK",
            **kwargs,
        )

    return layout.run_in_transaction(db, _apply)


def _roster_verdict(db, *, verdict="approved"):
    """A roster verdict referencing REAL completion evidence in the outbox."""
    (roster_msg,) = outbox_messages(db, "roster_assignment.v2")
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="validation_verdict.v2",
            event_id=deterministic_event_id("t-roster-verdict", WF, verdict),
            trace_id=TRACE,
            idempotency_key=f"idem-t-roster-verdict-{verdict}",
        ),
        "payload": {
            "subject_event_id": roster_msg["envelope"]["event_id"],
            "verdict": verdict,
            "rule_refs": ["SP-QUAL-001"],
            "reasons": ["roster judgment"],
        },
    }
    from forge_common.contracts import validate_message

    validate_message(message)
    return message


def suspension_with_roster(db):
    """To SUSPENDED with the workforce roster already executed (live shape)."""
    from services.workforce.handlers import make_handler as make_workforce

    workforce_assignment = run_spine_until_suspension(db)
    process_message(
        db,
        workforce_assignment,
        make_workforce(db, stub_json(ROSTER)),
        consumer_identity="forge-workforce",
    )


def resume(db):
    due_day = layout.workflow_ref(db, WF).get().to_dict()["due_at"]
    advance_clock(db, 21)
    assert emit_due_events(db) == [WF]
    due_msg = next(
        m for m in outbox_messages(db, "due_event.v2") if m["payload"]["due_at_logical"] == due_day
    )
    process_message(db, due_msg, make_due_handler(db), consumer_identity="forge-orchestrator")


def test_vetoed_roster_pre_resume_blocks_at_resume_not_before():
    """Verify-pass blocker: a VETOED roster verdict arriving pre-resume is
    completion evidence, not a plan veto — it must neither send the workflow
    back to PLANNING nor vanish. It re-keys at resume and blocks there."""
    db = spine_db()
    suspension_with_roster(db)
    veto = _roster_verdict(db, verdict="vetoed")
    process_message(db, veto, make_verdict_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "SUSPENDED_AWAITING_PART", "pre-resume roster veto must not transition"
    resume(db)
    (rekeyed,) = [
        m
        for m in outbox_messages(db, "validation_verdict.v2")
        if m["envelope"]["idempotency_key"].startswith("idem-resume-reverdict")
    ]
    assert rekeyed["payload"]["verdict"] == "vetoed"
    process_message(db, rekeyed, make_verdict_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "BLOCKED_AGENT_FAILURE"
    reasons = audit_reasons(db)
    assert "ROSTER_VERDICT_RECORDED" in reasons
    assert "RELEASE_VERDICT_VETOED" in reasons


def test_unknown_subject_veto_is_stale_never_a_plan_veto():
    """Verify-pass blocker (second half): a vetoed verdict whose subject is
    not in THIS workflow's outbox must be an audited no-op at VALIDATING —
    never a PLANNING transition."""
    db = spine_db()
    _transition(db, "PLANNING")
    _transition(db, "VALIDATING")
    foreign = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="validation_verdict.v2",
            event_id=deterministic_event_id("t-foreign-veto", WF),
            trace_id=TRACE,
            idempotency_key="idem-t-foreign-veto",
        ),
        "payload": {
            "subject_event_id": deterministic_event_id("not-in-this-workflow"),
            "verdict": "vetoed",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["foreign subject"],
        },
    }
    process_message(db, foreign, make_verdict_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "VALIDATING"
    assert "VERDICT_STALE" in audit_reasons(db)


def test_raced_completion_evidence_rekeys_when_resume_committed_first():
    """Verify-pass blocker (lost wakeup): if the resume transition is
    already durable when the roster verdict's transaction commits, the
    commit-phase workflow-doc re-read routes the evidence straight onto the
    bus — the marker is never silently parked past the resume."""
    from forge_common.bus import TxnWrites
    from forge_common.bus import process_message as pm

    db = spine_db()
    suspension_with_roster(db)
    resume(db)  # no marker yet -> no rekey; workflow is ASSEMBLY_RESUMED
    assert wf_status(db) == "ASSEMBLY_RESUMED"
    verdict = _roster_verdict(db)

    def stale_handler(message, writes: TxnWrites):
        # simulates the racing verdict handler whose HANDLER-phase status
        # read said SUSPENDED: it parks the evidence — the COMMIT must
        # detect the durable resume and re-key instead
        writes.completion_evidence = {"message": message}

    pm(db, verdict, stale_handler, consumer_identity="forge-orchestrator")
    rekeyed = [
        m
        for m in outbox_messages(db, "validation_verdict.v2")
        if m["envelope"]["idempotency_key"].startswith("idem-resume-reverdict")
    ]
    assert rekeyed, "commit-phase re-read must re-key past a durable resume"
    marker = layout.completion_evidence_ref(db, WF).get().to_dict()
    assert marker is None, "evidence must not ALSO park in the marker"


def test_second_suspension_rekey_does_not_crash_loop():
    """Verify-pass concern: resume #1 -> roster veto -> BLOCKED -> PLANNING
    recovery -> revised plan -> gate -> SUSPENDED #2 -> due #2. The re-key
    id folds the resume epoch, so the second resume commits cleanly instead
    of AlreadyExists-crashing into the DLQ."""
    db = spine_db()
    suspension_with_roster(db)
    process_message(
        db,
        _roster_verdict(db, verdict="vetoed"),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    resume(db)
    (rekey1,) = [
        m
        for m in outbox_messages(db, "validation_verdict.v2")
        if m["envelope"]["idempotency_key"].startswith("idem-resume-reverdict")
    ]
    process_message(db, rekey1, make_verdict_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "BLOCKED_AGENT_FAILURE"

    # human recovery -> revised plan -> revalidation -> second gate pass
    _transition(db, "PLANNING")
    (plan_msg,) = outbox_messages(db, "maintenance_action_plan.v2")
    revised = {
        "envelope": {
            **plan_msg["envelope"],
            "event_id": deterministic_event_id("t-plan-rev2", WF),
            "idempotency_key": "idem-t-plan-rev2",
        },
        "payload": {**plan_msg["payload"], "plan_id": "plan-spine-02"},
    }
    process_message(db, revised, make_plan_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "VALIDATING"
    (sourcing_msg,) = outbox_messages(db, "sourcing_report.v3")
    verdict2 = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="validation_verdict.v2",
            event_id=deterministic_event_id("t-verdict-round2", WF),
            trace_id=TRACE,
            idempotency_key="idem-t-verdict-round2",
        ),
        "payload": {
            "subject_event_id": sourcing_msg["envelope"]["event_id"],
            "verdict": "approved",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["revised plan approved"],
        },
    }
    process_message(db, verdict2, make_verdict_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "AWAITING_SCHEDULE_APPROVAL"
    client = make_client(db)
    (pending,) = client.get(f"/api/workflows/{WF}").json()["pending_approvals"]
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=AUTHED,
            json={"approval_id": pending["approval_id"], "decision": "approved"},
        ).status_code
        == 200
    )
    decision = next(
        m
        for m in outbox_messages(db, "approval_decision.v2")
        if m["payload"]["approval_id"] == pending["approval_id"]
    )
    process_message(db, decision, make_decision_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "SUSPENDED_AWAITING_PART"

    # resume #2: must commit cleanly with a FRESH epoch-keyed re-key
    resume(db)
    assert wf_status(db) == "ASSEMBLY_RESUMED"
    rekeys = [
        m
        for m in outbox_messages(db, "validation_verdict.v2")
        if m["envelope"]["idempotency_key"].startswith("idem-resume-reverdict")
    ]
    assert len(rekeys) == 2, "second resume produces a NEW epoch-keyed re-key"
    assert rekeys[0]["envelope"]["event_id"] != rekeys[1]["envelope"]["event_id"]


def test_orchestrator_binds_no_domain_tools():
    """ORC-2 structural half: the Orchestrator's ADK agent declares ZERO
    tools — it cannot execute domain work by construction. (The behavioral
    half is the acceptance-run audit trail: only orchestration event kinds.)"""
    from services.orchestrator.handlers import make_nmc_handler

    db = spine_db()
    make_nmc_handler(db, model=stub_json(DECOMPOSITION))
    from forge_common.agent_base import AdkTextRunner

    runner = AdkTextRunner(name="forge_orchestrator", model=stub_json(DECOMPOSITION))
    assert not runner._agent.tools, "ORC-2: the orchestrator agent must bind no tools"
