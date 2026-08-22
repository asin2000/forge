"""FORGE dashboard + HUM-1 approval surface (HUM-1, HUM-2, REG-4, §10).

Read-only except the approve/reject controls (§10). Runs behind Cloud Run
IAM or IAP; the approver identity derives from the authenticated principal
(services.dashboard.auth), never from client input (HUM-1).

Surfaces:
- Workflows: state, work packages, and the audit trail reconstructed from
  Firestore alone (AUD-2 — this is the closing-shot render).
- Agent Catalog (REG-4): definitions, instances, and DERIVED health
  (heartbeat staleness; never a stored flag).
- Pending approvals with the full HUM-2 decision record (source refs,
  extracted facts, applicable rules, constraints, confidence, alternatives,
  recommended action, agent/model/schema versions — no chain-of-thought).
- POST decide: records the approval_decision.v2 via the single sanctioned
  writer (state.record_approval_decision) and enqueues the bus copy in the
  SAME transaction.
"""

from __future__ import annotations

import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse

from forge_common import layout, state
from forge_common.audit import now_iso
from forge_common.messages import build_envelope, deterministic_event_id
from services.dashboard import auth
from services.dashboard.ui import PAGE_HTML

MONITORING_CYCLE_SECONDS = 5
STALE_AFTER_SECONDS = 3 * MONITORING_CYCLE_SECONDS


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
    db: Any, *, verifier: Any = None, iap_verifier: Any = None, publish: Any = None
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
            "pending_approvals": _pending_approvals(workflow_id),
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

    return app


def production_app() -> FastAPI:  # pragma: no cover - Cloud Run entrypoint
    import os

    from google.cloud import firestore

    from forge_common import otel
    from forge_common.pubsub import OrderedPublisher

    project = os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("PROJECT_ID")
    topic = os.environ.get("FORGE_BUS_TOPIC", "forge-bus")
    otel.init_tracing("forge-dashboard")
    publisher = OrderedPublisher(project)
    return create_app(
        firestore.Client(project=project),
        publish=lambda message, key: publisher.publish(topic, message, key),
    )
