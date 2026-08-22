"""Day 7: OBS-1/OBS-2 — W3C trace context across a stubbed Pub/Sub hop (CI-3).

The traceparent attribute is AUTHORITATIVE; the envelope trace_id is a
read-only mirror; spans carry metadata only. These tests install a real
in-memory TracerProvider so span identity and attributes are asserted, not
inferred.
"""

import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from tests.fake_firestore import FakeFirestore

from forge_common import otel
from forge_common.bus import process_message
from forge_common.clock import build_due_event

WF = "wf-obs-0001"
TRACE = "abcdef0123456789abcdef0123456789"

_EXPORTER = InMemorySpanExporter()
_PROVIDER = TracerProvider()
_PROVIDER.add_span_processor(SimpleSpanProcessor(_EXPORTER))
trace.set_tracer_provider(_PROVIDER)


@pytest.fixture(autouse=True)
def clear_spans():
    _EXPORTER.clear()
    yield


def due_message(trace_id=TRACE, due_at=21):
    return build_due_event(workflow_id=WF, trace_id=trace_id, due_at=due_at)


def consume_spans():
    return [s for s in _EXPORTER.get_finished_spans() if s.name.startswith("forge.consume")]


def test_traceparent_injects_and_extracts_across_stubbed_hop():
    """CI-3/OBS-1: publish injects W3C context into transport attributes; the
    consumer extracts it; the OTel trace id equals the envelope mirror; a
    second hop continues the SAME trace with parent/child span linkage."""
    db = FakeFirestore()
    message = due_message()

    # hop 1: published outside any span — the mirror seeds the W3C context
    attributes = otel.inject_trace_attributes({}, message["envelope"])
    assert "traceparent" in attributes
    assert TRACE in attributes["traceparent"]
    assert process_message(
        db,
        message,
        lambda m, w: None,
        consumer_identity="forge-orchestrator",
        transport_attributes=attributes,
    )
    (hop1,) = consume_spans()
    assert format(hop1.context.trace_id, "032x") == TRACE
    assert hop1.attributes["forge.outcome"] == "processed"

    # hop 2: published INSIDE hop 1's context — traceparent carries hop 1's
    # span id; the next consume span parents under it, same single trace
    second = build_due_event(workflow_id=WF, trace_id=TRACE, due_at=22)
    with trace.get_tracer("test").start_as_current_span(
        "publisher-context", context=otel._mirror_context(TRACE)
    ) as publishing_span:
        hop2_attributes = otel.inject_trace_attributes({}, second["envelope"])
    assert format(publishing_span.context.span_id, "016x") in hop2_attributes["traceparent"]
    _EXPORTER.clear()
    assert process_message(
        db,
        second,
        lambda m, w: None,
        consumer_identity="forge-supply",
        transport_attributes=hop2_attributes,
    )
    (hop2,) = consume_spans()
    assert format(hop2.context.trace_id, "032x") == TRACE
    assert hop2.parent.span_id == publishing_span.context.span_id


def test_consume_without_traceparent_joins_mirror_trace():
    """A hop with no W3C context (first publish, replay) still joins the
    workflow trace via the envelope mirror — never a second trace id."""
    db = FakeFirestore()
    assert process_message(db, due_message(), lambda m, w: None, consumer_identity="forge-safety")
    (span,) = consume_spans()
    assert format(span.context.trace_id, "032x") == TRACE


def test_span_attributes_are_metadata_only():
    """OBS-2: no payload text in any span attribute — only routing metadata."""
    db = FakeFirestore()
    message = due_message()
    message["payload"]["purpose"] = "part_eta_reached"
    process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")
    (span,) = consume_spans()
    allowed = {
        "forge.consumer",
        "forge.workflow_id",
        "forge.work_package_id",
        "forge.event_id",
        "forge.schema_version",
        "forge.trace_id_mirror",
        "forge.trust_state",
        "forge.trace_mirror_mismatch",
        "forge.outcome",
    }
    assert set(span.attributes) <= allowed
    dumped = " ".join(str(v) for v in span.attributes.values())
    assert "part_eta_reached" not in dumped, "payload fields must never reach span attributes"


def test_mirror_mismatch_is_flagged_never_fatal():
    """W3C context is authoritative: a traceparent that disagrees with the
    envelope mirror wins, and the drift is surfaced as an attribute."""
    db = FakeFirestore()
    other = "ffff0123456789abcdefffff01234567"
    foreign = otel.inject_trace_attributes({}, {"trace_id": other})
    assert process_message(
        db,
        due_message(),
        lambda m, w: None,
        consumer_identity="forge-orchestrator",
        transport_attributes=foreign,
    )
    (span,) = consume_spans()
    assert format(span.context.trace_id, "032x") == other
    assert span.attributes["forge.trace_mirror_mismatch"] is True


