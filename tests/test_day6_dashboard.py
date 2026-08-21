"""Day 6: HUM-1 approval surface + dashboard + spine gate handlers.

Covers: approver identity from the authenticated principal only (HUM-1),
the HUM-2 decision record composed with the gate transition in one commit,
decision consumption (gated release / suspension with due day), REG-4
catalog rendering with DERIVED health, and the full request->decide->apply
loop over the fake Firestore.
"""

import pytest
from fastapi.testclient import TestClient
from services.dashboard import auth
from services.dashboard.app import create_app, derive_health
from services.orchestrator.handlers import (
    build_approval_request,
    make_decision_handler,
    make_verdict_handler,
)
from tests.fake_firestore import FakeFirestore

from forge_common import layout, registry, state
from forge_common.audit import now_iso
from forge_common.bus import process_message
from forge_common.contracts import ContractViolation, validate_message
from forge_common.messages import build_envelope, deterministic_event_id

WF = "wf-day6-001"
TRACE = "c0ffee00c0ffee00c0ffee00c0ffee00"
IAP = {auth.IAP_HEADER: "accounts.google.com:approver@example.com"}


def ready_db(*, status="VALIDATING"):
    db = FakeFirestore()
    registry.load_registry(db)
    layout.clock_ref(db).set({"logical_time": 0})
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    walk = {
        "INTAKE": [],
        "PLANNING": ["PLANNING"],
        "VALIDATING": ["PLANNING", "VALIDATING"],
    }[status if status in ("INTAKE", "PLANNING", "VALIDATING") else "VALIDATING"]
    for target in walk:
        _transition(db, target)
    return db


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


def seed_sourcing_report(db, *, eta_days=21):
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=f"wp-supply-{WF.removeprefix('wf-')}",
            schema_version="sourcing_report.v3",
            event_id=deterministic_event_id("t-sourcing", WF),
            trace_id=TRACE,
            idempotency_key="idem-t-sourcing-day6",
        ),
        "payload": {
            "part_number": "HYD-ACT-4402",
            "part_approved": True,
            "shipment_status": "delayed",
            "eta_days": eta_days,
        },
    }
    validate_message(message)
    layout.outbox_ref(db, WF, message["envelope"]["event_id"]).set(
        {"message": message, "published": False, "enqueued_at": now_iso()}
    )
    return message


def verdict_message(*, verdict="approved", subject="plan", reasons=None):
    subject_event_id = deterministic_event_id("t-subject", WF, subject)
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="validation_verdict.v2",
            event_id=deterministic_event_id("t-verdict", WF, subject, verdict),
            trace_id=TRACE,
            idempotency_key=f"idem-t-verdict-{subject}-{verdict}",
        ),
        "payload": {
            "subject_event_id": subject_event_id,
            "verdict": verdict,
            "rule_refs": ["SP-PART-001"],
            "reasons": reasons or ["plan uses only approved parts for DSC-0042"],
        },
    }
    validate_message(message)
    return message


def wf_status(db):
    return layout.workflow_ref(db, WF).get().to_dict()["status"]


def outbox_of_type(db, schema_version):
    out = []
    for snapshot in layout.workflow_ref(db, WF).collection("outbox").stream():
        message = snapshot.to_dict()["message"]
        if message["envelope"]["schema_version"] == schema_version:
            out.append(message)
    return out


def audit_reasons(db):
    return [
        snapshot.to_dict()["payload"]["reason_code"]
        for snapshot in layout.workflow_ref(db, WF).collection("audit").stream()
    ]


# ---------- auth: approver identity from the principal ONLY (HUM-1) ----------


def test_auth_iap_header_yields_principal():
    assert auth.approver_from_request(dict(IAP)) == "approver@example.com"


def test_auth_bearer_token_uses_verifier():
    headers = {"authorization": "Bearer tok-123"}
    assert auth.approver_from_request(headers, verifier=lambda t: f"v:{t}") == "v:tok-123"


def test_auth_no_principal_raises():
    with pytest.raises(PermissionError):
        auth.approver_from_request({})


# ---------- derived health (REG-1/REG-4) ----------


def test_health_derived_from_heartbeat_staleness():
    now = "2026-08-21T12:00:00Z"
    assert derive_health(None, now=now) == "UNKNOWN"
    assert derive_health("2026-08-21T11:59:50Z", now=now) == "HEALTHY"
    assert derive_health("2026-08-21T11:58:00Z", now=now) == "STALE"


