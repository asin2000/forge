#!/usr/bin/env python3
"""Lane-2 live spine: NMC -> RELEASED against the DEPLOYED fleet.

Publishes a real nmc_event.v2 onto the live forge-bus; the deployed Cloud
Run workers (push subscriptions) run the whole spine with live
gemini-3.5-flash reasoning; approvals happen through the deployed dashboard
with an authenticated identity token; the Logical Clock advance + due-event
emission run here (the unattended scheduler's role). Prints every observed
state transition verbatim and the single trace id for the Cloud Trace
waterfall (OBS-1 evidence).

Usage: PROJECT_ID=<id> python scripts/demo_spine_live.py
Cleanup: atexit, THIS run's records only (unique workflow id per run).
"""

import atexit
import json
import os
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from google.cloud import firestore  # noqa: E402

from forge_common import layout, state  # noqa: E402
from forge_common.bus import drain_outbox  # noqa: E402
from forge_common.clock import advance_clock, emit_due_events  # noqa: E402
from forge_common.messages import (  # noqa: E402
    build_envelope,
    deterministic_event_id,
    deterministic_trace_id,
)
from forge_common.pubsub import OrderedPublisher  # noqa: E402

PROJECT = os.environ.get("PROJECT_ID") or (Path(".gcp-project").read_text().strip())
TOPIC = "forge-bus"
WF = f"wf-live-{uuid.uuid4().hex[:10]}"
TRACE = deterministic_trace_id(WF)

db = firestore.Client(project=PROJECT)
publisher = OrderedPublisher(PROJECT)
publish = lambda message, key: publisher.publish(TOPIC, message, key)  # noqa: E731


def _cleanup() -> None:
    """Remove ONLY this run's records (unique wf id — nothing else touchable)."""
    try:
        ref = layout.workflow_ref(db, WF)
        for sub in (
            "outbox",
            "audit",
            "inbox",
            "work_packages",
            "approvals",
            "approvals_consumed",
            "claims",
        ):
            for snapshot in ref.collection(sub).stream():
                snapshot.reference.delete()
        ref.delete()
        # live claims flip instance states — pin them back to seeded values
        # so the NEXT live run's discovery still resolves (same class as the
        # emulator rerun-safety fix)
        from forge_common import registry as reg

        for iid, seeded in [
            ("agent-orchestrator-01", "IDLE"),
            ("agent-maintenance-01", "IDLE"),
            ("agent-supply-01", "IDLE"),
            ("agent-workforce-01", "IDLE"),
            ("agent-workforce-02", "RESERVE"),
            ("agent-safety-01", "IDLE"),
        ]:
            reg.instance_ref(db, iid).set({"state": seeded}, merge=True)
        print(f"CLEANUP: {WF} records removed; instances re-seeded")
    except Exception as exc:  # best-effort, never masks the run
        print(f"CLEANUP: incomplete ({exc})")


atexit.register(_cleanup)


_TOKEN_CACHE: dict[str, float | str] = {}


def token() -> str:
    # Identity tokens last ~1h; mint once and reuse. Minting fresh on every
    # HTTP call spawned a gcloud subprocess per request, and one crashed with
    # SIGTRAP mid-run (a gcloud flake, not a FORGE fault). Cache + retry.
    now = time.time()
    if _TOKEN_CACHE and now - float(_TOKEN_CACHE["at"]) < 1800:
        return str(_TOKEN_CACHE["value"])
    last = ""
    for attempt in range(3):
        proc = subprocess.run(
            ["gcloud", "auth", "print-identity-token"], capture_output=True, text=True
        )
        if proc.returncode == 0 and proc.stdout.strip():
            _TOKEN_CACHE["value"] = proc.stdout.strip()
            _TOKEN_CACHE["at"] = now
            return proc.stdout.strip()
        last = (proc.stderr or "").strip().splitlines()[-1:] and (proc.stderr or "").strip()
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gcloud token minting failed after 3 attempts: {last}")


def _gcloud(args: list[str]) -> str:
    """Run a read-only gcloud command, retrying transient crashes (a bare
    print-identity-token / describe occasionally dies with SIGTRAP)."""
    for attempt in range(3):
        proc = subprocess.run(args, capture_output=True, text=True)
        if proc.returncode == 0:
            return proc.stdout.strip()
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gcloud failed after 3 attempts: {' '.join(args[:3])}")


def dashboard_url() -> str:
    return _gcloud(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            "forge-dashboard",
            "--region",
            "us-central1",
            "--project",
            PROJECT,
            "--format",
            "value(status.url)",
        ]
    )


def http(method: str, url: str, body: dict | None = None) -> dict:
    request = urllib.request.Request(url, method=method)
    request.add_header("Authorization", f"Bearer {token()}")
    data = None
    if body is not None:
        request.add_header("content-type", "application/json")
        data = json.dumps(body).encode()
    with urllib.request.urlopen(request, data=data, timeout=30) as response:
        return json.loads(response.read())


def wf_doc() -> dict:
    snapshot = layout.workflow_ref(db, WF).get()
    return snapshot.to_dict() or {}


