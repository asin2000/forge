"""Agent worker runtime: one Cloud Run service per role (PLT-3, AGT-6).

Pub/Sub PUSH delivery: each role has a filtered push subscription whose OIDC
identity is the role's own service account, and every worker deploys with
``--no-allow-unauthenticated`` — the platform refuses anything that is not
the subscription's push (HUM-1-style platform gate; the worker itself holds
no auth logic).

Delivery semantics (ICD-5/ICD-6):
- success -> 204 (ack); the workflow outbox is drained to the bus afterwards
  (crash between commit and drain republishes on the NEXT drain — consumers
  dedupe via the inbox);
- another instance holds the claim (DeliveryInProgress) -> 429 (retry);
- contract violation / trust-gate rejection -> 200: the message was
  REJECTED AND AUDITED (ICD-2/DAT-2) — terminally, so retrying it into the
  DLQ would only duplicate rejection audits;
- anything else -> 500 (Pub/Sub retries; DLQ after 5 attempts per ICD-5).

W3C trace context (OBS-1) arrives in the push message attributes and flows
into process_message; ADK spans nest under the consume span. Cloud Run's
automatic HTTP spans are not duplicated.
"""

from __future__ import annotations

import base64
import json
from typing import Any

from fastapi import FastAPI, Response

from forge_common import otel
from forge_common.bus import (
    DeliveryInProgress,
    UntrustedMessageRejected,
    drain_outbox,
    process_message,
)
from forge_common.contracts import ContractViolation

DEFAULT_MODEL = "gemini-3.5-flash"


def build_handlers(db: Any, role: str, model: Any) -> dict[str, Any]:
    """schema_version -> handler for one role. Mirrors agents/registry.yaml
    contract declarations; subscription filters keep other schemas away."""
    if role == "orchestrator":
        from services.orchestrator.handlers import (
            make_decision_handler,
            make_due_handler,
            make_failure_handler,
            make_nmc_handler,
            make_plan_handler,
            make_sourcing_report_handler,
            make_verdict_handler,
        )

        return {
            "nmc_event.v2": make_nmc_handler(db, model=model),
            "maintenance_action_plan.v2": make_plan_handler(db),
            "sourcing_report.v3": make_sourcing_report_handler(db),
            "validation_verdict.v2": make_verdict_handler(db),
            "approval_decision.v2": make_decision_handler(db),
            "due_event.v2": make_due_handler(db),
            "agent_failure_event.v2": make_failure_handler(db),
        }
    if role == "safety":
        from services.safety.handlers import make_validation_handler

        handler = make_validation_handler(db, model)
        return {
            "maintenance_action_plan.v2": handler,
            "sourcing_report.v3": handler,
            "roster_assignment.v2": handler,
            "quarantine_verdict.v3": handler,
        }
    if role in ("maintenance", "supply", "workforce"):
        module = __import__(f"services.{role}.handlers", fromlist=["make_handler"])
        return {"work_package_assignment.v2": module.make_handler(db, model)}
    if role == "cyber_trust":
        return {}  # SEC-4: no bus consumption — ingestion is storage-driven
    raise ValueError(f"unknown worker role {role!r}")


