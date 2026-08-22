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
APPROVER = "approver@example.com"
AUTHED = {"authorization": "Bearer test-token"}
FORGED_PLAIN = {auth.IAP_PLAIN_HEADER: "accounts.google.com:attacker@evil.example"}


def make_client(db):
    """Surface in mode 'verify' with an injected verifier: AUTHED headers
    resolve to APPROVER; identity NEVER comes from client-suppliable
    fields."""
    return TestClient(create_app(db, verifier=lambda token: APPROVER))


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


def verdict_message(*, verdict="approved", subject_event_id, reasons=None):
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="validation_verdict.v2",
            event_id=deterministic_event_id("t-verdict", WF, subject_event_id, verdict),
            trace_id=TRACE,
            idempotency_key=f"idem-t-verdict-{subject_event_id[:8]}-{verdict}",
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


def seed_roster(db):
    message = {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=f"wp-workforce-{WF.removeprefix('wf-')}",
            schema_version="roster_assignment.v2",
            event_id=deterministic_event_id("t-roster", WF),
            trace_id=TRACE,
            idempotency_key="idem-t-roster-day6",
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
    validate_message(message)
    layout.outbox_ref(db, WF, message["envelope"]["event_id"]).set(
        {"message": message, "published": False, "enqueued_at": now_iso()}
    )
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


def _stripped_token(claims):
    import base64
    import json

    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    return f"header.{payload}"


_GOOD_CLAIMS = {
    "iss": "https://accounts.google.com",
    "email": APPROVER,
    "email_verified": True,
    "exp": 4102444800,
}


def test_auth_bearer_token_uses_verifier():
    headers = {"authorization": "Bearer tok-123"}
    assert auth.approver_from_request(headers, verifier=lambda t: f"v:{t}") == "v:tok-123"


def test_auth_no_principal_raises_in_every_mode():
    for mode in ("verify", "cloudrun-iam", "iap"):
        with pytest.raises(PermissionError):
            auth.approver_from_request({}, mode=mode)


def test_auth_unknown_mode_refuses_all_principals():
    with pytest.raises(PermissionError):
        auth.approver_from_request(dict(AUTHED), mode="trust-everyone", verifier=lambda t: "x")


def test_plain_iap_header_never_grants_identity():
    """Google's IAP guidance: the plain x-goog-authenticated-user-email
    header is client-forgeable and MUST NOT be an identity source — in any
    mode, with or without other credentials."""
    for mode in ("verify", "cloudrun-iam", "iap"):
        with pytest.raises(PermissionError):
            auth.approver_from_request(dict(FORGED_PLAIN), mode=mode)
    # forged header alongside a valid credential: identity is the CREDENTIAL's
    headers = {**FORGED_PLAIN, "authorization": "Bearer tok"}
    assert (
        auth.approver_from_request(headers, mode="verify", verifier=lambda t: APPROVER) == APPROVER
    )
    platform = {**FORGED_PLAIN, "authorization": f"Bearer {_stripped_token(_GOOD_CLAIMS)}"}
    assert auth.approver_from_request(platform, mode="cloudrun-iam") == APPROVER


def test_iap_mode_uses_only_the_signed_assertion():
    seen = []

    def iap_verifier(assertion):
        seen.append(assertion)
        return "verified@example.com"

    headers = {**FORGED_PLAIN, auth.IAP_ASSERTION_HEADER: "signed.jwt.value"}
    assert (
        auth.approver_from_request(headers, mode="iap", iap_verifier=iap_verifier)
        == "verified@example.com"
    )
    assert seen == ["signed.jwt.value"]


def test_iap_assertion_verification_failure_is_permission_error():
    def broken(assertion):
        raise ValueError("audience mismatch")

    headers = {auth.IAP_ASSERTION_HEADER: "signed.jwt.value"}
    with pytest.raises(PermissionError):
        auth.approver_from_request(headers, mode="iap", iap_verifier=broken)


def test_iap_default_verifier_fails_closed_without_audience(monkeypatch):
    monkeypatch.delenv("IAP_AUDIENCE", raising=False)
    with pytest.raises(PermissionError):
        auth._iap_verify("any.assertion.value")


# ---------- derived health (REG-1/REG-4) ----------


def test_health_derived_from_heartbeat_staleness():
    now = "2026-08-21T12:00:00Z"
    assert derive_health(None, now=now) == "UNKNOWN"
    assert derive_health("2026-08-21T11:59:50Z", now=now) == "HEALTHY"
    assert derive_health("2026-08-21T11:58:00Z", now=now) == "STALE"


# ---------- verdict handler: gate entry composes the HUM-2 record ----------


def test_approved_verdict_enters_schedule_gate_with_decision_record():
    db = ready_db()
    sourcing = seed_sourcing_report(db, eta_days=21)
    process_message(
        db,
        verdict_message(subject_event_id=sourcing["envelope"]["event_id"]),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    assert wf_status(db) == "AWAITING_SCHEDULE_APPROVAL"
    (request,) = outbox_of_type(db, "approval_request.v2")
    payload = request["payload"]
    assert payload["action_type"] == "schedule_override"
    assert "resume_on_day:21" in payload["constraints"]
    assert payload["applicable_rules"] == ["SP-PART-001"]
    assert any("HYD-ACT-4402" in fact for fact in payload["extracted_facts"])
    assert payload["versions"]["agent_id"] == "forge-orchestrator"


def test_verdict_before_sourcing_report_holds_then_report_opens_gate():
    """Live push delivery makes arrival order arbitrary: a verdict landing
    BEFORE the sourcing report is HELD (never a block); the report's
    arrival opens the gate — whichever lands last opens it."""
    from services.orchestrator.handlers import make_sourcing_report_handler

    db = ready_db()
    # the verdict's subject is the plan; seed the plan message it judges
    plan = {
        "envelope": build_envelope(
            workflow_id=WF,
            work_package_id=f"wp-maintenance-{WF.removeprefix('wf-')}",
            schema_version="maintenance_action_plan.v2",
            event_id=deterministic_event_id("t-plan", WF),
            trace_id=TRACE,
            idempotency_key="idem-t-plan-day6",
        ),
        "payload": {
            "plan_id": "plan-day6-01",
            "equipment_id": "GX12-07",
            "tasks": [
                {
                    "task_code": "TC-101",
                    "title": "Replace actuator",
                    "est_hours": 6.5,
                    "parts_required": [{"part_number": "HYD-ACT-4402", "qty": 1}],
                }
            ],
        },
    }
    validate_message(plan)
    layout.outbox_ref(db, WF, plan["envelope"]["event_id"]).set(
        {"message": plan, "published": False, "enqueued_at": now_iso()}
    )
    verdict = verdict_message(subject_event_id=plan["envelope"]["event_id"])
    # on the real bus the verdict sits in the workflow outbox (safety
    # published it there); the sourcing handler's held-verdict lookup
    # depends on that record
    layout.outbox_ref(db, WF, verdict["envelope"]["event_id"]).set(
        {"message": verdict, "published": True, "enqueued_at": now_iso()}
    )
    process_message(db, verdict, make_verdict_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "VALIDATING"
    assert outbox_of_type(db, "approval_request.v2") == []
    assert "VERDICT_HELD_AWAITING_EVIDENCE" in audit_reasons(db)
    # now the sourcing report lands: ITS handler opens the gate
    sourcing = seed_sourcing_report(db)
    process_message(
        db, sourcing, make_sourcing_report_handler(db), consumer_identity="forge-orchestrator"
    )
    assert wf_status(db) == "AWAITING_SCHEDULE_APPROVAL"
    (request,) = outbox_of_type(db, "approval_request.v2")
    assert "resume_on_day:21" in request["payload"]["constraints"]


def test_vetoed_verdict_returns_to_planning():
    db = ready_db()
    sourcing = seed_sourcing_report(db)
    process_message(
        db,
        verdict_message(
            verdict="vetoed",
            subject_event_id=sourcing["envelope"]["event_id"],
            reasons=["SP-PART-001: part not approved"],
        ),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    assert wf_status(db) == "PLANNING"


def test_stale_verdict_is_audited_noop():
    db = ready_db(status="PLANNING")
    sourcing = seed_sourcing_report(db)
    process_message(
        db,
        verdict_message(subject_event_id=sourcing["envelope"]["event_id"]),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    assert wf_status(db) == "PLANNING"
    assert "VERDICT_STALE" in audit_reasons(db)


# ---------- dashboard API ----------


def approved_db():
    """Workflow at AWAITING_SCHEDULE_APPROVAL with a pending request."""
    db = ready_db()
    sourcing = seed_sourcing_report(db)
    process_message(
        db,
        verdict_message(subject_event_id=sourcing["envelope"]["event_id"]),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    return db, outbox_of_type(db, "approval_request.v2")[0]["payload"]["approval_id"]


def test_dashboard_serves_page_and_catalog():
    db, _ = approved_db()
    client = make_client(db)
    assert "FORGE Readiness Console" in client.get("/").text
    catalog = client.get("/api/catalog").json()
    orchestrator = next(d for d in catalog if d["agent_id"] == "forge-orchestrator")
    assert orchestrator["instances"], "definitions carry their instances"
    assert orchestrator["instances"][0]["health"] in ("UNKNOWN", "HEALTHY", "STALE")


def test_dashboard_workflow_detail_renders_trail_and_pending():
    db, approval_id = approved_db()
    client = make_client(db)
    detail = client.get(f"/api/workflows/{WF}").json()
    assert detail["state"]["status"] == "AWAITING_SCHEDULE_APPROVAL"
    assert [p["approval_id"] for p in detail["pending_approvals"]] == [approval_id]
    kinds = [e["event_kind"] for e in detail["audit_trail"]]
    assert "state_change" in kinds
    assert client.get("/api/workflows/wf-nope").status_code == 404


def test_decide_requires_authenticated_principal():
    db, approval_id = approved_db()
    client = make_client(db)
    response = client.post(
        f"/api/workflows/{WF}/decide", json={"approval_id": approval_id, "decision": "approved"}
    )
    assert response.status_code == 401
    assert layout.approval_ref(db, WF, approval_id).get().to_dict() is None


def test_decide_rejects_client_supplied_identity():
    db, approval_id = approved_db()
    client = make_client(db)
    response = client.post(
        f"/api/workflows/{WF}/decide",
        headers=AUTHED,
        json={"approval_id": approval_id, "decision": "approved", "approver_identity": "spoof@x"},
    )
    assert response.status_code == 400


def test_decide_validates_target_and_decision():
    db, approval_id = approved_db()
    client = make_client(db)
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=AUTHED,
            json={"approval_id": "apr-none", "decision": "approved"},
        ).status_code
        == 404
    )
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=AUTHED,
            json={"approval_id": approval_id, "decision": "maybe"},
        ).status_code
        == 400
    )


def test_decide_records_atomically_with_outbox_copy():
    db, approval_id = approved_db()
    client = make_client(db)
    response = client.post(
        f"/api/workflows/{WF}/decide",
        headers=AUTHED,
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
        headers=AUTHED,
        json={"approval_id": approval_id, "decision": "approved"},
    )
    assert dup.status_code == 404  # no longer pending


# ---------- decision handler: the gate consumes the recorded approval ----------


def decided_db(decision="approved"):
    db, approval_id = approved_db()
    client = make_client(db)
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=AUTHED,
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
    roster = seed_roster(db)
    process_message(
        db,
        verdict_message(
            subject_event_id=roster["envelope"]["event_id"], reasons=["post-repair checks passed"]
        ),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
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
    roster = seed_roster(db)
    process_message(
        db,
        verdict_message(subject_event_id=roster["envelope"]["event_id"]),
        make_verdict_handler(db),
        consumer_identity="forge-orchestrator",
    )
    client = make_client(db)
    (pending,) = client.get(f"/api/workflows/{WF}").json()["pending_approvals"]
    client.post(
        f"/api/workflows/{WF}/decide",
        headers=AUTHED,
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
    roster = seed_roster(db)
    process_message(
        db,
        verdict_message(
            subject_event_id=roster["envelope"]["event_id"],
            verdict="vetoed",
            reasons=["leak detected"],
        ),
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


def test_cloudrun_mode_reads_platform_validated_claims(monkeypatch):
    """In mode cloudrun-iam (set only by the deploy, under
    --no-allow-unauthenticated) identity comes from the platform-validated
    token; issuer/expiry/verified-email still enforced."""
    monkeypatch.setenv(auth.AUTH_MODE_ENV, "cloudrun-iam")
    headers = {"authorization": f"Bearer {_stripped_token(_GOOD_CLAIMS)}"}
    assert auth.approver_from_request(headers) == APPROVER
    for bad in (
        {**_GOOD_CLAIMS, "iss": "https://evil.example"},
        {**_GOOD_CLAIMS, "email_verified": False},
        {**_GOOD_CLAIMS, "exp": 1},
        {k: v for k, v in _GOOD_CLAIMS.items() if k != "email"},
    ):
        with pytest.raises(PermissionError):
            auth.approver_from_request({"authorization": f"Bearer {_stripped_token(bad)}"})
    with pytest.raises(PermissionError):
        auth.approver_from_request({"authorization": "Bearer not-a-jwt"})


def test_default_mode_still_fully_verifies(monkeypatch):
    """Without the deploy-set mode the same stripped token is fully verified
    and refused — the platform shortcut never applies outside Cloud Run."""
    monkeypatch.delenv(auth.AUTH_MODE_ENV, raising=False)
    token = _stripped_token({**_GOOD_CLAIMS, "email": "a@b.c"})
    calls = []

    def verifier(t):
        calls.append(t)
        raise ValueError("no signature")

    with pytest.raises(PermissionError):
        auth.approver_from_request({"authorization": f"Bearer {token}"}, verifier=verifier)
    assert calls == [token]


def test_whoami_reflects_principal_and_forged_header_gets_nothing():
    """App-layer forged-header negatives: the plain IAP header alone is 401;
    alongside a real credential the identity is the credential's."""
    db, _ = approved_db()
    client = make_client(db)
    assert client.get("/api/whoami").status_code == 401
    assert client.get("/api/whoami", headers=FORGED_PLAIN).status_code == 401
    both = client.get("/api/whoami", headers={**FORGED_PLAIN, **AUTHED})
    assert both.status_code == 200
    assert both.json() == {"approver_identity": APPROVER}


def test_forged_header_cannot_decide():
    db, approval_id = approved_db()
    client = make_client(db)
    response = client.post(
        f"/api/workflows/{WF}/decide",
        headers=FORGED_PLAIN,
        json={"approval_id": approval_id, "decision": "approved"},
    )
    assert response.status_code == 401
    assert layout.approval_ref(db, WF, approval_id).get().to_dict() is None


def test_decision_continues_request_trace_exactly():
    """OBS-1/ICD-4: ONE trace per workflow — the decision's envelope carries
    the approval_request's trace_id verbatim (which is itself the workflow
    trace the verdict rode in on). No second application trace identifier."""
    db, approval_id = approved_db()
    (request,) = outbox_of_type(db, "approval_request.v2")
    assert request["envelope"]["trace_id"] == TRACE
    client = make_client(db)
    assert (
        client.post(
            f"/api/workflows/{WF}/decide",
            headers=AUTHED,
            json={"approval_id": approval_id, "decision": "approved"},
        ).status_code
        == 200
    )
    (decision,) = outbox_of_type(db, "approval_decision.v2")
    assert decision["envelope"]["trace_id"] == request["envelope"]["trace_id"] == TRACE
    # the authoritative record and its audit carry the same trace
    record = layout.approval_ref(db, WF, approval_id).get().to_dict()
    assert record["message"]["envelope"]["trace_id"] == TRACE

    def audit_traces():
        return {
            snapshot.to_dict()["envelope"]["trace_id"]
            for snapshot in layout.workflow_ref(db, WF).collection("audit").stream()
        }

    assert audit_traces() == {TRACE}
    # ...and STILL a single trace after the gate consumes the approval — the
    # decision handler's transition + audits must ride the same trace too
    process_message(db, decision, make_decision_handler(db), consumer_identity="forge-orchestrator")
    assert wf_status(db) == "SUSPENDED_AWAITING_PART"
    assert audit_traces() == {TRACE}


def test_cloudrun_mode_malformed_claim_shapes_are_401_not_500(monkeypatch):
    """Adversarial-verify finding: a payload that decodes to a non-dict JSON
    value, or a non-numeric exp, must be refused as PermissionError (401) —
    never escape as AttributeError/TypeError (500)."""
    import base64
    import json as jsonlib

    monkeypatch.setenv(auth.AUTH_MODE_ENV, "cloudrun-iam")

    def raw_token(payload_obj):
        payload = base64.urlsafe_b64encode(jsonlib.dumps(payload_obj).encode()).decode().rstrip("=")
        return f"header.{payload}"

    for payload_obj in (
        123,
        "str",
        [1, 2, 3],
        {**_GOOD_CLAIMS, "exp": "soon"},
        {**_GOOD_CLAIMS, "exp": [1]},
        {**_GOOD_CLAIMS, "exp": {}},
    ):
        with pytest.raises(PermissionError):
            auth.approver_from_request({"authorization": f"Bearer {raw_token(payload_obj)}"})


def test_ci9_planned_with_code_caught_even_with_emptied_source_list():
    """Adversarial-verify finding: the planned-vs-code guard must key off the
    services/<key>/ directory, not only the entry's declared source list."""
    import yaml
    from scripts.check_config import ROOT, check_manifest

    manifest = yaml.safe_load((ROOT / "architecture" / "manifest.yaml").read_text())
    registry = yaml.safe_load((ROOT / "agents" / "registry.yaml").read_text())
    residency = yaml.safe_load((ROOT / "infra" / "residency.yaml").read_text())
    manifest["services"]["supply"]["status"] = "planned"
    manifest["services"]["supply"]["source"] = []
    failures = check_manifest(manifest, registry, residency)
    assert any("supply" in f and "planned" in f for f in failures)


def test_audit_trail_render_carries_dat2_labels():
    """DAT-2: data_origin and trust_state are displayed in the rendered
    audit trail (API layer feeding the dashboard table)."""
    db, _ = approved_db()
    client = make_client(db)
    trail = client.get(f"/api/workflows/{WF}").json()["audit_trail"]
    assert trail
    for entry in trail:
        assert entry["data_origin"] == "SYNTHETIC"
        assert entry["trust_state"] == "TRUSTED"
