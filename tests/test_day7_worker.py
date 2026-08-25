"""Day 7: the production worker runtime (services/worker.py).

The same push surface Cloud Run serves, exercised end to end: the full
spine runs THROUGH worker HTTP pushes (base64 push envelopes, filtered
routing by schema/wp_role attributes, drain-after-commit republish), plus
the delivery-semantics matrix (ack / retry / terminal-reject / DLQ).
"""

import base64
import json
import re

from fastapi.testclient import TestClient
from google.adk.models import LlmResponse
from google.genai import types
from services.worker import create_worker
from tests.adk_stub import StubLlm, stub_json
from tests.fake_firestore import FakeFirestore
from tests.test_day6_dashboard import AUTHED, make_client
from tests.test_day7_spine import DECOMPOSITION, PLAN, ROSTER, SOURCING, TRACE, WF, nmc_message

from forge_common import layout, otel, registry, state
from forge_common.clock import advance_clock, emit_due_events

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"


class SubjectEchoLlm(StubLlm):
    """Safety stub for multi-message runs: echoes the subject_event_id it
    finds in the prompt, so one handler instance can approve every subject
    (the payload_check still enforces subject == consumed event id)."""

    async def generate_content_async(self, llm_request, stream: bool = False):
        text = " ".join(
            part.text or "" for content in llm_request.contents for part in content.parts or []
        )
        subject = re.search(rf"subject_event_id[^0-9a-f]*({_UUID})", text).group(1)
        reply = json.dumps(
            {
                "subject_event_id": subject,
                "verdict": "approved",
                "rule_refs": ["SP-PART-001", "SP-HRS-002"],
                "reasons": ["All parts approved for DSC-0042; hours within bounds."],
            }
        )
        yield LlmResponse(content=types.Content(role="model", parts=[types.Part(text=reply)]))


def push_envelope(message, attributes):
    return {
        "message": {
            "data": base64.b64encode(json.dumps(message).encode()).decode(),
            "attributes": attributes,
            "messageId": "m-1",
        },
        "subscription": "projects/test/subscriptions/test",
    }


# THE canonical routing map — the tests route exactly as the deploy does
from forge_common.pubsub import ROUTING as ROUTES  # noqa: E402


def test_worker_full_spine_via_push():
    """NMC -> RELEASED entirely through the production push surface: each
    hop is a base64 push envelope routed by the deploy's filter semantics
    with IMMEDIATE delivery (live shape — no scene-timing deferral): the
    roster executes during validation, its verdict is recorded, and the due
    handler re-keys it at resume to open the release gate."""
    db = FakeFirestore()
    registry.load_registry(db)
    layout.clock_ref(db).set({"logical_time": 0})
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )

    bus = []

    def publish(message, ordering_key):
        from forge_common.pubsub import routing_attributes

        attributes = routing_attributes(message)
        otel.inject_trace_attributes(attributes, message["envelope"])
        bus.append((message, attributes))

    workers = {
        role: TestClient(create_worker(db, role, model=model, publish=publish))
        for role, model in [
            ("orchestrator", stub_json(DECOMPOSITION)),
            ("maintenance", stub_json(PLAN)),
            ("supply", stub_json(SOURCING)),
            ("workforce", stub_json(ROSTER)),
            ("safety", SubjectEchoLlm()),
        ]
    }

    def wf_status():
        return layout.workflow_ref(db, WF).get().to_dict()["status"]

    def pump(max_rounds=60):
        rounds = 0
        while bus and rounds < max_rounds:
            rounds += 1
            message, attributes = bus.pop(0)
            schema = message["envelope"]["schema_version"]
            if schema == "work_package_assignment.v2":
                targets = [message["payload"]["role"]]
            else:
                targets = ROUTES[schema]
            for role in targets:
                response = workers[role].post("/", json=push_envelope(message, attributes))
                assert response.status_code in (200, 204), (schema, role, response.status_code)
        assert not bus, "bus did not quiesce"

    publish(nmc_message(), WF)
    pump()
    assert wf_status() == "AWAITING_SCHEDULE_APPROVAL"

    dashboard = make_client(db)

    def decide(expected_action):
        (pending,) = dashboard.get(f"/api/workflows/{WF}").json()["pending_approvals"]
        assert pending["action_type"] == expected_action
        assert (
            dashboard.post(
                f"/api/workflows/{WF}/decide",
                headers=AUTHED,
                json={"approval_id": pending["approval_id"], "decision": "approved"},
            ).status_code
            == 200
        )

    decide("schedule_override")
    # the dashboard's outbox copy reaches the bus on the next drain — any
    # worker's post-commit drain does it; here the clock advance's emitter
    # runs first, so drain explicitly as the deploy's dashboard does
    from forge_common.bus import drain_outbox

    drain_outbox(db, WF, publish)
    pump()
    doc = layout.workflow_ref(db, WF).get().to_dict()
    assert (doc["status"], doc["due_at"]) == ("SUSPENDED_AWAITING_PART", 21)

    advance_clock(db, 21)
    assert emit_due_events(db) == [WF]
    drain_outbox(db, WF, publish)
    pump()
    # no deferral: the roster ran DURING validation (live immediate delivery);
    # its approved verdict was recorded and re-keyed at resume — one pump
    # carries the workflow straight through ASSEMBLY_RESUMED (the state
    # history assertion below proves the intermediate state) to the gate
    assert wf_status() == "AWAITING_RELEASE_APPROVAL"

    decide("equipment_release")
    drain_outbox(db, WF, publish)
    pump()
    assert wf_status() == "RELEASED"

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
    audit_traces = {
        s.to_dict()["envelope"]["trace_id"]
        for s in layout.workflow_ref(db, WF).collection("audit").stream()
    }
    assert audit_traces == {TRACE}


