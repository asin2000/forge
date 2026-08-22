#!/usr/bin/env bash
# FORGE Day-1 GCP bootstrap (run from any machine with gcloud authenticated).
# Prerequisites (PLT-6): authenticated principal, project ID, billing enabled.
# Usage: PROJECT_ID=your-project ./infra/setup-gcp.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID env var}"
REGION="${REGION:-us-central1}"   # single pinned region (PLT-1)

echo "== FORGE GCP bootstrap: project=${PROJECT_ID} region=${REGION}"
gcloud config set project "${PROJECT_ID}"

echo "== Billing check"
gcloud billing projects describe "${PROJECT_ID}" \
  --format="value(billingEnabled)" | grep -q True \
  || { echo "FATAL: billing not enabled on ${PROJECT_ID}"; exit 1; }

echo "== Enabling APIs (Cloud Run, Firestore, Pub/Sub, Vertex, Model Armor, IAM, build)"
gcloud services enable \
  run.googleapis.com \
  firestore.googleapis.com \
  pubsub.googleapis.com \
  aiplatform.googleapis.com \
  modelarmor.googleapis.com \
  iam.googleapis.com \
  iamcredentials.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  cloudscheduler.googleapis.com \
  compute.googleapis.com \
  storage.googleapis.com

echo "== Provisioning the Pub/Sub service agent (dead-letter forwarding identity)"
# On a brand-new project the agent may not exist yet; provision it
# deterministically so the DLQ IAM grants in deploy.sh never race it.
gcloud beta services identity create --service=pubsub.googleapis.com \
  --project "${PROJECT_ID}" || true

echo "== Firestore database (native mode) in ${REGION}"
gcloud firestore databases create --location="${REGION}" --type=firestore-native \
  || echo "   (database may already exist — fine)"

echo "== Model Armor screening template (PLT-4/SEC-1: forge-screening, LOW_AND_ABOVE)"
# cyber-trust screens against this template at runtime; a genuinely clean
# project has none, so setup creates it (idempotent — 409 = already exists).
PROJECT_ID="${PROJECT_ID}" REGION="${REGION}" python3 - << 'PYARMOR' || echo "   (Model Armor template step skipped: $?)"
import json, os, urllib.error, urllib.request, subprocess
project, region = os.environ["PROJECT_ID"], os.environ["REGION"]
tok = subprocess.run(["gcloud","auth","print-access-token"],capture_output=True,text=True).stdout.strip()
host = f"https://modelarmor.{region}.rep.googleapis.com/v1"
parent = f"projects/{project}/locations/{region}"
body = {"filterConfig": {"piAndJailbreakFilterSettings": {"filterEnforcement": "ENABLED", "confidenceLevel": "LOW_AND_ABOVE"}}}
req = urllib.request.Request(f"{host}/{parent}/templates?templateId=forge-screening",
    data=json.dumps(body).encode(), method="POST",
    headers={"Authorization": f"Bearer {tok}", "Content-Type": "application/json"})
try:
    urllib.request.urlopen(req); print("   forge-screening template created")
except urllib.error.HTTPError as e:
    print("   forge-screening template exists" if e.code == 409 else f"   template HTTP {e.code}")
PYARMOR

echo "== Quota sanity: Vertex AI gemini-3.5-flash in ${REGION}"
echo "   Check quotas in console: IAM & Admin -> Quotas -> Vertex AI API."
echo "   If request-per-minute quota < 200, file an increase TODAY (approvals take days; risk R5)."

echo "== Done. Now run the two smoke tests (risk R2):"
echo "   python scripts/smoke_vertex.py       # PLT-1 model reachable"
echo "   python scripts/smoke_model_armor.py  # PLT-4 Model Armor reachable"