# ---------- verdict handler: gate entry composes the HUM-2 record ----------


def test_approved_verdict_enters_schedule_gate_with_decision_record():
    db = ready_db()
    seed_sourcing_report(db, eta_days=21)
    process_message(
        db, verdict_message(), make_verdict_handler(db), consumer_identity="forge-orchestrator"
    )
    assert wf_status(db) == "AWAITING_SCHEDULE_APPROVAL"
    (request,) = outbox_of_type(db, "approval_request.v2")
    payload = request["payload"]
    assert payload["action_type"] == "schedule_override"
    assert "resume_on_day:21" in payload["constraints"]
    assert payload["applicable_rules"] == ["SP-PART-001"]
    assert any("HYD-ACT-4402" in fact for fact in payload["extracted_facts"])
    assert payload["versions"]["agent_id"] == "forge-orchestrator"


def test_approved_verdict_without_sourcing_evidence_escalates():
    db = ready_db()
    process_message(
        db, verdict_message(), make_verdict_handler(db), consumer_identity="forge-orchestrator"
    )
    assert wf_status(db) == "BLOCKED_AGENT_FAILURE"
    assert outbox_of_type(db, "approval_request.v2") == []
    assert "NO_SOURCING_EVIDENCE" in audit_reasons(db)


def test_vetoed_verdict_returns_to_planning():
    db = ready_db()
    process_message(
        db,
        verdict_message(verdict="vetoed", reasons=["SP-PART-001: part not approved"]),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    assert wf_status(db) == "PLANNING"


def test_stale_verdict_is_audited_noop():
    db = ready_db(status="PLANNING")
    process_message(
        db, verdict_message(), make_verdict_handler(db), consumer_identity="forge-orchestrator"
    )
    assert wf_status(db) == "PLANNING"
    assert "VERDICT_STALE" in audit_reasons(db)


# ---------- dashboard API ----------


def approved_db():
    """Workflow at AWAITING_SCHEDULE_APPROVAL with a pending request."""
    db = ready_db()
    seed_sourcing_report(db)
    process_message(
        db, verdict_message(), make_verdict_handler(db), consumer_identity="forge-orchestrator"
    )
    return db, outbox_of_type(db, "approval_request.v2")[0]["payload"]["approval_id"]


def test_dashboard_serves_page_and_catalog():
    db, _ = approved_db()
    client = TestClient(create_app(db))
    assert "FORGE Readiness Console" in client.get("/").text
    catalog = client.get("/api/catalog").json()
    orchestrator = next(d for d in catalog if d["agent_id"] == "forge-orchestrator")
    assert orchestrator["instances"], "definitions carry their instances"
    assert orchestrator["instances"][0]["health"] in ("UNKNOWN", "HEALTHY", "STALE")


def test_dashboard_workflow_detail_renders_trail_and_pending():
    db, approval_id = approved_db()
    client = TestClient(create_app(db))
    detail = client.get(f"/api/workflows/{WF}").json()
    assert detail["state"]["status"] == "AWAITING_SCHEDULE_APPROVAL"
    assert [p["approval_id"] for p in detail["pending_approvals"]] == [approval_id]
    kinds = [e["event_kind"] for e in detail["audit_trail"]]
    assert "state_change" in kinds
    assert client.get("/api/workflows/wf-nope").status_code == 404


def test_decide_requires_authenticated_principal():
    db, approval_id = approved_db()
    client = TestClient(create_app(db))
    response = client.post(
        f"/api/workflows/{WF}/decide", json={"approval_id": approval_id, "decision": "approved"}
    )
    assert response.status_code == 401
    assert layout.approval_ref(db, WF, approval_id).get().to_dict() is None


def test_decide_rejects_client_supplied_identity():
    db, approval_id = approved_db()
    client = TestClient(create_app(db))
    response = client.post(
        f"/api/workflows/{WF}/decide",
        headers=IAP,
        json={"approval_id": approval_id, "decision": "approved", "approver_identity": "spoof@x"},
    )
    assert response.status_code == 400


def test_decide_validates_target_and_decision():
    db, approval_id = approved_db()
    client = TestClient(create_app(db))
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=IAP,
            json={"approval_id": "apr-none", "decision": "approved"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=IAP,
            json={"approval_id": approval_id, "decision": "maybe"},
        ).status_code
        == 400
    )


