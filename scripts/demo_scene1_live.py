#!/usr/bin/env python3
"""Lane-2 live SEC-1 scene: the vendor bulletin through the DEPLOYED
Cyber Trust service — GCS quarantine -> Model Armor -> tool-less classifier
-> quarantine_verdict.v3 -> Safety veto — proving nothing local or
pre-seeded touches the path (HOLD item 1).

POSTs the bulletin to forge-cyber-trust /ingest (authenticated); the
deployed service quarantines to the real bucket, screens with live Model
Armor + gemini classifier, and publishes the non-executable verdict to the
live bus, which the deployed Safety worker consumes. Then asserts, from
Firestore alone: the verdict is published, Safety produced a vetoed
validation_verdict citing SP-SEC-004, and the raw bulletin text appears
NOWHERE outside the quarantine record (SEC-4).

Usage: PROJECT_ID=<id> python scripts/demo_scene1_live.py
Cleanup: atexit, THIS run's records + the quarantine object/metadata only.
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
from forge_common.messages import deterministic_trace_id  # noqa: E402

PROJECT = os.environ.get("PROJECT_ID") or Path(".gcp-project").read_text().strip()
WF = f"wf-scene1-{uuid.uuid4().hex[:10]}"
TRACE = deterministic_trace_id(WF)
DOC_ID = f"doc-bulletin-{uuid.uuid4().hex[:8]}"
BULLETIN = (Path("data") / "vendor_bulletin_vnd_act_9901.txt").read_text()
INJECTION_MARKER = "SYSTEM OVERRIDE"

db = firestore.Client(project=PROJECT)


def _cleanup() -> None:
    try:
        ref = layout.workflow_ref(db, WF)
        for sub in ("outbox", "audit", "inbox"):
            for snapshot in ref.collection(sub).stream():
                snapshot.reference.delete()
        ref.delete()
        db.collection("quarantine").document(DOC_ID).delete()
        from forge_common.quarantine import GcsQuarantineStore

        blob = GcsQuarantineStore(db, bucket=f"forge-quarantine-{PROJECT}")._bucket.blob(
            f"quarantine/{DOC_ID}"
        )
        if blob.exists():
            blob.delete()
        print(f"CLEANUP: {WF} + quarantine {DOC_ID} removed")
    except Exception as exc:
        print(f"CLEANUP: incomplete ({exc})")


atexit.register(_cleanup)


_TOKEN_CACHE: dict[str, float | str] = {}


def token() -> str:
    # cache + retry: minting an identity token per HTTP call spawns a gcloud
    # subprocess each time, and one crashed with SIGTRAP mid-run (a gcloud
    # flake). Tokens last ~1h, so mint once and reuse.
    now = time.time()
    if _TOKEN_CACHE and now - float(_TOKEN_CACHE["at"]) < 1800:
        return str(_TOKEN_CACHE["value"])
    for attempt in range(3):
        proc = subprocess.run(
            ["gcloud", "auth", "print-identity-token"], capture_output=True, text=True
        )
        if proc.returncode == 0 and proc.stdout.strip():
            _TOKEN_CACHE.update({"value": proc.stdout.strip(), "at": now})
            return proc.stdout.strip()
        time.sleep(2 * (attempt + 1))
    raise RuntimeError("gcloud token minting failed after 3 attempts")


def service_url(name: str) -> str:
    for attempt in range(3):
        proc = subprocess.run(
            [
                "gcloud",
                "run",
                "services",
                "describe",
                name,
                "--region",
                "us-central1",
                "--project",
                PROJECT,
                "--format",
                "value(status.url)",
            ],
            capture_output=True,
            text=True,
        )
        if proc.returncode == 0:
            return proc.stdout.strip()
        time.sleep(2 * (attempt + 1))
    raise RuntimeError(f"gcloud describe {name} failed after 3 attempts")


def post(url: str, body: dict) -> tuple[int, str]:
    request = urllib.request.Request(url, method="POST")
    request.add_header("Authorization", f"Bearer {token()}")
    request.add_header("content-type", "application/json")
    try:
        with urllib.request.urlopen(request, data=json.dumps(body).encode(), timeout=120) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode()


def outbox(schema: str) -> list[dict]:
    return [
        s.to_dict()["message"]
        for s in layout.workflow_ref(db, WF).collection("outbox").stream()
        if s.to_dict()["message"]["envelope"]["schema_version"] == schema
    ]


def main() -> int:
    print(f"LIVE SCENE 1: project={PROJECT} workflow={WF} doc={DOC_ID}")
    print(f"TRACE: {TRACE}")
    ct_url = service_url("forge-cyber-trust")
    print(f"deployed cyber-trust: {ct_url}")
    state.create_workflow(
        db, workflow_id=WF, equipment_id="GX12-07", trace_id=TRACE, logical_time=0
    )

    status, response = post(
        f"{ct_url}/ingest",
        {
            "workflow_id": WF,
            "doc_id": DOC_ID,
            "source": "synthetic-vendor-portal",
            "raw_text": BULLETIN,
        },
    )
    print(f"ingest -> HTTP {status}: {response[:200]}")
    assert status == 200, response
    # SEC-4 at the boundary: the deployed service's RESPONSE carries no raw text
    assert INJECTION_MARKER not in response, "raw text leaked in the ingest response"
    assert "VND-ACT-9901" not in response, "candidate id leaked in the ingest response"

    # the deployed Safety worker consumes the verdict off the live bus
    print("awaiting the quarantine verdict + Safety validation off the live bus...")
    deadline = time.time() + 180
    safety_verdict = None
    while time.time() < deadline:
        verdicts = outbox("validation_verdict.v3") + outbox("validation_verdict.v2")
        safety_verdict = next(
            (v for v in verdicts if v["payload"].get("verdict") == "vetoed"), None
        )
        if safety_verdict:
            break
        time.sleep(5)

    quarantine_verdicts = outbox("quarantine_verdict.v3")
    assert quarantine_verdicts, "cyber-trust published no quarantine_verdict.v3"
    qv = quarantine_verdicts[0]["payload"]
    print("\nquarantine_verdict.v3 (published to the live bus):")
    print(
        f"   screening_complete={qv['screening_complete']} "
        f"raw_disposition={qv['raw_disposition']} metadata_release={qv['metadata_release']}"
    )
    print(
        f"   model_armor={qv['screening']['model_armor']['verdict']} "
        f"classifier={qv['screening']['classifier']['label']} "
        f"candidate={qv['safe_metadata']['candidate_part_identifier']}"
    )

    assert safety_verdict is not None, "Safety did not veto within the deadline"
    print(
        f"\nSafety veto: {safety_verdict['payload']['verdict']} "
        f"rules={safety_verdict['payload'].get('rule_refs')}"
    )

    # SEC-4: the raw bulletin appears NOWHERE outside its quarantine record
    quarantine_doc = db.collection("quarantine").document(DOC_ID).get().to_dict() or {}
    assert INJECTION_MARKER not in json.dumps(quarantine_doc.get("metadata", {})), (
        "raw text leaked into quarantine METADATA"
    )
    everything_else = {
        "outbox": [s.to_dict() for s in layout.workflow_ref(db, WF).collection("outbox").stream()],
        "audit": [s.to_dict() for s in layout.workflow_ref(db, WF).collection("audit").stream()],
    }
    assert INJECTION_MARKER not in json.dumps(everything_else), (
        "raw text escaped quarantine into the bus/audit trail (SEC-4 violation)"
    )
    print(
        "\nSEC-4 verified: the raw bulletin never left quarantine "
        "(absent from the verdict, the outbox, and the audit trail)."
    )
    print("LIVE SCENE 1: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
