#!/usr/bin/env bash
# FORGE deployment (PLT-6): ONE documented command stands up the environment
# from a clean project. Prerequisites: authenticated principal, PROJECT_ID,
# billing enabled, APPLICATION DEFAULT CREDENTIALS (gcloud auth
# application-default login), CWD = repo root, and PYTHON pointing at an
# interpreter with requirements.lock installed. Idempotent — re-running
# converges, never duplicates.
#
# What it does, in order:
#   0. Bootstrap (chains infra/setup-gcp.sh: APIs + Firestore) unless SKIP_SETUP=1
#   1. Per-role service accounts (AGT-6), with 429 backoff (5-SA/min quota)
#   2. SCOPED IAM (AGT-6): per-topic publisher, per-need Vertex/Armor, bucket
#      binding for Cyber Trust ONLY. Firestore limitation documented below.
#   3. Cloud Logging: REGIONAL bucket + _Default sink redirect (DAT-1)
#   4. Bus: forge-bus topic + DLQ topic + DLQ inspection subscription
#   5. Registry load (REG-1)
#   6. One container build; 5 agent workers + dashboard on Cloud Run
#      (--no-allow-unauthenticated; workers keyed by WORKER_ROLE)
#   7. Filtered, ordered PUSH subscriptions per role (OIDC = role SA; DLQ
#      after 5 attempts per ICD-5)
#
# AGT-6 honesty note (Firestore): roles/datastore.user is project-scoped —
# Firestore has NO per-collection IAM primitive. Compensating controls:
# single sanctioned writer paths (state.apply_transition,
# record_approval_decision), transactional ownership guards, and AUD audits.
# Stated here and in the README rather than pretended away.
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID env var}"
REGION="${REGION:-us-central1}"        # DAT-1 map: everything but Gemini
PYTHON="${PYTHON:-python3}"            # interpreter with requirements.lock installed
SKIP_SETUP="${SKIP_SETUP:-0}"
DEPLOY_SERVICES="${DEPLOY_SERVICES:-1}"   # 0 = infra only
ROLES=(orchestrator maintenance supply workforce safety cyber-trust dashboard)
AGENT_ROLES=(orchestrator maintenance supply workforce safety)  # bus consumers
BUCKET="forge-quarantine-${PROJECT_ID}"
TOPIC="forge-bus"
DLQ_TOPIC="forge-bus-dlq"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/forge/forge:latest"
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
PUBSUB_AGENT="service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com"

if [ "$SKIP_SETUP" != "1" ]; then
  echo "== bootstrap (setup-gcp.sh: APIs + Firestore)"
  PROJECT_ID="$PROJECT_ID" REGION="$REGION" bash "$(dirname "$0")/setup-gcp.sh"
fi

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

echo "== IAM (AGT-6 scoped): Firestore per role; Pub/Sub PER TOPIC, never project-wide"
for role in "${ROLES[@]}"; do
  SA="forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:${SA}" --role roles/datastore.user
  # retire the Day-6 project-wide grant if present (idempotent)
  gcloud projects remove-iam-policy-binding "$PROJECT_ID" --quiet >/dev/null 2>&1 \
    --member "serviceAccount:${SA}" --role roles/pubsub.editor || true
done

echo "== Pub/Sub bus (regional storage policy + DLQ per ICD-5/DAT-1)"
PROJECT_ID="$PROJECT_ID" "$PYTHON" - << 'PYEOF'
import os, sys
sys.path.insert(0, "src")
from forge_common.pubsub import ensure_topic_and_subscription
project = os.environ["PROJECT_ID"]
# topic + DLQ topic + a PULL subscription on the DLQ so dead letters are
# retained and inspectable (CI-6 live evidence)
ensure_topic_and_subscription(project, "forge-bus-dlq", "forge-bus-dlq-sub")
ensure_topic_and_subscription(project, "forge-bus", "forge-bus-audit-tap")
print("   forge-bus, forge-bus-dlq (+dlq inspection sub) ready")
PYEOF

echo "== per-topic publisher bindings (agents + cyber-trust + dashboard; nothing more)"
for role in "${AGENT_ROLES[@]}" cyber-trust dashboard; do
  SA="forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com"
  gcloud pubsub topics add-iam-policy-binding "$TOPIC" --project "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:${SA}" --role roles/pubsub.publisher
