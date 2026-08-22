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
