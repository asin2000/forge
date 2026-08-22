"""OpenTelemetry tracing for the bus (OBS-1) with metadata-only spans (OBS-2).

The W3C trace context is AUTHORITATIVE: ``traceparent``/``tracestate``
propagate across Pub/Sub hops as message attributes (injected at publish,
extracted at consume). The ICD-4 envelope ``trace_id`` is a READ-ONLY MIRROR
of the 32-hex OpenTelemetry trace ID — when a hop carries no W3C context
(the workflow's first publish, a replay, a drain after a crash) the span
parents under a synthetic remote context DERIVED from that mirror, so every
span of a workflow shares the one trace and no second application trace
identifier ever exists. A mismatch between an incoming ``traceparent`` and
the envelope mirror is surfaced as a span attribute, never an exception.

Spans carry ONLY metadata (OBS-2): envelope routing fields, consumer
identity, outcome. Never prompts, payloads, document text, or decision
prose. Exceptions set span status but are not recorded as events (their
stringified args could smuggle payload fragments).

Export (OBS-1): ``init_tracing`` installs the Cloud Trace exporter with
100% sampling when ``FORGE_TRACE_EXPORT=cloud`` (set by the deployment);
anywhere else the global no-op/test provider stays in charge, so unit tests
assert on in-memory spans and the emulator gate runs without credentials.
ADK's built-in agent/model spans use the same global provider and nest
under the active consume span; Cloud Run's automatic HTTP spans are not
duplicated — consume spans parent under whatever context the transport
delivers.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.propagate import extract, inject
from opentelemetry.trace import NonRecordingSpan, SpanContext, StatusCode, TraceFlags

_TRACER_NAME = "forge.bus"

# OBS-2/DAT-3: google-adk's telemetry captures the FULL llm request AND
# response into span attributes (gcp.vertex.agent.llm_request/llm_response)
# unless this is off — and its default is ON. Set at import time so no ADK
# span can ever carry prompt or decision prose, in any process that touches
# this module (workers, dashboard, tests, smokes).
os.environ["ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS"] = "false"


def init_tracing(service_name: str) -> None:
    """Install the Cloud Trace exporter (100% sampling) when the deployment
    asks for it. Idempotent; a no-op outside FORGE_TRACE_EXPORT=cloud."""
    if os.environ.get("FORGE_TRACE_EXPORT") != "cloud":  # pragma: no cover
        return
    from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    from opentelemetry.sdk.trace.sampling import ALWAYS_ON

    provider = TracerProvider(
        resource=Resource.create({"service.name": service_name}), sampler=ALWAYS_ON
    )
    provider.add_span_processor(BatchSpanProcessor(CloudTraceSpanExporter()))
    trace.set_tracer_provider(provider)


def _mirror_context(trace_id_hex: Any) -> Any:
    """A synthetic REMOTE parent carrying the envelope's mirrored trace ID —
    deterministic span id, sampled, so late/first hops join the one trace.
    A malformed mirror yields None (span starts a fresh root): the malformed
    MESSAGE is contract-rejected downstream; the span helper never raises."""
    try:
        trace_id = int(str(trace_id_hex), 16)
    except ValueError:
        return None
    if not 0 < trace_id < 2**128:
        return None
    span_context = SpanContext(
        trace_id=trace_id,
        span_id=int(str(trace_id_hex)[:16], 16) or 1,
        is_remote=True,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
    )
    return trace.set_span_in_context(NonRecordingSpan(span_context))


def inject_trace_attributes(attributes: dict[str, str], envelope: dict[str, Any]) -> dict[str, str]:
    """Inject W3C ``traceparent``/``tracestate`` into transport attributes.

    Publishing inside an active span continues that span's context;
    publishing outside any span (outbox drain) injects the envelope-mirror
    context, so downstream consumers still join the workflow trace.
    """
    current = trace.get_current_span().get_span_context()
    if current.is_valid:
        inject(attributes)
    else:
        mirror = _mirror_context(envelope.get("trace_id", ""))
        if mirror is not None:
            inject(attributes, context=mirror)
    return attributes


@contextmanager
def consume_span(
    consumer_identity: str,
    envelope: dict[str, Any],
    transport_attributes: dict[str, str] | None = None,
):
    """Span around one bus consumption. W3C context from the transport
    attributes is authoritative; the envelope mirror is the fallback parent.
    Attributes are OBS-2 metadata ONLY."""
    mirror_mismatch = False
    traceparent_malformed = False
    if transport_attributes and "traceparent" in transport_attributes:
        parent = extract(transport_attributes)
        extracted = trace.get_current_span(parent).get_span_context()
        if extracted.is_valid:
            # string comparison: never int-parse a possibly-malformed mirror
            mirror_mismatch = format(extracted.trace_id, "032x") != str(
                envelope.get("trace_id", "")
            )
        else:
            # a PRESENT-but-garbage traceparent must not sever the hop from
            # the workflow trace: fall back to the mirror AND flag it
            parent = _mirror_context(envelope.get("trace_id", ""))
            traceparent_malformed = True
    else:
        parent = _mirror_context(envelope.get("trace_id", ""))
    tracer = trace.get_tracer(_TRACER_NAME)
    # .get() throughout: a malformed envelope is a CONTRACT problem, audited
    # and rejected downstream — the span helper itself never raises.
    with tracer.start_as_current_span(
        f"forge.consume {envelope.get('schema_version', 'unknown')}",
        context=parent,
        # OBS-2: exception args can carry payload text — no exception events,
        # and no auto status either (the SDK would write str(exc) into
        # status.description, which exports). Status is set manually below
        # with the exception TYPE only.
        record_exception=False,
        set_status_on_exception=False,
        attributes={
            "forge.consumer": consumer_identity,
            "forge.workflow_id": str(envelope.get("workflow_id", "")),
            "forge.work_package_id": envelope.get("work_package_id", "wp-none"),
            "forge.event_id": str(envelope.get("event_id", "")),
            "forge.schema_version": str(envelope.get("schema_version", "")),
            "forge.trace_id_mirror": str(envelope.get("trace_id", "")),
            "forge.trust_state": envelope.get("trust_state", ""),
            **({"forge.trace_mirror_mismatch": True} if mirror_mismatch else {}),
            **({"forge.traceparent_malformed": True} if traceparent_malformed else {}),
        },
    ) as span:
        try:
            yield span
        except Exception as exc:
            span.set_status(StatusCode.ERROR, description=type(exc).__name__)
            raise