def test_worker_delivery_semantics():
    db = FakeFirestore()
    registry.load_registry(db)
    layout.clock_ref(db).set({"logical_time": 0})
    client = TestClient(
        create_worker(db, "orchestrator", model=stub_json(DECOMPOSITION)),
        raise_server_exceptions=False,
    )
    assert client.get("/healthz").json()["status"] == "serving"

    # undecodable transport payload -> 500 (Pub/Sub retries -> DLQ)
    bad = {"message": {"data": "!!!not-base64!!!", "attributes": {}}}
    assert client.post("/", json=bad).status_code == 500

    # schema this role does not handle -> 204 ack
    unknown = dict(nmc_message())
    unknown = {
        **unknown,
        "envelope": {**unknown["envelope"], "schema_version": "approval_request.v2"},
    }
    assert client.post("/", json=push_envelope(unknown, {})).status_code == 204

    # contract violation -> 200 (terminally rejected AND audited)
    malformed = nmc_message()
    del malformed["payload"]["equipment_id"]
    assert client.post("/", json=push_envelope(malformed, {})).status_code == 200
    audits = [
        s.to_dict()["payload"]["reason_code"]
        for s in layout.workflow_ref(db, malformed["envelope"]["workflow_id"])
        .collection("audit")
        .stream()
    ]
    assert "CONTRACT_VIOLATION_REJECTED" in audits

    # claim held elsewhere -> 429 (transport retries later). The claim path
    # itself is emulator-proven; the unit contract here is the STATUS MAPPING.
    import services.worker as worker_module

    from forge_common.bus import DeliveryInProgress

    def raising_handler(message, writes):
        raise DeliveryInProgress("claim held by another instance")

    original = worker_module.build_handlers
    try:
        worker_module.build_handlers = lambda db_, role_, model_: {"nmc_event.v2": raising_handler}
        raiser = TestClient(
            worker_module.create_worker(db, "orchestrator"), raise_server_exceptions=False
        )
        assert raiser.post("/", json=push_envelope(nmc_message(), {})).status_code == 429
    finally:
        worker_module.build_handlers = original