def test_decide_records_atomically_with_outbox_copy():
    db, approval_id = approved_db()
    client = TestClient(create_app(db))
    response = client.post(
        f"/api/workflows/{WF}/decide",
        headers=IAP,
        json={"approval_id": approval_id, "decision": "approved", "comment": "part ETA accepted"},
    )
    assert response.status_code == 200
    record = layout.approval_ref(db, WF, approval_id).get().to_dict()
    payload = record["message"]["payload"]
    assert payload["approver_identity"] == "approver@example.com"
    assert payload["decision"] == "approved"
    (bus_copy,) = outbox_of_type(db, "approval_decision.v2")
    assert bus_copy["payload"]["approval_id"] == approval_id
    assert "APPROVAL_RECORDED" in audit_reasons(db)
    # the decided request leaves the pending list
    detail = client.get(f"/api/workflows/{WF}").json()
    assert detail["pending_approvals"] == []
    # duplicate decision on the same approval -> 409, single record stands
    dup = client.post(
        f"/api/workflows/{WF}/decide",
        headers=IAP,
        json={"approval_id": approval_id, "decision": "approved"},
    )
    assert dup.status_code == 404  # no longer pending


# ---------- decision handler: the gate consumes the recorded approval ----------


def decided_db(decision="approved"):
    db, approval_id = approved_db()
    client = TestClient(create_app(db))
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=IAP,
            json={"approval_id": approval_id, "decision": decision},
        ).status_code
        == 200
    )
    (bus_copy,) = outbox_of_type(db, "approval_decision.v2")
    return db, bus_copy


def test_approved_override_suspends_with_due_day_and_consumes_approval():
    db, bus_copy = decided_db()
    process_message(db, bus_copy, make_decision_handler(db), consumer_identity="forge-orchestrator")
    doc = layout.workflow_ref(db, WF).get().to_dict()
    assert doc["status"] == "SUSPENDED_AWAITING_PART"
    assert doc["due_at"] == 21
    approval_id = bus_copy["payload"]["approval_id"]
    assert layout.consumed_approval_ref(db, WF, approval_id).get().to_dict() is not None


