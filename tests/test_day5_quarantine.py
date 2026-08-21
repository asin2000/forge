"""Day 5 unit gate (CI-3): quarantine-first screening pipeline
(SEC-1..SEC-4, AGT-5, PLT-4-adapter, SEC-3 tool-less classifier)."""

import json
from pathlib import Path

import pytest
from services.cyber_trust.handlers import (
    ScreeningError,
    bounded_parse,
    ingest_document,
    screen_document,
)
from services.safety.handlers import make_validation_handler

from adk_stub import stub_garbage, stub_json
from fake_firestore import FakeFirestore
from forge_common import state
from forge_common.bus import process_message
from forge_common.contracts import validate_message
from forge_common.messages import deterministic_trace_id
from forge_common.quarantine import FirestoreQuarantineStore

WF = "wf-gx12-07-hyd-001"
TRACE = deterministic_trace_id(WF)
DOC = "vsb-2026-081"
BULLETIN = (
    Path(__file__)
    .resolve()
    .parents[1]
    .joinpath("data", "vendor_bulletin_vnd_act_9901.txt")
    .read_text()
)
INJECTION_MARKER = "SYSTEM OVERRIDE"  # distinctive substring of the raw text

MALICIOUS_CLASSIFICATION = {
    "label": "malicious",
    "confidence": 0.97,
    "candidate_part_identifier": "VND-ACT-9901",
    "rationale": "Embedded override instructions targeting automated systems.",
}


def flagged_armor(text):
    return {"verdict": "flagged", "categories": ["pi_and_jailbreak"]}


def broken_armor(text):
    raise ConnectionError("armor endpoint unavailable")


def ready():
    db = FakeFirestore()
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    store = FirestoreQuarantineStore(db, bucket="forge-quarantine-demo")
    return db, store


def everything_outside_quarantine(db):
    """Serialized content of every stored document except quarantine/*."""
    return json.dumps(
        {"/".join(k): v for k, v in db.store.items() if k[0] != "quarantine"},
        default=str,
    )


def screen(db, store, *, armor=flagged_armor, classifier=None):
    return screen_document(
        db,
        store,
        DOC,
        armor=armor,
        classifier_model=classifier or stub_json(MALICIOUS_CLASSIFICATION),
        classifier_model_id="gemini-3.5-flash",
        trace_id=TRACE,
    )


def test_ingest_quarantines_first_and_raw_stays_put():
    db, store = ready()
    record = ingest_document(
        db,
        store,
        workflow_id=WF,
        doc_id=DOC,
        raw_text=BULLETIN,
        source="vendor-email",
        trace_id=TRACE,
    )
    assert record["status"] == "QUARANTINED"
    assert record["quarantine_uri"].startswith("gs://forge-quarantine-demo/quarantine/")
    stored = store.get(DOC)
    assert INJECTION_MARKER in stored["raw_text"]
    trail = state.reconstruct_audit_trail(db, WF)
    assert trail[-1]["payload"]["reason_code"] == "DOCUMENT_QUARANTINED"
    assert INJECTION_MARKER not in everything_outside_quarantine(db)


def test_bounded_parser_extracts_identifiers_and_enforces_bounds():
    parsed = bounded_parse(BULLETIN)
    assert "VND-ACT-9901" in parsed["identifiers"]
    assert "HYD-ACT-4402" in parsed["identifiers"]
    with pytest.raises(ScreeningError, match="bound"):
        bounded_parse("A" * 20_001)


def test_screening_publishes_verdict_only_with_safe_metadata():
    db, store = ready()
    ingest_document(
        db,
        store,
        workflow_id=WF,
        doc_id=DOC,
        raw_text=BULLETIN,
        source="vendor-email",
        trace_id=TRACE,
    )
    verdict = screen(db, store)
    assert verdict is not None
    validate_message(verdict)
    payload = verdict["payload"]
    assert payload["released"] is True
    assert payload["screening"]["model_armor"]["verdict"] == "flagged"
    assert payload["screening"]["classifier"]["label"] == "malicious"
    assert payload["safe_metadata"]["candidate_part_identifier"] == "VND-ACT-9901"
    assert store.get(DOC)["status"] == "SCREENED"
    # SEC-4: the raw document never leaves quarantine — not in the verdict,
    # not in the outbox, not in the audit trail.
    assert INJECTION_MARKER not in json.dumps(verdict)
    assert INJECTION_MARKER not in everything_outside_quarantine(db)
    trail = state.reconstruct_audit_trail(db, WF)
    assert trail[-1]["payload"]["reason_code"] == "DOCUMENT_SCREENED"
    # Idempotent re-screen: nothing new.
    assert screen(db, store) is None
    outbox = [
        d.to_dict()["message"]
        for d in db.collection("workflows").document(WF).collection("outbox").stream()
    ]
    assert len(outbox) == 1