def test_output_side_contract_violation_reaches_the_dlq_path():
    """Verify-pass finding: only INPUT-boundary violations (audited) may be
    acked. A handler producing a nonconforming message must 500 to the DLQ —
    acking would be silent loss of a valid TRUSTED input."""
    import services.worker as worker_module

    from forge_common.contracts import ContractViolation

    db = FakeFirestore()

    def bad_producer(message, writes):
        raise ContractViolation("verdict.v9", ["made-up output schema"])

    original = worker_module.build_handlers
    try:
        worker_module.build_handlers = lambda db_, role_, model_: {"nmc_event.v2": bad_producer}
        client = TestClient(
            worker_module.create_worker(db, "orchestrator"), raise_server_exceptions=False
        )
        assert client.post("/", json=push_envelope(nmc_message(), {})).status_code == 500
    finally:
        worker_module.build_handlers = original

    # the INPUT boundary still acks (audited terminal rejection)
    malformed = nmc_message()
    del malformed["payload"]["equipment_id"]
    client = TestClient(
        create_worker(db, "orchestrator", model=stub_json(DECOMPOSITION)),
        raise_server_exceptions=False,
    )
    assert client.post("/", json=push_envelope(malformed, {})).status_code == 200


def test_tick_runs_monitor_clock_and_sweeps_undrained_outboxes():
    """ORC-4/ORC-5 as DEPLOYED (verify-pass finding: neither ran in
    production, and dashboard decisions had no guaranteed drainer). The
    orchestrator's /tick flags timeouts, emits due events, and publishes
    every undrained outbox record."""
    from forge_common.audit import now_iso
    from forge_common.clock import advance_clock

    db = FakeFirestore()
    registry.load_registry(db)
    layout.clock_ref(db).set({"logical_time": 0})
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    published = []
    client = TestClient(
        create_worker(
            db,
            "orchestrator",
            model=stub_json(DECOMPOSITION),
            publish=lambda m, k: published.append(m),
            # entrant QA P2: the registry carries real deployed endpoints —
            # a unit test must never fall through to the OIDC prober
            health_prober=lambda endpoint: False,
        )
    )
    # an undrained record (a dashboard decision whose post-commit drain died)
    orphan = nmc_message()
    layout.outbox_ref(db, WF, orphan["envelope"]["event_id"]).set(
        {"message": orphan, "published": False, "enqueued_at": now_iso()}
    )

    # a suspended workflow past its due day
    def _suspend(txn):
        state.apply_transition(
            txn,
            db,
            workflow_id=WF,
            target="PLANNING",
            agent_identity="forge-orchestrator",
            trace_id=TRACE,
            reason_code="TEST_WALK",
        )

    layout.run_in_transaction(db, _suspend)
    advance_clock(db, 30)

    result = client.post("/tick").json()
    assert result["outbox_published"] >= 1
    assert any(m["envelope"]["event_id"] == orphan["envelope"]["event_id"] for m in published)
    record = layout.outbox_ref(db, WF, orphan["envelope"]["event_id"]).get().to_dict()
    assert record["published"] is True