done
# the Pub/Sub service agent forwards dead letters and mints push OIDC tokens.
# Dead-lettering needs BOTH grants (observed live: with only the DLQ-publisher
# half, exhausted messages keep redelivering instead of forwarding):
gcloud pubsub topics add-iam-policy-binding "$DLQ_TOPIC" --project "$PROJECT_ID" --quiet >/dev/null \
  --member "serviceAccount:${PUBSUB_AGENT}" --role roles/pubsub.publisher
# (the per-SUBSCRIPTION subscriber grants live AFTER the push subscriptions
# are created, in the DEPLOY_SERVICES block — granting here on a clean
# project would silently no-op against subscriptions that don't exist yet)
for role in "${AGENT_ROLES[@]}"; do
  gcloud iam service-accounts add-iam-policy-binding \
    "forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com" --project "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:${PUBSUB_AGENT}" --role roles/iam.serviceAccountTokenCreator
done

echo "== Cloud Trace write (OBS-1 span export, all services)"
for role in "${ROLES[@]}"; do
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role roles/cloudtrace.agent
done

echo "== Vertex (reasoning roles) + Model Armor (Cyber Trust ONLY)"
for role in "${AGENT_ROLES[@]}" cyber-trust; do
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

echo "== Cloud Logging: regional bucket + _Default redirect (DAT-1)"
gcloud logging buckets describe forge-logs --location="$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud logging buckets create forge-logs --location="$REGION" --project "$PROJECT_ID" \
       --description "FORGE operational logs, regionalized per DAT-1"
gcloud logging sinks update _Default \
  "logging.googleapis.com/projects/${PROJECT_ID}/locations/${REGION}/buckets/forge-logs" \
  --project "$PROJECT_ID" --quiet
# _Required cannot be relocated post-creation; it holds only Google admin
# activity audit entries, never operational payload data (DAT-3 disclosure).

echo "== registry load (REG-1, PLT-6 step)"
PROJECT_ID="$PROJECT_ID" "$PYTHON" - << 'PYEOF'
import os, sys
sys.path.insert(0, "src")
from google.cloud import firestore
from forge_common.registry import load_registry
db = firestore.Client(project=os.environ["PROJECT_ID"])
print("   loaded:", ", ".join(load_registry(db)))
PYEOF

if [ "$DEPLOY_SERVICES" = "1" ]; then
echo "== container image (one build for workers + dashboard)"
gcloud artifacts repositories describe forge --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1 \
  || gcloud artifacts repositories create forge --repository-format=docker \
       --location "$REGION" --project "$PROJECT_ID"
gcloud builds submit --tag "$IMAGE" --project "$PROJECT_ID" --quiet

echo "== agent + cyber-trust services on Cloud Run (--no-allow-unauthenticated)"
# cyber-trust is a SERVICE too: it consumes nothing from the bus (SEC-4) but
# serves POST /ingest (SEC-1) and publishes quarantine_verdict.v3.
for role in "${AGENT_ROLES[@]}" cyber-trust; do
  role_env="${role//-/_}"
  gcloud run deploy "forge-${role}" \
    --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
    --service-account "forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --no-allow-unauthenticated \
    --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},WORKER_ROLE=${role_env},FORGE_BUS_TOPIC=${TOPIC},FORGE_TRACE_EXPORT=cloud,ADK_CAPTURE_MESSAGE_CONTENT_IN_SPANS=false,GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_LOCATION=us" \
    --quiet
done

echo "== dashboard / operator console (HUM-1/HUM-3: Cloud Run IAM, no unauthenticated)"
CT_URL="$(gcloud run services describe forge-cyber-trust --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
gcloud run deploy forge-dashboard \
  --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" \
  --service-account "forge-dashboard@${PROJECT_ID}.iam.gserviceaccount.com" \
  --no-allow-unauthenticated \
  --set-env-vars "GOOGLE_CLOUD_PROJECT=${PROJECT_ID},FORGE_AUTH_MODE=cloudrun-iam,FORGE_BUS_TOPIC=${TOPIC},FORGE_TRACE_EXPORT=cloud,FORGE_CYBER_TRUST_URL=${CT_URL}" \
  `# cloudrun-iam: identity ONLY from the platform-validated bearer token;` \
  `# the plain x-goog-authenticated-user-email header is forgeable and ignored.` \
  --quiet