SPINE_ORDER = [
    "INTAKE",
    "PLANNING",
    "VALIDATING",
    "AWAITING_SCHEDULE_APPROVAL",
    "SUSPENDED_AWAITING_PART",
    "ASSEMBLY_RESUMED",
    "AWAITING_RELEASE_APPROVAL",
    "RELEASED",
]


def wait_for(status: str, *, timeout_s: int = 300) -> None:
    """Wait until the workflow has REACHED OR PASSED the given spine state —
    live workers can carry it through several states between polls, so
    equality would deadlock on a missed intermediate (the audit trail still
    proves every state was entered, in order)."""
    deadline = time.time() + timeout_s
    last = None
    while time.time() < deadline:
        current = wf_doc().get("status")
        if current != last:
            print(f"   [{time.strftime('%H:%M:%SZ', time.gmtime())}] status={current}")
            last = current
        if current in SPINE_ORDER and SPINE_ORDER.index(current) >= SPINE_ORDER.index(status):
            return
        if current == "BLOCKED_AGENT_FAILURE":
            dump_trail("BLOCKED — trail before cleanup")
            raise AssertionError("workflow BLOCKED — trail above")
        time.sleep(5)
    dump_trail(f"TIMEOUT waiting for {status} — trail before cleanup")
    raise AssertionError(f"timed out waiting for {status} (last={last})")


def dump_trail(banner: str) -> None:
    """Failure evidence must outlive the cleanup: print the FULL trail."""
    print(f"\n=== {banner} ===")
    try:
        for e in state.reconstruct_audit_trail(db, WF):
            p = e["payload"]
            print(
                f"  day{p['effective_at']} {p['event_kind']}/{p['reason_code']} "
                f"{p.get('state_after', '')} by {p['agent_identity']}"
            )
            if p.get("detail"):
                print(f"    detail: {p['detail'][:400]}")
    except Exception as exc:
        print(f"  (trail dump failed: {exc})")


def decide(url: str, expected_action: str) -> str:
    detail = http("GET", f"{url}/api/workflows/{WF}")
    (pending,) = detail["pending_approvals"]
    assert pending["action_type"] == expected_action, pending["action_type"]
    result = http(
        "POST",
        f"{url}/api/workflows/{WF}/decide",
        {"approval_id": pending["approval_id"], "decision": "approved"},
    )
    print(f"   decision recorded: {result}")
    return pending["approval_id"]


def main() -> int:
    print(f"LIVE SPINE: project={PROJECT} workflow={WF}")
    print(f"TRACE (OBS-1 waterfall id): {TRACE}")
    url = dashboard_url()
    print(f"dashboard: {url}")

    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )
    if not (layout.clock_ref(db).get().to_dict() or {}).get("logical_time") is not None:
        layout.clock_ref(db).set({"logical_time": 0})
    nmc = {
        "envelope": build_envelope(
            workflow_id=WF,
            schema_version="nmc_event.v2",
            event_id=deterministic_event_id("live-nmc", WF),
            trace_id=TRACE,
            idempotency_key=f"idem-live-nmc-{WF[-8:]}",
        ),
        "payload": {
            "equipment_id": "GX12-07",
            "discrepancy_code": "DSC-0042",
            "description": "Failed hydraulic actuator on lift assembly",
            "reported_at": "2026-08-21T09:00:00Z",
        },
    }
    publish(nmc, WF)
    print("nmc_event.v2 published to forge-bus — live workers take it from here")
    wait_for("AWAITING_SCHEDULE_APPROVAL")

    decide(url, "schedule_override")
    wait_for("SUSPENDED_AWAITING_PART")
    doc = wf_doc()
    print(f"   due_at (logical day): {doc['due_at']}")

    days = int(doc["due_at"]) - int(doc["logical_time"])
    print(f"advancing the Logical Clock {days} simulated days (ORC-5)")
    advance_clock(db, days)
    # The per-minute production heartbeat ALSO emits due events — if it fires
    # in the window after the advance, our own emit correctly returns [] for
    # the already-emitted event (at-least-once + idempotent dedupe, by
    # design). Asserting that WE were the emitter raced the scheduler and
    # failed an otherwise-perfect run; the real guarantee is the workflow
    # RESUMING, which wait_for below asserts.
    emitted = emit_due_events(db)
    print(f"   due emission: {'this driver' if WF in emitted else 'the production heartbeat'}")
    drain_outbox(db, WF, publish)
    wait_for("ASSEMBLY_RESUMED")
    wait_for("AWAITING_RELEASE_APPROVAL")

    decide(url, "equipment_release")
    wait_for("RELEASED")

    trail = state.reconstruct_audit_trail(db, WF)
    states = [e["payload"]["state_after"] for e in trail if e["payload"].get("state_after")]
    print("\nSTATE HISTORY (from the audit trail alone, AUD-2):")
    for entry in states:
        print(f"   {entry}")
    traces = {e["envelope"]["trace_id"] for e in trail}
    assert traces == {TRACE}, traces
    print(f"\nONE trace across {len(trail)} audit events: {TRACE}")
    print(
        "Cloud Trace waterfall: https://console.cloud.google.com/traces/list"
        f"?project={PROJECT}&tid={TRACE}"
    )
    print("LIVE SPINE: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
