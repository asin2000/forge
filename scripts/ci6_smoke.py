#!/usr/bin/env python3
"""CI-6 post-deploy smoke (WIF pipeline): one live-Gemini agent round-trip
and one Pub/Sub publish/consume, against the freshly deployed fleet.

Flow: a benign synthetic bulletin is ingested through the deployed Cyber
Trust service (quarantine-first; the tool-less Gemini classifier is the
live-model round-trip), which PUBLISHES a quarantine_verdict.v3 to the bus;
Safety CONSUMES it and its validation lands in the workflow's audit trail.
Both halves are asserted from Firestore alone. The smoke workflow and its
quarantine record are cleaned up on exit.

Auth: Application Default Credentials (the WIF-federated deployer service
account in GitHub Actions; any ADC principal with run.invoker on
forge-cyber-trust locally).
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
import uuid
from pathlib import Path

sys.path.insert(0, "src")
sys.path.insert(0, ".")

from google.cloud import firestore  # noqa: E402

from forge_common import layout, state  # noqa: E402
from forge_common.messages import deterministic_trace_id  # noqa: E402

PROJECT = os.environ.get("PROJECT_ID") or Path(".gcp-project").read_text().strip()
REGION = "us-central1"
WF = f"wf-ci6smoke-{uuid.uuid4().hex[:10]}"
DOC_ID = f"doc-ci6-{uuid.uuid4().hex[:8]}"
BENIGN = (
    "Routine vendor notice: scheduled maintenance window for the parts "
    "portal this weekend. No action required for GX-12 operators. "
    "Reference HYD-ACT-4402 stocking levels remain unchanged."
)

db = firestore.Client(project=PROJECT)


def cyber_trust_url() -> str:
    import subprocess

    return subprocess.run(
        [
            "gcloud",
            "run",
            "services",
            "describe",
            "forge-cyber-trust",
            "--region",
            REGION,
            "--project",
            PROJECT,
            "--format",
            "value(status.url)",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def id_token_for(audience: str) -> str:
    """An OIDC identity token for the ADC principal. Prefers google-auth's
    fetch_id_token (service-account / WIF-impersonated credentials — the
    GitHub Actions path); falls back to gcloud for interactive user ADC."""
    import subprocess

    import google.auth.transport.requests
    import google.oauth2.id_token

    try:
        return google.oauth2.id_token.fetch_id_token(
            google.auth.transport.requests.Request(), audience
        )
    except Exception:
        pass
    for args in (["--audiences", audience], []):
        proc = subprocess.run(
            ["gcloud", "auth", "print-identity-token", *args],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    raise RuntimeError("no identity-token path available for the smoke")


def cleanup() -> None:
    ref = layout.workflow_ref(db, WF)
    for sub in ("outbox", "audit", "inbox"):
        for snapshot in ref.collection(sub).stream():
            snapshot.reference.delete()
    ref.delete()
    doc = db.collection("quarantine").document(DOC_ID)
    if doc.get().to_dict():
        doc.delete()
    try:
        from google.cloud import storage

        storage.Client(project=PROJECT).bucket(f"forge-quarantine-{PROJECT}").blob(
            f"quarantine/{DOC_ID}"
        ).delete()
    except Exception:
        pass  # blob may not exist on early failure; never fail the cleanup


def main() -> int:
    url = cyber_trust_url()
    print(f"CI-6 smoke against {url}")
    state.create_workflow(
        db,
        workflow_id=WF,
        equipment_id="GX12-12",
        trace_id=deterministic_trace_id(WF),
        logical_time=0,
    )
    request = urllib.request.Request(
        f"{url}/ingest",
        data=json.dumps(
            {"workflow_id": WF, "doc_id": DOC_ID, "source": "ci6-smoke", "raw_text": BENIGN}
        ).encode("utf-8"),
        method="POST",
    )
    request.add_header("Authorization", f"Bearer {id_token_for(url)}")
    request.add_header("content-type", "application/json")
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.loads(response.read())
    print(f"ingest: {response.status} verdict_published={body.get('verdict_published')}")
    assert body.get("verdict_published") is True, "screening did not publish a verdict"

    # publish half: the quarantine_verdict.v3 left through the bus
    published = None
    consumed = None
    deadline = time.time() + 180
    while time.time() < deadline and not (published and consumed):
        for snapshot in layout.workflow_ref(db, WF).collection("outbox").stream():
            record = snapshot.to_dict()
            schema = record["message"]["envelope"]["schema_version"]
            if schema == "quarantine_verdict.v3" and record.get("published"):
                published = record["message"]["envelope"]["event_id"]
        # consume half: Safety's validation of that verdict is audited in
        # the SAME workflow trail (bus-delivered, claim/leased, committed)
        for event in state.reconstruct_audit_trail(db, WF):
            if event["payload"]["agent_identity"] == "agent-safety-01":
                consumed = event["payload"]["reason_code"]
        if not (published and consumed):
            time.sleep(5)
    assert published, "quarantine_verdict.v3 was never published to the bus"
    assert consumed, "no Safety consumption evidence appeared in the audit trail"
    print(f"publish/consume proven: verdict {published[:8]}… -> safety audit {consumed}")
    print("CI-6 SMOKE: PASS (live Gemini round-trip + bus publish/consume)")
    return 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        cleanup()
    sys.exit(code)
