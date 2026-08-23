"""FORGE dashboard + operator console (HUM-1, HUM-2, HUM-3, REG-4, §10).

Read-only except the approve/reject controls (HUM-1) and the governed
operator controls (HUM-3). Runs behind Cloud Run IAM or IAP; approver AND
operator identity derive from the authenticated principal
(services.dashboard.auth), never from client input.

Surfaces:
- Workflows: state, work packages, and the audit trail reconstructed from
  Firestore alone (AUD-2 — this is the closing-shot render).
- Agent Catalog (REG-4): definitions, instances, and DERIVED health
  (heartbeat staleness; never a stored flag).
- Live Agent Activity: newest-first fleet-wide feed of the SAME audit
  events AUD-2 already records — pure read-side display.
- Pending approvals with the full HUM-2 decision record (source refs,
  extracted facts, applicable rules, constraints, confidence, alternatives,
  recommended action, agent/model/schema versions — no chain-of-thought).
- POST decide: records the approval_decision.v2 via the single sanctioned
  writer (state.record_approval_decision) and enqueues the bus copy in the
  SAME transaction.
- HUM-3 operator controls, all audited with the authenticated principal and
  all routed through the sanctioned transactional writers: start (NMC event
  enqueued atomically with the workflow doc), cancel (terminal CANCELLED
  transition), clock advance, poisoned-bulletin injection (relayed to the
  deployed Cyber Trust quarantine path; responses stay metadata-only), and
  instance fail/restore (REG-5 registry transitions).
"""

from __future__ import annotations

import datetime
import os
import uuid
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from forge_common import layout, registry, state
from forge_common.audit import bounded_json_detail, build_audit_event, now_iso
from forge_common.clock import CLOCK_AUDIT_WORKFLOW, advance_clock, emit_due_events, read_clock
from forge_common.contracts import ContractViolation, validate_message
from forge_common.messages import build_envelope, deterministic_event_id, deterministic_trace_id
from services.dashboard import auth
from services.dashboard.ui import PAGE_HTML

OPERATOR_SURFACE_IDENTITY = "forge-approval-surface"

_BULLETIN_PATH = Path(__file__).resolve().parents[2] / "data" / "vendor_bulletin_vnd_act_9901.txt"

# REG-1: health derives from heartbeat staleness at the DEPLOYED probe
# cadence (Cloud Scheduler's per-minute tick); three missed beats = STALE.
# The 5s monitoring cycle governs ORC-4 timeout detection, not heartbeats.
HEARTBEAT_CADENCE_SECONDS = int(os.environ.get("HEARTBEAT_CADENCE_SECONDS", "60"))
STALE_AFTER_SECONDS = 3 * HEARTBEAT_CADENCE_SECONDS


def _docs(collection: Any) -> list[dict[str, Any]]:
    out = []
    for snapshot in collection.stream():
        doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if doc:
            out.append(doc)
    return out


def derive_health(last_heartbeat_at: str | None, *, now: str | None = None) -> str:
    """REG-1: health is DERIVED from heartbeat staleness, never stored."""
    if not last_heartbeat_at:
        return "UNKNOWN"
    current = datetime.datetime.fromisoformat((now or now_iso()).replace("Z", "+00:00"))
    beat = datetime.datetime.fromisoformat(last_heartbeat_at.replace("Z", "+00:00"))
    age = (current - beat).total_seconds()
    return "HEALTHY" if age <= STALE_AFTER_SECONDS else "STALE"