def test_malformed_mirror_never_crashes_the_span_helper():
    """A garbage envelope trace_id is a CONTRACT problem (rejected + audited
    downstream), never a crash inside the tracing helper."""
    with otel.consume_span(
        "forge-orchestrator",
        {
            "workflow_id": WF,
            "event_id": "e-1",
            "schema_version": "due_event.v2",
            "trace_id": "Z" * 32,
        },
    ) as span:
        assert span is not None


def test_dedupe_outcome_is_visible_on_the_span():
    db = FakeFirestore()
    message = due_message()
    process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")
    _EXPORTER.clear()
    assert (
        process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")
        is False
    )
    (span,) = consume_spans()
    assert span.attributes["forge.outcome"] == "deduplicated"


def test_adk_model_spans_nest_under_the_consume_span():
    """OBS-1: ADK's own agent/model spans parent under the active bus span —
    the workflow is ONE trace including model calls (observed live before
    the fix: 7 orphan model-call traces beside the workflow trace)."""
    from tests.adk_stub import stub_json

    from forge_common.agent_base import AdkTextRunner

    runner = AdkTextRunner(name="obs_probe", model=stub_json({"ok": True}))
    with otel.consume_span(
        "forge-orchestrator",
        {
            "workflow_id": WF,
            "event_id": "e-obs-adk",
            "schema_version": "nmc_event.v2",
            "trace_id": TRACE,
        },
    ):
        runner("say ok")
    spans = _EXPORTER.get_finished_spans()
    adk_spans = [s for s in spans if not s.name.startswith("forge.consume")]
    assert adk_spans, "ADK emitted no spans under the test provider"
    for span in adk_spans:
        assert format(span.context.trace_id, "032x") == TRACE, span.name


def test_adk_spans_carry_no_prompt_or_response_content():
    """OBS-2/DAT-3 (verify-pass finding): google-adk defaults to capturing
    the FULL llm request and response into span attributes; this module
    forces that OFF. Assert no ADK span attribute contains the prompt or the
    model's reply."""
    from tests.adk_stub import stub_json

    from forge_common.agent_base import AdkTextRunner

    sentinel_prompt = "SENTINEL-PROMPT-PAYLOAD-TEXT do not export"
    runner = AdkTextRunner(name="obs_leak_probe", model=stub_json({"reply": "SENTINEL-REPLY"}))
    with otel.consume_span(
        "forge-orchestrator",
        {
            "workflow_id": WF,
            "event_id": "e-leak",
            "schema_version": "nmc_event.v2",
            "trace_id": TRACE,
        },
    ):
        runner(sentinel_prompt)
    for span in _EXPORTER.get_finished_spans():
        dumped = " ".join(f"{k}={v}" for k, v in (span.attributes or {}).items())
        assert "SENTINEL-PROMPT" not in dumped, span.name
        assert "SENTINEL-REPLY" not in dumped, span.name


def test_exception_through_consume_span_leaks_no_payload_in_status():
    """OBS-2 (verify-pass finding): the SDK's auto-status would write
    str(exc) — which can embed payload text — into the exported
    status.description. Only the exception TYPE may appear."""
    db = FakeFirestore()
    message = due_message()
    message["payload"]["purpose"] = "CONFIDENTIAL-PAYLOAD-FRAGMENT"
    del message["payload"]["due_at_logical"]  # force a ContractViolation
    import pytest as _pytest

    from forge_common.contracts import ContractViolation

    with _pytest.raises(ContractViolation):
        process_message(db, message, lambda m, w: None, consumer_identity="forge-orchestrator")
    (span,) = consume_spans()
    description = span.status.description or ""
    assert "CONFIDENTIAL-PAYLOAD-FRAGMENT" not in description
    assert description == "ContractViolation"


def test_malformed_traceparent_falls_back_to_mirror_and_flags():
    """A present-but-garbage traceparent must not sever the hop from the
    workflow trace (verify-pass finding): mirror fallback + explicit flag."""
    db = FakeFirestore()
    assert process_message(
        db,
        due_message(),
        lambda m, w: None,
        consumer_identity="forge-orchestrator",
        transport_attributes={"traceparent": "00-zzzz-bad-01"},
    )
    (span,) = consume_spans()
    assert format(span.context.trace_id, "032x") == TRACE
    assert span.attributes["forge.traceparent_malformed"] is True
