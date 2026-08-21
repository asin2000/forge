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
  storage.googleapis.com

echo "== Firestore database (native mode) in ${REGION}"
gcloud firestore databases create --location="${REGION}" --type=firestore-native \
  || echo "   (database may already exist — fine)"

echo "== Quota sanity: Vertex AI gemini-3.5-flash in ${REGION}"
echo "   Check quotas in console: IAM & Admin -> Quotas -> Vertex AI API."
echo "   If request-per-minute quota < 200, file an increase TODAY (approvals take days; risk R5)."

echo "== Done. Now run the two smoke tests (risk R2):"
echo "   python scripts/smoke_vertex.py       # PLT-1 model reachable"
echo "   python scripts/smoke_model_armor.py  # PLT-4 Model Armor reachable"
