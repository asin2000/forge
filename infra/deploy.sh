#!/usr/bin/env bash
# FORGE deployment (PLT-6): stands up the environment from a clean project.
# Prerequisites: authenticated principal, PROJECT_ID, billing enabled, and
# infra/setup-gcp.sh already run (APIs + Firestore). Idempotent.
#
# Day-6 scope: per-role service accounts + IAM (AGT-6), quarantine bucket
# IAM (Cyber Trust read/write ONLY — AGT-5), Pub/Sub topic/subscriptions
# with regional storage policy + DLQ (ICD-5/DAT-1), registry load (REG-1),
# and the dashboard/approval surface on Cloud Run behind IAM (HUM-1).
# Agent worker services deploy in Lane 2 (Day 7) via the same pattern.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID env var}"
REGION="${REGION:-us-central1}"
PYTHON="${PYTHON:-python3}"            # interpreter with requirements.lock installed
DEPLOY_DASHBOARD="${DEPLOY_DASHBOARD:-1}"  # 0 = infra only (Lane-2 split)   # DAT-1 map: everything but Gemini
ROLES=(orchestrator maintenance supply workforce safety cyber-trust dashboard)
BUCKET="forge-quarantine-${PROJECT_ID}"

echo "== service accounts (AGT-6: one identity per role)"
for role in "${ROLES[@]}"; do
  SA="forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud iam service-accounts describe "$SA" --project "$PROJECT_ID" >/dev/null 2>&1 && continue
  # clean projects hit the 5-per-minute SA creation quota (429) — back off and retry
  for attempt in 1 2 3; do
    if gcloud iam service-accounts create "forge-${role}" --project "$PROJECT_ID" \
         --display-name "FORGE ${role}"; then
      break
    elif [ "$attempt" = 3 ]; then
      echo "FATAL: could not create ${SA}" >&2; exit 1
    else
      echo "   quota backoff (${SA}), retrying in 65s"; sleep 65
    fi
  done
done

echo "== Firestore + Pub/Sub role bindings"
for role in "${ROLES[@]}"; do
  SA="forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:${SA}" --role roles/datastore.user
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:${SA}" --role roles/pubsub.editor
done
# Vertex for reasoning roles only; Model Armor for Cyber Trust only.
for role in orchestrator maintenance supply workforce safety cyber-trust; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role roles/aiplatform.user
done
gcloud projects add-iam-policy-binding "$PROJECT_ID" --quiet >/dev/null \
  --member "serviceAccount:forge-cyber-trust@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/modelarmor.user

echo "== quarantine bucket IAM (AGT-5: Cyber Trust ONLY)"
gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1 \
  || gcloud storage buckets create "gs://${BUCKET}" --project "$PROJECT_ID" \
       --location "$REGION" --uniform-bucket-level-access
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" --quiet >/dev/null \
  --member "serviceAccount:forge-cyber-trust@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/storage.objectAdmin

echo "== Pub/Sub bus (regional storage policy + DLQ per ICD-5/DAT-1)"
PROJECT_ID="$PROJECT_ID" "$PYTHON" - << 'PYEOF'
import os, sys
sys.path.insert(0, "src")
from forge_common.pubsub import ensure_topic_and_subscription
project = os.environ["PROJECT_ID"]
ensure_topic_and_subscription(
    project, "forge-bus", "forge-bus-sub", dead_letter_topic="forge-bus-dlq"
)
print("   forge-bus + forge-bus-sub (+dlq) ready")
PYEOF

echo "== registry load (REG-1, PLT-6 step)"
PROJECT_ID="$PROJECT_ID" "$PYTHON" - << 'PYEOF'
import os, sys
sys.path.insert(0, "src")
from google.cloud import firestore
from forge_common.registry import load_registry
db = firestore.Client(project=os.environ["PROJECT_ID"])
print("   loaded:", ", ".join(load_registry(db)))
PYEOF

if [ "$DEPLOY_DASHBOARD" = "1" ]; then
echo "== dashboard / approval surface (HUM-1: Cloud Run IAM, no unauthenticated)"
gcloud run deploy forge-dashboard \
  --project "$PROJECT_ID" --region "$REGION" \
  --source . \
  --service-account "forge-dashboard@${PROJECT_ID}.iam.gserviceaccount.com" \
  --no-allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},FORGE_AUTH_MODE=cloudrun-iam" \
  `# cloudrun-iam: identity ONLY from the platform-validated bearer token (Cloud` \
  `# Run strips its signature after verifying) — safe ONLY with` \
  `# --no-allow-unauthenticated. The plain x-goog-authenticated-user-email` \
  `# header is client-forgeable and is ignored in every mode (HUM-1).` \
  --quiet
fi

echo "== done. Approver access: grant roles/run.invoker on forge-dashboard to the approver principal."
