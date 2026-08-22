# FORGE demo runbook — ≤4-minute video

Record against the **tagged runtime**. The demo drivers call the *deployed*
Cloud Run fleet on `forge-agentic-0821-6418`, which was built and deployed
from `v1.2-demo` (commit `c3b04d8`) — so you do **not** checkout the tag; you
just run the scripts from `main` (whose runtime code is byte-identical to the
tag — only submission docs have changed since). Do not modify runtime code or
touch the infrastructure during capture. The dress rehearsal ran both scenes
in 68 s, so there is comfortable headroom to narrate.

## Before you hit record

```bash
cd ~/dev/forge
export PROJECT_ID=$(cat .gcp-project)
gcloud auth application-default print-access-token >/dev/null   # ADC warm
# sanity: the deployed fleet is the frozen runtime
gcloud run services list --project "$PROJECT_ID" --region us-central1 \
  --format="value(metadata.name,status.conditions[0].status)"

# 1) reset the synthetic clock so the demo reads Day 0 -> Day 21
PROJECT_ID=$PROJECT_ID python - <<'PY'
import sys; sys.path.insert(0,'src')
from google.cloud import firestore
from forge_common import layout
import os
layout.clock_ref(firestore.Client(project=os.environ['PROJECT_ID'])).set({'logical_time': 0})
print('logical clock reset to Day 0')
PY

# 2) start the identity-token forwarder (the browser's approval clicks need
#    a bearer the APP can see; the gcloud proxy only satisfies the platform)
PROJECT_ID=$PROJECT_ID python scripts/../../path-to/auth_forwarder.py 8090 &
```

The forwarder script lives with the session scratchpad; keep a copy beside
the repo if recording later. Browser to `http://localhost:8090/`.

**Framing rules (from review):** record the browser CONTENT AREA only —
crop all browser chrome so no `localhost` URL is visible. Prove deployment
with the Cloud Run console tab, not the address bar. Read the veto's "why"
from the approval card's FACTS/RULES rows; show trace evidence on a
separate Cloud Trace tab (the dashboard intentionally carries no trace
link). In the Agent Catalog, point at the STATE column (IDLE / RESERVE /
ACTIVE / FAILED) — health badges derive from live heartbeats and read
HEALTHY when the fleet is up.

Have two browser tabs ready (optional but strong on camera): the Cloud Run
services list, and a Cloud Trace waterfall you can refresh after Scene 2.

## Scene 1 — a poisoned document is caught (≈60 s)

```bash
PROJECT_ID=$PROJECT_ID ./.venv/bin/python -u scripts/demo_scene1_live.py
```

Narrate, in order, from the live output:
1. The vendor bulletin is POSTed to the **deployed** Cyber Trust service and
   quarantined first.
2. **`model_armor=clean`** — the diluted injection *evades* the inline filter.
3. **`classifier=malicious`, candidate `VND-ACT-9901`** — the second,
   tool-less layer catches it.
4. **`Safety veto … SP-SEC-004, SP-PART-001`** — Safety rejects on the trusted
   registry, never on the document.
5. "**SEC-4 verified: the raw bulletin never left quarantine.**"

One line to say: *"Neither guardrail alone was enough. The two in series
were — that's defense in depth, and we caught Model Armor missing the diluted
injection live."*

## Scene 2 — full recovery, NMC → RELEASED (≈70 s)

```bash
PROJECT_ID=$PROJECT_ID ./.venv/bin/python -u scripts/demo_spine_live.py
```

Narrate the printed state line as it advances: INTAKE → PLANNING → VALIDATING
→ **AWAITING_SCHEDULE_APPROVAL** (point out the recorded approver identity) →
SUSPENDED_AWAITING_PART → *(21 simulated days pass — the Logical Clock)* →
ASSEMBLY_RESUMED → **AWAITING_RELEASE_APPROVAL** → **RELEASED**. Call out
"one trace across N audit events" and the Cloud Trace link it prints.

Optional 10-second closer: refresh the Cloud Trace tab on that trace id to
show one waterfall spanning all services (metadata only — no prompts).

## What to claim, precisely

- Gemini 3.5 Flash on Vertex, Google ADK, seven Cloud Run services — all live.
- Model Armor **missed the diluted injection**; the Cyber Trust classifier
  caught it (defense in depth). Do not claim Model Armor blocked it.
- Human approvals are recorded from the **authenticated principal**, not
  client input.
- All data synthetic; no Assured Workloads / IL4 claim.

## After capture (only then)

Per the freeze directive, leave infrastructure untouched until the video is
captured and verified. Afterward, to trim idle spend:

```bash
gcloud scheduler jobs pause forge-tick --location us-central1 --project $PROJECT_ID
# prune old Artifact Registry images, keeping the current one
```

Leave `forge-dashboard` (and the fleet, if cost is negligible) deployed
through judging so the hosted surface can be shown on request.