def test_rejected_override_returns_to_planning():
    db, bus_copy = decided_db(decision="rejected")
    process_message(db, bus_copy, make_decision_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "PLANNING"


def test_replayed_decision_is_idempotent_then_stale():
    db, bus_copy = decided_db()
    handler = make_decision_handler(db)
    assert process_message(db, bus_copy, handler, consumer_identity="forge-orchestrator") is True
    # exact replay: consumer-scoped inbox marker short-circuits (ICD-6)
    assert process_message(db, bus_copy, handler, consumer_identity="forge-orchestrator") is False
    # same decision under a new event_id: audited stale no-op, state holds
    replay = {
        "envelope": {
            **bus_copy["envelope"],
            "event_id": deterministic_event_id("t-replay", WF),
            "idempotency_key": "idem-t-replay-day6",
        },
        "payload": bus_copy["payload"],
    }
    process_message(db, replay, handler, consumer_identity="forge-orchestrator")
    assert wf_status(db) == "SUSPENDED_AWAITING_PART"
    assert "DECISION_STALE" in audit_reasons(db)


# ---------- the release gate, end to end on the fake ----------


def test_full_release_loop_through_dashboard():
    db, bus_copy = decided_db()
    process_message(db, bus_copy, make_decision_handler(db), consumer_identity="forge-orchestrator")
    _transition(db, "ASSEMBLY_RESUMED")
    process_message(
        db,
        verdict_message(subject="repair-done", reasons=["post-repair checks passed"]),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    assert wf_status(db) == "AWAITING_RELEASE_APPROVAL"
    client = TestClient(create_app(db))
    (pending,) = client.get(f"/api/workflows/{WF}").json()["pending_approvals"]
    assert pending["action_type"] == "equipment_release"
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=IAP,
            json={"approval_id": pending["approval_id"], "decision": "approved"},
        ).status_code
        == 200
    )
    decision = next(
        m
        for m in outbox_of_type(db, "approval_decision.v2")
        if m["payload"]["approval_id"] == pending["approval_id"]
    )
    process_message(db, decision, make_decision_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "RELEASED"
    assert layout.consumed_approval_ref(db, WF, pending["approval_id"]).get().to_dict() is not None


def test_rejected_release_returns_to_rework():
    db, bus_copy = decided_db()
    process_message(db, bus_copy, make_decision_handler(db), consumer_identity="forge-orchestrator")
    _transition(db, "ASSEMBLY_RESUMED")
    process_message(
        db,
        verdict_message(subject="repair-done"),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    client = TestClient(create_app(db))
    (pending,) = client.get(f"/api/workflows/{WF}").json()["pending_approvals"]
    client.post(
        f"/api/workflows/{WF}/decide",
        headers=IAP,
        json={"approval_id": pending["approval_id"], "decision": "rejected"},
    )
    decision = next(
        m
        for m in outbox_of_type(db, "approval_decision.v2")
        if m["payload"]["approval_id"] == pending["approval_id"]
    )
    process_message(db, decision, make_decision_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "ASSEMBLY_RESUMED"


def test_vetoed_release_verdict_blocks_for_human_attention():
    db, bus_copy = decided_db()
    process_message(db, bus_copy, make_decision_handler(db), consumer_identity="forge-orchestrator")
    _transition(db, "ASSEMBLY_RESUMED")
    process_message(
        db,
        verdict_message(subject="repair-done", verdict="vetoed", reasons=["leak detected"]),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    assert wf_status(db) == "BLOCKED_AGENT_FAILURE"
    assert "RELEASE_VERDICT_VETOED" in audit_reasons(db)


def test_decision_record_contract_is_enforced_at_build():
    with pytest.raises(ContractViolation):
        build_approval_request(
            workflow_id=WF,
            trace_id=TRACE,
            action_type="equipment_release",
            subject_event_id=deterministic_event_id("t-x"),
            recommended_action="Release.",
            source_refs=[f"workflow:{WF}"],
            extracted_facts=["checks passed"],
            applicable_rules=[],  # HUM-2: a record with no cited rules is invalid
            constraints=[],
            alternatives_considered=[],
        )


def test_auth_verifier_failure_maps_to_permission_error():
    """Any token-verification exception is a 401-class refusal, never a 500
    (live finding: MalformedError on a platform-stripped signature)."""

    def broken(token):
        raise ValueError("Could not verify token signature.")

    with pytest.raises(PermissionError):
        auth.approver_from_request({"authorization": "Bearer x.y.z"}, verifier=broken)


def _stripped_token(claims):
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}"


def test_platform_trust_path_reads_validated_claims(monkeypatch):
    """Behind Cloud Run IAM (TRUST_PLATFORM_AUTH=1) identity comes from the
    platform-validated token; issuer/expiry/verified-email still enforced."""
    monkeypatch.setenv(auth.PLATFORM_TRUST_ENV, "1")
    good = {
        "iss": "https://accounts.google.com",
        "email": "approver@example.com",
        "email_verified": True,
        "exp": 4102444800,
    }
    headers = {"authorization": f"Bearer {_stripped_token(good)}"}
    assert auth.approver_from_request(headers) == "approver@example.com"
    for bad in (
        {**good, "iss": "https://evil.example"},
        {**good, "email_verified": False},
        {**good, "exp": 1},
        {k: v for k, v in good.items() if k != "email"},
    ):
        with pytest.raises(PermissionError):
            auth.approver_from_request({"authorization": f"Bearer {_stripped_token(bad)}"})
    with pytest.raises(PermissionError):
        auth.approver_from_request({"authorization": "Bearer not-a-jwt"})


def test_platform_trust_off_still_fully_verifies(monkeypatch):
    """Without the deploy-set flag the same stripped token is fully verified
    and refused — the trust shortcut never applies outside Cloud Run."""
    monkeypatch.delenv(auth.PLATFORM_TRUST_ENV, raising=False)
    token = _stripped_token(
        {
            "iss": "https://accounts.google.com",
            "email": "a@b.c",
            "email_verified": True,
            "exp": 4102444800,
        }
    )
    calls = []

    def verifier(t):
        calls.append(t)
        raise ValueError("no signature")

    with pytest.raises(PermissionError):
        auth.approver_from_request({"authorization": f"Bearer {token}"}, verifier=verifier)
    assert calls == [token]