def create_worker(
    db: Any,
    role: str,
    *,
    model: Any = DEFAULT_MODEL,
    publish: Any = None,
    quarantine_store: Any = None,
    armor: Any = None,
) -> FastAPI:
    """The push-delivery surface for one role. ``publish(message, key)`` is
    the bus transport for draining (injected in tests). The cyber_trust role
    additionally serves POST /ingest (SEC-1 storage-driven intake) when a
    quarantine store and screen are provided."""
    handlers = build_handlers(db, role, model)
    consumer_identity = f"forge-{role.replace('_', '-')}"
    app = FastAPI(title=f"FORGE worker {role}", docs_url=None, redoc_url=None)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"role": role, "status": "serving"}

    if role == "cyber_trust" and quarantine_store is not None:

        @app.post("/ingest")
        def ingest(body: dict[str, Any]) -> dict[str, Any]:
            """SEC-1 intake, behind the platform gate (--no-allow-
            unauthenticated): quarantine FIRST (GCS object + metadata +
            audit in one committed step), then the full screening pipeline
            (bounded parse -> Model Armor -> tool-less classifier), then the
            non-executable quarantine_verdict.v3 drains to the bus for
            Safety. The response carries METADATA ONLY — never document
            text, never verdict content (SEC-4)."""
            from fastapi import HTTPException

            from forge_common.messages import deterministic_trace_id
            from forge_common.quarantine import QuarantineConflict
            from services.cyber_trust.handlers import (
                ScreeningError,
                ingest_document,
                screen_document,
            )

            for field in ("workflow_id", "doc_id", "source", "raw_text"):
                if not body.get(field):
                    raise HTTPException(400, f"missing {field}")
            workflow_id = body["workflow_id"]
            wf_snapshot = db.collection("workflows").document(workflow_id).get()
            wf_doc = (
                wf_snapshot.to_dict() if hasattr(wf_snapshot, "to_dict") else wf_snapshot
            ) or {}
            trace_id = wf_doc.get("trace_id") or deterministic_trace_id(workflow_id)
            try:
                ingest_document(
                    db,
                    quarantine_store,
                    workflow_id=workflow_id,
                    doc_id=body["doc_id"],
                    raw_text=body["raw_text"],
                    source=body["source"][:200],
                    trace_id=trace_id,
                )
            except QuarantineConflict as exc:
                raise HTTPException(
                    409, "document conflicts with an existing quarantined record (audited)"
                ) from exc
            try:
                verdict = screen_document(
                    db,
                    quarantine_store,
                    body["doc_id"],
                    armor=armor,
                    classifier_model=model,
                    classifier_model_id=str(model) if isinstance(model, str) else "stub-model",
                    trace_id=trace_id,
                )
            except ScreeningError as exc:
                raise HTTPException(500, type(exc).__name__) from exc
            if publish is not None:
                drain_outbox(db, workflow_id, publish)
            return {
                "doc_id": body["doc_id"],
                "workflow_id": workflow_id,
                "verdict_published": verdict is not None,
            }

    if role == "orchestrator":

        @app.post("/tick")
        def tick() -> dict[str, Any]:
            """The unattended heartbeat (Cloud Scheduler, OIDC = this SA):
            ORC-4 monitoring cycle, ORC-5 due-event emission, and an outbox
            SWEEP so every enqueued record eventually publishes — including
            decisions recorded by the dashboard for workflows that receive
            no further bus traffic (verify-pass finding: without a sweeper,
            a failed post-commit drain parked the workflow forever)."""
            from forge_common.clock import emit_due_events
            from services.orchestrator.monitor import (
                probe_fleet_health,
                run_monitoring_cycle,
            )

            flagged = run_monitoring_cycle(db)
            due = emit_due_events(db)
            swept = 0
            if publish is not None:
                for snapshot in db.collection("workflows").stream():
                    doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
                    swept += drain_outbox(db, doc["workflow_id"], publish)
            health = probe_fleet_health(db)  # REG-1: authenticated fleet probe
            return {
                "timed_out": flagged,
                "due_emitted": due,
                "outbox_published": swept,
                "health": health,
            }

    @app.post("/")
    def push(body: dict[str, Any]) -> Response:
        # SYNC endpoint deliberately: FastAPI runs it in a threadpool, so
        # AdkTextRunner's in-thread asyncio.run has no running loop to
        # collide with (live finding: async-def here crashed every
        # model-bearing handler with "asyncio.run() cannot be called from a
        # running event loop" and the spine stalled at INTAKE). OTel context
        # propagates into the threadpool, so span nesting is unaffected.
        pubsub_message = body.get("message", {})
        attributes = dict(pubsub_message.get("attributes") or {})
        try:
            message = json.loads(base64.b64decode(pubsub_message.get("data", "")))
        except (ValueError, json.JSONDecodeError):
            # undecodable transport payload: nothing to audit against — DLQ it
            return Response(status_code=500)
        schema = message.get("envelope", {}).get("schema_version")
        handler = handlers.get(schema)
        if handler is None:
            return Response(status_code=204)  # filtered subscriptions make this rare
        try:
            process_message(
                db,
                message,
                handler,
                consumer_identity=consumer_identity,
                transport_attributes=attributes,
            )
        except DeliveryInProgress:
            return Response(status_code=429)
        except UntrustedMessageRejected:
            return Response(status_code=200)  # rejected AND audited — terminal
        except ContractViolation as exc:
            if getattr(exc, "audited_input_rejection", False):
                return Response(status_code=200)  # input boundary: audited, terminal
            # OUTPUT-side violation (handler produced a nonconforming
            # message): acking would be SILENT loss — 500 to the DLQ where
            # it is visible and replayable
            return Response(status_code=500)
        if publish is not None:
            workflow_id = message["envelope"]["workflow_id"]
            drain_outbox(db, workflow_id, publish)
        return Response(status_code=204)

    return app


def production_app() -> FastAPI:  # pragma: no cover - Cloud Run entrypoint
    import os

    from google.cloud import firestore

    from forge_common.pubsub import OrderedPublisher

    role = os.environ["WORKER_ROLE"]
    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    topic = os.environ.get("FORGE_BUS_TOPIC", "forge-bus")
    otel.init_tracing(f"forge-{role.replace('_', '-')}")
    db = firestore.Client(project=project)
    publisher = OrderedPublisher(project)
    quarantine_store = None
    armor = None
    if role == "cyber_trust":
        from forge_common.quarantine import GcsQuarantineStore
        from services.cyber_trust.model_armor import ModelArmorScreen

        quarantine_store = GcsQuarantineStore(db, bucket=f"forge-quarantine-{project}")
        armor = ModelArmorScreen(project_id=project)
    return create_worker(
        db,
        role,
        model=os.environ.get("FORGE_MODEL", DEFAULT_MODEL),
        publish=lambda message, key: publisher.publish(topic, message, key),
        quarantine_store=quarantine_store,
        armor=armor,
    )