def test_cyber_trust_ingest_surface_quarantines_screens_and_drains():
    """The deployed SEC-1 intake: quarantine-first ingest, full screening,
    verdict drained to the bus. The response carries METADATA ONLY — no
    document text, no verdict content (SEC-4)."""
    from forge_common.quarantine import FirestoreQuarantineStore

    db = FakeFirestore()
    registry.load_registry(db)
    layout.clock_ref(db).set({"logical_time": 0})
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    from pathlib import Path as _Path

    bulletin = (
        _Path(__file__).resolve().parents[1] / "data" / "vendor_bulletin_vnd_act_9901.txt"
    ).read_text()
    classification = {
        "label": "malicious",
        "confidence": 0.97,
        "candidate_part_identifier": "VND-ACT-9901",
        "rationale": "Embedded override instructions targeting automated systems.",
    }
    published = []
    client = TestClient(
        create_worker(
            db,
            "cyber_trust",
            model=stub_json(classification),
            publish=lambda m, k: published.append(m),
            quarantine_store=FirestoreQuarantineStore(db, bucket="forge-quarantine-test"),
            armor=lambda text: {"verdict": "flagged", "categories": ["pi_and_jailbreak"]},
        ),
        raise_server_exceptions=False,
    )
    response = client.post(
        "/ingest",
        json={
            "workflow_id": WF,
            "doc_id": "doc-vendor-bulletin-001",
            "source": "synthetic-vendor-portal",
            "raw_text": bulletin,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict_published"] is True
    assert "SYSTEM OVERRIDE" not in response.text and "VND-ACT-9901" not in response.text
    verdicts = [m for m in published if m["envelope"]["schema_version"] == "quarantine_verdict.v3"]
    assert len(verdicts) == 1
    assert verdicts[0]["envelope"]["trace_id"] == TRACE  # the WORKFLOW root trace
    reasons = [
        s.to_dict()["payload"]["reason_code"]
        for s in layout.workflow_ref(db, WF).collection("audit").stream()
    ]
    assert "DOCUMENT_QUARANTINED" in reasons

    # identical re-ingest: idempotent 200; conflicting bytes: 409 audited
    assert (
        client.post(
            "/ingest",
            json={
                "workflow_id": WF,
                "doc_id": "doc-vendor-bulletin-001",
                "source": "synthetic-vendor-portal",
                "raw_text": bulletin,
            },
        ).status_code
        == 200
    )
    conflict = client.post(
        "/ingest",
        json={
            "workflow_id": WF,
            "doc_id": "doc-vendor-bulletin-001",
            "source": "synthetic-vendor-portal",
            "raw_text": bulletin + " TAMPERED",
        },
    )
    assert conflict.status_code == 409
    assert client.post("/ingest", json={"workflow_id": WF}).status_code == 400


def test_reg1_fleet_probe_stamps_heartbeats_and_health_derives():
    """REG-1 verify criterion: staleness derivation including the
    scaled-to-zero UNKNOWN case. The Orchestrator's authenticated probe
    stamps last_heartbeat_at on success; health stays DERIVED — probed
    services read HEALTHY, a service that stops answering decays to STALE,
    a never-probed / endpointless definition stays UNKNOWN, and nothing
    ever writes a health flag."""
    from services.dashboard.app import create_app, derive_health
    from services.orchestrator.monitor import probe_fleet_health

    db = FakeFirestore()
    registry.load_registry(db)
    # give every definition an endpoint except cyber-trust (scaled-to-zero,
    # never invoked -> must stay UNKNOWN)
    for snapshot in db.collection("agent_registry").stream():
        d = snapshot.to_dict()
        endpoint = (
            None
            if d["agent_id"] == "forge-cyber-trust"
            else f"https://{d['agent_id']}.example.run.app"
        )
        db.collection("agent_registry").document(d["agent_id"]).set({**d, "endpoint": endpoint})

    down = {"forge-safety"}  # this service will not answer its probe
    probed_endpoints = []

    def prober(endpoint):
        probed_endpoints.append(endpoint)
        return not any(agent in endpoint for agent in down)

    result = probe_fleet_health(db, prober=prober)
    assert result["probed"] == 5  # six definitions, one endpointless
    assert result["stamped"] >= 4

    client = TestClient(create_app(db, verifier=lambda t: "x"))
    catalog = {d["agent_id"]: d for d in client.get("/api/catalog").json()}
    assert all(i["health"] == "HEALTHY" for i in catalog["forge-workforce"]["instances"]), (
        "probed definitions derive HEALTHY (both instances stamped)"
    )
    assert catalog["forge-cyber-trust"]["instances"][0]["health"] == "UNKNOWN", (
        "scaled-to-zero, never probed -> UNKNOWN, never fabricated HEALTHY"
    )
    assert catalog["forge-safety"]["instances"][0]["health"] == "UNKNOWN", (
        "failed probe stamps nothing: stays UNKNOWN until first success"
    )
    # decay: a stamp older than three deployed-cadence beats reads STALE
    assert (
        derive_health("2026-08-22T00:00:00.000000Z", now="2026-08-22T00:02:00.000000Z") == "HEALTHY"
    )
    assert (
        derive_health("2026-08-22T00:00:00.000000Z", now="2026-08-22T00:03:30.000000Z") == "STALE"
    )

    # probe failures never crash the tick
    def exploding(endpoint):
        raise RuntimeError("connection refused")

    assert probe_fleet_health(db, prober=exploding)["stamped"] == 0