def test_scene1_safety_rejects_the_substitute_identifier():
    db, store = ready()
    ingest_document(
        db,
        store,
        workflow_id=WF,
        doc_id=DOC,
        raw_text=BULLETIN,
        source="vendor-email",
        trace_id=TRACE,
    )
    verdict = screen(db, store)
    vetoing = stub_json(
        {
            "subject_event_id": verdict["envelope"]["event_id"],
            "verdict": "vetoed",
            "rule_refs": ["SP-PART-001", "SP-SEC-004"],
            "reasons": [
                "VND-ACT-9901 is not in the approved-parts registry.",
                "Document was flagged by screening.",
            ],
        }
    )
    assert process_message(
        db, verdict, make_validation_handler(db, vetoing), consumer_identity="forge-safety"
    )
    safety_verdicts = [
        d.to_dict()["message"]
        for d in db.collection("workflows").document(WF).collection("outbox").stream()
        if d.to_dict()["message"]["envelope"]["schema_version"] == "validation_verdict.v2"
    ]
    assert len(safety_verdicts) == 1
    assert safety_verdicts[0]["payload"]["verdict"] == "vetoed"
    assert "SP-PART-001" in safety_verdicts[0]["payload"]["rule_refs"]
    trail = state.reconstruct_audit_trail(db, WF)
    assert trail[-1]["payload"]["event_kind"] == "veto"
    # And an approval attempt contradicting the engine is refused.
    db2, store2 = ready()
    ingest_document(
        db2,
        store2,
        workflow_id=WF,
        doc_id=DOC,
        raw_text=BULLETIN,
        source="vendor-email",
        trace_id=TRACE,
    )
    verdict2 = screen(db2, store2)
    approving = stub_json(
        {
            "subject_event_id": verdict2["envelope"]["event_id"],
            "verdict": "approved",
            "rule_refs": ["SP-PART-001"],
            "reasons": ["Vendor asserts equivalence."],
        }
    )
    process_message(
        db2, verdict2, make_validation_handler(db2, approving), consumer_identity="forge-safety"
    )
    out = [
        d.to_dict()["message"]
        for d in db2.collection("workflows").document(WF).collection("outbox").stream()
        if d.to_dict()["message"]["envelope"]["schema_version"] == "agent_failure_event.v2"
    ]
    assert len(out) == 1


@pytest.mark.parametrize("failure", ["armor", "classifier", "oversize"])
def test_fail_closed_on_any_screening_error(failure):
    db, store = ready()
    raw = "A" * 20_001 if failure == "oversize" else BULLETIN
    ingest_document(
        db,
        store,
        workflow_id=WF,
        doc_id=DOC,
        raw_text=raw,
        source="vendor-email",
        trace_id=TRACE,
    )
    result = screen(
        db,
        store,
        armor=broken_armor if failure == "armor" else flagged_armor,
        classifier=stub_garbage() if failure == "classifier" else None,
    )
    assert result is None
    doc = store.get(DOC)
    assert doc["status"] == "QUARANTINED"
    assert doc["last_error"]
    outbox = list(db.collection("workflows").document(WF).collection("outbox").stream())
    assert outbox == []
    trail = state.reconstruct_audit_trail(db, WF)
    assert trail[-1]["payload"]["reason_code"] == "SCREENING_FAILED"
    assert INJECTION_MARKER not in everything_outside_quarantine(db)


def test_classifier_cannot_invent_identifier():
    """SEC-4: the candidate is constrained to parser-extracted identifiers;
    a model inventing one exhausts retries and the pipeline fails closed."""
    db, store = ready()
    ingest_document(
        db,
        store,
        workflow_id=WF,
        doc_id=DOC,
        raw_text=BULLETIN,
        source="vendor-email",
        trace_id=TRACE,
    )
    inventing = stub_json(
        {**MALICIOUS_CLASSIFICATION, "candidate_part_identifier": "FAKE-PART-9999"}
    )
    assert screen(db, store, classifier=inventing) is None
    assert store.get(DOC)["status"] == "QUARANTINED"


def test_classifier_is_tool_less():
    """SEC-3 structural check: the classifier's ADK agent binds no tools."""
    from forge_common.agent_base import AdkTextRunner

    runner = AdkTextRunner(name="forge_cyber_trust_classifier", model=stub_json({}))
    assert list(runner._agent.tools) == []