def create_app(
    db: Any,
    *,
    verifier: Any = None,
    iap_verifier: Any = None,
    publish: Any = None,
    ingest_forward: Any = None,
) -> FastAPI:
    app = FastAPI(title="FORGE dashboard", docs_url=None, redoc_url=None)
    verify: dict[str, Any] = {}
    if verifier is not None:
        verify["verifier"] = verifier
    if iap_verifier is not None:
        verify["iap_verifier"] = iap_verifier

    def _principal(request: Request) -> str:
        try:
            return auth.approver_from_request(request.headers, **verify)
        except PermissionError as exc:
            raise HTTPException(401, str(exc)) from exc

    @app.get("/api/whoami")
    def whoami(request: Request) -> dict[str, str]:
        """Read-only: the principal THIS surface derives for the caller —
        lets ops (and the live forged-header negative test) confirm identity
        never comes from client-suppliable fields."""
        return {"approver_identity": _principal(request)}

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return PAGE_HTML

    @app.get("/api/workflows")
    def workflows() -> list[dict[str, Any]]:
        return sorted(_docs(db.collection("workflows")), key=lambda w: w.get("workflow_id", ""))

    @app.get("/api/workflows/{workflow_id}")
    def workflow_detail(workflow_id: str) -> dict[str, Any]:
        snapshot = layout.workflow_ref(db, workflow_id).get()
        doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if not doc:
            raise HTTPException(404, "unknown workflow")
        packages = _docs(layout.workflow_ref(db, workflow_id).collection("work_packages"))
        trail = state.reconstruct_audit_trail(db, workflow_id)
        return {
            "state": doc,
            "work_packages": sorted(packages, key=lambda p: p["work_package_id"]),
            "audit_trail": [
                {
                    "effective_at": e["payload"]["effective_at"],
                    "observed_at": e["payload"]["observed_at"],
                    "event_kind": e["payload"]["event_kind"],
                    "reason_code": e["payload"]["reason_code"],
                    "agent_identity": e["payload"]["agent_identity"],
                    "state_after": e["payload"].get("state_after"),
                    "detail": e["payload"].get("detail"),
                    # DAT-2: both labels displayed in the rendered trail
                    "data_origin": e["envelope"].get("data_origin"),
                    "trust_state": e["envelope"].get("trust_state"),
                }
                for e in trail
            ],
            # a finished workflow renders no live gates (HUM-3): leftover
            # requests are history in the trail, not decidable cards
            "pending_approvals": (
                [] if doc["status"] in state.TERMINAL_STATES else _pending_approvals(workflow_id)
            ),
        }

    def _pending_requests(workflow_id: str) -> dict[str, dict[str, Any]]:
        """Full approval_request messages with no recorded decision, keyed by
        approval_id (HUM-2). The ENVELOPE matters too: the decision must ride
        the request's trace (OBS-1/ICD-4 — one trace per workflow, no second
        application trace identifier)."""
        pending: dict[str, dict[str, Any]] = {}
        for record in _docs(layout.workflow_ref(db, workflow_id).collection("outbox")):
            message = record.get("message", {})
            if message.get("envelope", {}).get("schema_version") != "approval_request.v2":
                continue
            approval_id = message["payload"]["approval_id"]
            snapshot = layout.approval_ref(db, workflow_id, approval_id).get()
            decided = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
            if not decided:
                pending[approval_id] = message
        return pending

    def _pending_approvals(workflow_id: str) -> list[dict[str, Any]]:
        return [m["payload"] for m in _pending_requests(workflow_id).values()]

    @app.get("/api/catalog")
    def catalog() -> list[dict[str, Any]]:
        """REG-4: read-only Agent Catalog."""
        instances = _docs(db.collection("agent_instances"))
        by_definition: dict[str, list[dict[str, Any]]] = {}
        for inst in instances:
            by_definition.setdefault(inst["definition_id"], []).append(
                {
                    "instance_id": inst["instance_id"],
                    "state": inst["state"],
                    "health": derive_health(inst.get("last_heartbeat_at")),
                }
            )
        out = []
        for definition in _docs(db.collection("agent_registry")):
            out.append(
                {
                    "agent_id": definition["agent_id"],
                    "version": definition["version"],
                    "department_owner": definition["department_owner"],
                    "capabilities": definition["capabilities"],
                    "lifecycle_status": definition["lifecycle_status"],
                    "contracts": definition["contracts"],
                    "instances": sorted(
                        by_definition.get(definition["agent_id"], []),
                        key=lambda i: i["instance_id"],
                    ),
                }
            )
        return sorted(out, key=lambda d: d["agent_id"])

    @app.post("/api/workflows/{workflow_id}/decide")
    def decide(workflow_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        approver = _principal(request)
        if "approver_identity" in body:
            # HUM-1: identity comes from the authenticated principal ONLY.
            raise HTTPException(400, "approver_identity is not client-suppliable")
        approval_id = body.get("approval_id")
        pending = _pending_requests(workflow_id)
        if approval_id not in pending:
            raise HTTPException(404, "no such pending approval request")
        wf_snapshot = layout.workflow_ref(db, workflow_id).get()
        wf_doc = wf_snapshot.to_dict() if hasattr(wf_snapshot, "to_dict") else wf_snapshot
        if (wf_doc or {}).get("status") in state.TERMINAL_STATES:
            # HUM-3: a finished workflow accepts no further decisions — the
            # leftover request is inert history, not a live gate.
            raise HTTPException(409, f"workflow is already {wf_doc['status']}")
        decision = body.get("decision")
        if decision not in ("approved", "rejected"):
            raise HTTPException(400, "decision must be approved or rejected")
        request_message = pending[approval_id]
        request_payload = request_message["payload"]
        event_id = deterministic_event_id("decision", workflow_id, approval_id, decision, approver)
        message = {
            "envelope": build_envelope(
                workflow_id=workflow_id,
                schema_version="approval_decision.v2",
                event_id=event_id,
                # OBS-1/ICD-4: the decision CONTINUES the workflow trace the
                # request rode in on — never a new application trace id.
                trace_id=request_message["envelope"]["trace_id"],
                idempotency_key=f"idem-{approval_id}-{decision}",
            ),
            "payload": {
                "approval_id": approval_id,
                "action_type": request_payload["action_type"],
                "decision": decision,
                "approver_identity": approver,
                "decided_at": now_iso().split(".")[0] + "Z",
                **({"comment": body["comment"][:1000]} if body.get("comment") else {}),
            },
        }
        try:
            state.record_approval_decision(db, message, enqueue_outbox=True)
        except state.GateBlocked as exc:
            raise HTTPException(409, str(exc)) from exc
        if publish is not None:
            # best-effort post-commit drain: a failure here leaves the copy
            # unpublished in the outbox, and the next drain republishes it
            from forge_common.bus import drain_outbox

            drain_outbox(db, workflow_id, publish)
        return {"approval_id": approval_id, "decision": decision, "approver": approver}

    def _drain(workflow_id: str) -> None:
        if publish is not None:
            # same best-effort post-commit pattern as decide(); the /tick
            # sweep republishes anything a failed drain leaves behind
            from forge_common.bus import drain_outbox

            drain_outbox(db, workflow_id, publish)

    def _operator_guard(request: Request, body: dict[str, Any]) -> str:
        """HUM-3 mirrors HUM-1: principal first, never client-suppliable."""
        operator = _principal(request)
        for field in ("operator_identity", "approver_identity"):
            if field in body:
                raise HTTPException(400, f"{field} is not client-suppliable")
        return operator

    @app.get("/api/clock")
    def clock() -> dict[str, int]:
        return {"logical_time": read_clock(db)}

    @app.get("/api/activity")
    def activity(limit: int = 30) -> list[dict[str, Any]]:
        """Live Agent Activity: newest-first fleet-wide audit feed — a pure
        projection of the AUD-2 trail (workflows plus the system audit
        workflows), no new data recorded anywhere."""
        limit = max(1, min(int(limit), 100))
        ids = [w["workflow_id"] for w in _docs(db.collection("workflows"))]
        ids += [CLOCK_AUDIT_WORKFLOW, registry.REGISTRY_AUDIT_WORKFLOW]
        events: list[dict[str, Any]] = []
        for workflow_id in ids:
            for e in state.reconstruct_audit_trail(db, workflow_id)[-limit:]:
                events.append(
                    {
                        "workflow_id": workflow_id,
                        "observed_at": e["payload"]["observed_at"],
                        "effective_at": e["payload"]["effective_at"],
                        "event_kind": e["payload"]["event_kind"],
                        "reason_code": e["payload"]["reason_code"],
                        "agent_identity": e["payload"]["agent_identity"],
                        "state_after": e["payload"].get("state_after"),
                        # DAT-2: labels ride the feed exactly as in the trail
                        "data_origin": e["envelope"].get("data_origin"),
                        "trust_state": e["envelope"].get("trust_state"),
                    }
                )
        events.sort(key=lambda e: (e["observed_at"], e["workflow_id"]), reverse=True)
        return events[:limit]

    @app.post("/api/workflows")
    def start_workflow(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """HUM-3 start: inject the nmc_event.v2 the field would emit. The
        state doc, its WORKFLOW_CREATED audit (operator recorded), and the
        triggering event's outbox copy commit in ONE transaction (AUD-2)."""
        operator = _operator_guard(request, body)
        workflow_id = f"wf-op-{uuid.uuid4().hex[:10]}"
        trace_id = deterministic_trace_id(workflow_id)
        nmc = {
            "envelope": build_envelope(
                workflow_id=workflow_id,
                schema_version="nmc_event.v2",
                event_id=deterministic_event_id("operator-nmc", workflow_id),
                trace_id=trace_id,
                idempotency_key=f"idem-op-nmc-{workflow_id.removeprefix('wf-')}",
            ),
            "payload": {
                "equipment_id": body.get("equipment_id"),
                "discrepancy_code": body.get("discrepancy_code"),
                "description": body.get("description"),
                "reported_at": now_iso().split(".")[0] + "Z",
            },
        }
        try:
            validate_message(nmc)  # ICD-2 gates the operator's input shape
        except ContractViolation as exc:
            raise HTTPException(400, str(exc)) from exc
        state.create_workflow(
            db,
            workflow_id=workflow_id,
            equipment_id=nmc["payload"]["equipment_id"],
            trace_id=trace_id,
            logical_time=read_clock(db),
            agent_identity=OPERATOR_SURFACE_IDENTITY,
            detail=bounded_json_detail({"operator": operator, "action": "workflow_start"}),
            outbox_messages=[nmc],
        )
        _drain(workflow_id)
        return {"workflow_id": workflow_id, "status": "INTAKE", "operator": operator}

    @app.post("/api/workflows/{workflow_id}/cancel")
    def cancel_workflow(
        workflow_id: str, request: Request, body: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """HUM-3 cancel: the audited terminal transition. In-flight bus
        traffic for the workflow then drains as audited stale no-ops."""
        operator = _operator_guard(request, body or {})
        snapshot = layout.workflow_ref(db, workflow_id).get()
        doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if not doc:
            raise HTTPException(404, "unknown workflow")
        if doc["status"] in state.TERMINAL_STATES:
            raise HTTPException(409, f"workflow is already {doc['status']}")
        try:
            state.transition_workflow(
                db,
                workflow_id=workflow_id,
                target="CANCELLED",
                agent_identity=OPERATOR_SURFACE_IDENTITY,
                trace_id=doc["trace_id"],
                reason_code="WORKFLOW_CANCELLED",
                detail=bounded_json_detail({"operator": operator, "action": "workflow_cancel"}),
            )
        except state.InvalidTransition as exc:
            # raced a concurrent transition; the client re-reads and retries
            raise HTTPException(409, str(exc)) from exc
        return {"workflow_id": workflow_id, "status": "CANCELLED", "operator": operator}

    @app.post("/api/clock/advance")
    def clock_advance(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """HUM-3: advance the Logical Clock (audited with the operator),
        then emit newly-due events and drain their workflows."""
        operator = _operator_guard(request, body)
        days = body.get("days")
        if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
            raise HTTPException(400, "days must be an integer between 1 and 365")
        logical_time = advance_clock(
            db,
            days,
            agent_identity=OPERATOR_SURFACE_IDENTITY,
            detail=bounded_json_detail({"operator": operator}),
        )
        due = emit_due_events(db)
        for due_workflow in due:
            _drain(due_workflow)
        return {"logical_time": logical_time, "due_events": len(due), "operator": operator}

    @app.post("/api/anomalies/bulletin")
    def inject_bulletin(request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """HUM-3 anomaly: relay the poisoned vendor bulletin to the DEPLOYED
        Cyber Trust /ingest (quarantine-first, SEC-1..4). The response stays
        metadata-only — raw text never transits back through this surface."""
        operator = _operator_guard(request, body)
        workflow_id = body.get("workflow_id")
        snapshot = layout.workflow_ref(db, str(workflow_id)).get() if workflow_id else None
        doc = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if not doc:
            raise HTTPException(404, "unknown workflow")
        if doc["status"] in state.TERMINAL_STATES:
            raise HTTPException(409, f"workflow is already {doc['status']}")
        if ingest_forward is None:
            raise HTTPException(503, "cyber-trust forwarding is not configured")
        doc_id = f"doc-bulletin-{uuid.uuid4().hex[:8]}"
        # The operator-attribution audit is recorded BEFORE the relay, with a
        # deterministic id: whatever fails downstream, the trail names the
        # principal behind the injection (HUM-3). The quarantine/screening
        # OUTCOMES are audited by cyber-trust itself on its own path.
        audit = build_audit_event(
            workflow_id=workflow_id,
            trace_id=doc["trace_id"],
            agent_identity=OPERATOR_SURFACE_IDENTITY,
            event_kind="escalation",
            reason_code="OPERATOR_BULLETIN_INJECTED",
            input_obj={"doc_id": doc_id, "source": "operator-console"},
            output_obj={"relayed_to": "forge-cyber-trust"},
            effective_at=read_clock(db),
            detail=bounded_json_detail({"operator": operator}),
            event_id=deterministic_event_id("operator-bulletin", workflow_id, doc_id),
        )

        def _record(txn: Any) -> None:
            # leading read: real transactions begin lazily on the first read
            layout.txn_get_dict(txn, layout.workflow_ref(db, workflow_id))
            txn.create(layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]), audit)

        layout.run_in_transaction(db, _record)
        try:
            result = ingest_forward(
                {
                    "workflow_id": workflow_id,
                    "doc_id": doc_id,
                    "source": "operator-console",
                    "raw_text": _BULLETIN_PATH.read_text(),
                }
            )
        except Exception as exc:
            # metadata-only on purpose: never echo upstream response bodies
            raise HTTPException(502, f"cyber-trust ingest failed: {type(exc).__name__}") from exc
        return {
            "workflow_id": workflow_id,
            "doc_id": doc_id,
            "verdict_published": bool(result.get("verdict_published")),
        }

    @app.post("/api/catalog/instances/{instance_id}")
    def instance_action(instance_id: str, request: Request, body: dict[str, Any]) -> dict[str, Any]:
        """HUM-3 anomaly: audited instance fail/restore (REG-5 transition
        under the registry system workflow)."""
        operator = _operator_guard(request, body)
        action = body.get("action")
        if action not in ("fail", "restore"):
            raise HTTPException(400, "action must be fail or restore")
        snapshot = registry.instance_ref(db, instance_id).get()
        if not (snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot):
            raise HTTPException(404, "unknown instance")
        try:
            result = registry.operator_set_instance_state(
                db,
                instance_id=instance_id,
                action=action,
                operator=operator,
                target_state=body.get("target_state"),
            )
        except registry.InvalidInstanceTransition as exc:
            raise HTTPException(409, str(exc)) from exc
        return {**result, "operator": operator}

    return app


def production_app() -> FastAPI:  # pragma: no cover - Cloud Run entrypoint
    import json
    import os
    import urllib.request

    from google.cloud import firestore

    from forge_common import otel
    from forge_common.pubsub import OrderedPublisher

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    topic = os.environ.get("FORGE_BUS_TOPIC", "forge-bus")
    otel.init_tracing("forge-dashboard")
    publisher = OrderedPublisher(project)

    ingest_forward = None
    cyber_trust_url = os.environ.get("FORGE_CYBER_TRUST_URL")
    if cyber_trust_url:

        def ingest_forward(payload: dict[str, Any]) -> dict[str, Any]:
            # OIDC service-to-service call, same mechanism as the REG-1
            # probe: the dashboard SA authenticates to Cloud Run IAM with
            # the cyber-trust service URL as audience.
            import google.auth.transport.requests
            import google.oauth2.id_token

            token = google.oauth2.id_token.fetch_id_token(
                google.auth.transport.requests.Request(), cyber_trust_url
            )
            request = urllib.request.Request(
                f"{cyber_trust_url}/ingest",
                data=json.dumps(payload).encode("utf-8"),
                method="POST",
            )
            request.add_header("Authorization", f"Bearer {token}")
            request.add_header("content-type", "application/json")
            with urllib.request.urlopen(request, timeout=120) as response:
                return json.loads(response.read())

    return create_app(
        firestore.Client(project=project),
        publish=lambda message, key: publisher.publish(topic, message, key),
        ingest_forward=ingest_forward,
    )
