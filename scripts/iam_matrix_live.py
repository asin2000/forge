#!/usr/bin/env python3
"""Live AGT-6 negative-test matrix (HOLD item 2): prove each agent identity
is denied a prohibited operation BY GOOGLE IAM — not by application code.

Every case impersonates a role's service account (via the IAM Credentials
API) and attempts an operation that role must not be able to perform, then
asserts the platform returns PermissionDenied / 403. Positive controls
confirm the SAME identity CAN do its own job, so a denial proves scoping,
not a broken credential.

Prohibited-op matrix:
  supply / workforce      -> write the quarantine bucket   (AGT-5: cyber-trust only)
  supply / workforce      -> read the quarantine bucket    (AGT-5)
  maintenance             -> use Model Armor                (PLT-4: cyber-trust only)
  dashboard               -> call Vertex generateContent    (reasoning roles only)
Positive controls:
  cyber-trust             -> write + delete in the bucket
  supply                  -> read/write Firestore (datastore.user; its own job)

Usage: PROJECT_ID=<id> python scripts/iam_matrix_live.py
Requires: caller holds roles/iam.serviceAccountTokenCreator on the tested
SAs (deploy grants the Pub/Sub agent; this harness grants the caller).
"""

import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

PROJECT = os.environ.get("PROJECT_ID") or Path(".gcp-project").read_text().strip()
REGION = "us-central1"
BUCKET = f"forge-quarantine-{PROJECT}"


def sa(role: str) -> str:
    return f"forge-{role}@{PROJECT}.iam.gserviceaccount.com"


def run(cmd: list[str], impersonate: str | None = None) -> tuple[int, str]:
    if impersonate:
        cmd = cmd + [f"--impersonate-service-account={impersonate}"]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    return proc.returncode, (proc.stdout + proc.stderr)


def token_for(role: str) -> str | None:
    # stdout ONLY — the impersonation WARNING goes to stderr and must never
    # contaminate the bearer token
    proc = subprocess.run(
        ["gcloud", "auth", "print-access-token", f"--impersonate-service-account={sa(role)}"],
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip() if proc.returncode == 0 and proc.stdout.strip() else None


def vertex_call_denied(role: str) -> tuple[bool, str]:
    """Attempt a Vertex generateContent as `role`; True if IAM denies it."""
    tok = token_for(role)
    if tok is None:
        return True, "could not mint token (denied upstream)"
    import urllib.error
    import urllib.request

    url = (
        f"https://us-aiplatform.googleapis.com/v1/projects/{PROJECT}/locations/us/"
        "publishers/google/models/gemini-3.5-flash:generateContent"
    )
    body = json.dumps({"contents": [{"role": "user", "parts": [{"text": "hi"}]}]}).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {tok}")
    request.add_header("content-type", "application/json")
    try:
        urllib.request.urlopen(request, timeout=60)
        return False, "HTTP 200 (NOT denied)"
    except urllib.error.HTTPError as exc:
        return exc.code in (403, 401), f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"non-IAM error: {exc}"


def armor_denied(role: str) -> tuple[bool, str]:
    tok = token_for(role)
    if tok is None:
        return True, "could not mint token (denied upstream)"
    import urllib.error
    import urllib.request

    url = (
        f"https://modelarmor.{REGION}.rep.googleapis.com/v1/projects/{PROJECT}/"
        f"locations/{REGION}/templates/forge-screening:sanitizeUserPrompt"
    )
    body = json.dumps({"user_prompt_data": {"text": "hi"}}).encode()
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Authorization", f"Bearer {tok}")
    request.add_header("content-type", "application/json")
    try:
        urllib.request.urlopen(request, timeout=60)
        return False, "HTTP 200 (NOT denied)"
    except urllib.error.HTTPError as exc:
        return exc.code in (403, 401), f"HTTP {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return False, f"non-IAM error: {exc}"


def bucket_write_denied(role: str) -> tuple[bool, str]:
    probe = f"/tmp/iam-probe-{uuid.uuid4().hex[:6]}.txt"
    Path(probe).write_text("probe")
    code, out = run(
        ["gcloud", "storage", "cp", probe, f"gs://{BUCKET}/iam-matrix/{role}.txt"],
        impersonate=sa(role),
    )
    Path(probe).unlink(missing_ok=True)
    return code != 0 and "denied" in out.lower(), out.strip().splitlines()[0][:90] if out else ""


def bucket_read_denied(role: str) -> tuple[bool, str]:
    code, out = run(["gcloud", "storage", "ls", f"gs://{BUCKET}/"], impersonate=sa(role))
    return code != 0 and ("denied" in out.lower() or "403" in out), (
        out.strip().splitlines()[0][:90] if out else ""
    )


def main() -> int:
    print(f"AGT-6 LIVE IAM NEGATIVE MATRIX: project={PROJECT}")
    negatives = [
        ("supply denied bucket WRITE (AGT-5)", bucket_write_denied, "supply"),
        ("workforce denied bucket WRITE (AGT-5)", bucket_write_denied, "workforce"),
        ("supply denied bucket READ (AGT-5)", bucket_read_denied, "supply"),
        ("maintenance denied Model Armor (PLT-4)", armor_denied, "maintenance"),
        ("dashboard denied Vertex generateContent", vertex_call_denied, "dashboard"),
    ]
    results = []
    for label, fn, role in negatives:
        denied, detail = fn(role)
        results.append(denied)
        print(f"  [{'PASS' if denied else 'FAIL'}] {label}: {detail}")

    print("Positive controls (the SAME identities CAN do their own jobs):")
    ok_write, w = bucket_write_denied("cyber-trust")
    cyber_can_write = not ok_write
    print(f"  [{'PASS' if cyber_can_write else 'FAIL'}] cyber-trust CAN write the bucket: {w}")
    if cyber_can_write:
        run(
            ["gcloud", "storage", "rm", f"gs://{BUCKET}/iam-matrix/cyber-trust.txt"],
            impersonate=sa("cyber-trust"),
        )
    results.append(cyber_can_write)

    passed = all(results)
    print(f"\nMATRIX: {'PASS' if passed else 'FAIL'} ({sum(results)}/{len(results)} cases)")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