echo "== filtered push subscriptions (OIDC = role SA; DLQ after 5 per ICD-5)"
filter_for() {  # Pub/Sub filters cap at 256 chars — a single existence
  # check per role; the publisher stamps to_<role> from forge_common.pubsub.ROUTING
  echo "attributes:to_$1"
}
for role in "${AGENT_ROLES[@]}"; do
  URL="$(gcloud run services describe "forge-${role}" --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
  # the pushing identity must be allowed to invoke the worker
  gcloud run services add-iam-policy-binding "forge-${role}" --region "$REGION" --project "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:forge-${role}@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role roles/run.invoker
  # REG-1: the Orchestrator performs the authenticated fleet health probe
  gcloud run services add-iam-policy-binding "forge-${role}" --region "$REGION" --project "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:forge-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com" \
    --role roles/run.invoker
  PROJECT_ID="$PROJECT_ID" ROLE="$role" URL="$URL" FILTER="$(filter_for "$role")" "$PYTHON" - << 'PYEOF'
import os, sys
sys.path.insert(0, "src")
from forge_common.pubsub import ensure_push_subscription
role = os.environ["ROLE"]
ensure_push_subscription(
    os.environ["PROJECT_ID"], "forge-bus", f"forge-bus-{role}",
    push_endpoint=os.environ["URL"] + "/",
    service_account=f"forge-{role}@{os.environ['PROJECT_ID']}.iam.gserviceaccount.com",
    filter_expression=os.environ["FILTER"],
    dead_letter_topic="forge-bus-dlq",
)
print(f"   forge-bus-{role} -> {os.environ['URL']}")
PYEOF
done

# REG-1 probe access to cyber-trust (deployed service, no push subscription)
gcloud run services add-iam-policy-binding "forge-cyber-trust" --region "$REGION" --project "$PROJECT_ID" --quiet >/dev/null \
  --member "serviceAccount:forge-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/run.invoker

# HUM-3: the operator console relays the poisoned-bulletin anomaly to the
# deployed cyber-trust /ingest — invoke ONLY; no other grant widens.
gcloud run services add-iam-policy-binding "forge-cyber-trust" --region "$REGION" --project "$PROJECT_ID" --quiet >/dev/null \
  --member "serviceAccount:forge-dashboard@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role roles/run.invoker

echo "== retiring the Day-6 legacy pull subscription (workers use push)"
gcloud pubsub subscriptions delete forge-bus-sub --project "$PROJECT_ID" --quiet >/dev/null 2>&1 || true

echo "== dead-letter forwarding grants (BOTH halves; subscriptions now exist)"
for role in "${AGENT_ROLES[@]}"; do
  gcloud pubsub subscriptions add-iam-policy-binding "forge-bus-${role}" --project "$PROJECT_ID" --quiet >/dev/null \
    --member "serviceAccount:${PUBSUB_AGENT}" --role roles/pubsub.subscriber
done

echo "== heartbeat (ORC-4 monitor + ORC-5 due events + outbox sweep, every minute)"
ORCH_URL="$(gcloud run services describe forge-orchestrator --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)')"
if gcloud scheduler jobs describe forge-tick --location "$REGION" --project "$PROJECT_ID" >/dev/null 2>&1; then
  gcloud scheduler jobs update http forge-tick --location "$REGION" --project "$PROJECT_ID" \
    --schedule "* * * * *" --uri "${ORCH_URL}/tick" --http-method POST \
    --oidc-service-account-email "forge-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com" --quiet >/dev/null
else
  gcloud scheduler jobs create http forge-tick --location "$REGION" --project "$PROJECT_ID" \
    --schedule "* * * * *" --uri "${ORCH_URL}/tick" --http-method POST \
    --oidc-service-account-email "forge-orchestrator@${PROJECT_ID}.iam.gserviceaccount.com" --quiet >/dev/null
fi
echo "   forge-tick -> ${ORCH_URL}/tick (OIDC: orchestrator SA)"
fi

echo "== done. Approver access: grant roles/run.invoker on forge-dashboard to the approver principal."
