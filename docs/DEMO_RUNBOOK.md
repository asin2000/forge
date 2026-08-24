# FORGE demo runbook — ≤4-minute video

Record against the **tagged runtime**. The deployed Cloud Run fleet on
`forge-agentic-0821-6418` is built from the acceptance-tagged commit
**`v1.3-demo` = `7e19d47`** (10/10 + 69 s rehearsal + console rehearsal:
`docs/verification/2026-08-23-v13-acceptance.md`) — you run everything from
`main`, whose runtime code is byte-identical to the tag. Do not modify
runtime code or touch the infrastructure during capture.

With HUM-3 the demo is **console-driven**: every beat happens as clicks in
the operator console, on camera, against the live fleet. The script drivers
(`scripts/demo_scene1_live.py`, `scripts/demo_spine_live.py`) remain the
fallback and the acceptance harness.

## Before you hit record

```bash
cd ~/dev/forge
export PROJECT_ID=$(cat .gcp-project)
gcloud auth application-default print-access-token >/dev/null   # ADC warm
# sanity: the deployed fleet is the frozen runtime
gcloud run services list --project "$PROJECT_ID" --region us-central1 \
  --format="value(metadata.name,status.conditions[0].status)"

# 1) reset the synthetic logical clock so the demo reads Day 0 -> Day 21
PROJECT_ID=$PROJECT_ID python - <<'PY'
import sys; sys.path.insert(0,'src')
from google.cloud import firestore
from forge_common import layout
import os
layout.clock_ref(firestore.Client(project=os.environ['PROJECT_ID'])).set({'logical_time': 0})
print('logical clock reset to Day 0')
PY

# 2) start the identity-token forwarder (browser clicks need a bearer the
#    APP can see; the gcloud proxy only satisfies the platform)
PROJECT_ID=$PROJECT_ID python ~/dev/forge-tools/auth_forwarder.py 8090 &
```

Browser to `http://localhost:8090/`. The header shows the logical Day and
your authenticated operator identity.

**Framing rules (from review):** record the browser CONTENT AREA only —
crop all browser chrome so no `localhost` URL is visible. Prove deployment
with the Cloud Run console tab, not the address bar. Read the veto's "why"
from the approval card's FACTS/RULES rows; show trace evidence on a
separate Cloud Trace tab (the dashboard intentionally carries no trace
link). In the Agent Catalog, point at the STATE column (IDLE / RESERVE /
ACTIVE / FAILED) — health badges derive from live heartbeats and read
HEALTHY when the fleet is up.

Have two extra browser tabs ready: the Cloud Run services list, and a Cloud
Trace waterfall you can refresh after the spine completes.

## Console-driven flow (primary)

1. **Start** — in Operator Controls, pick the equipment, keep
   `DSC-0042`, click **Report NMC**. Narrate: *"That click injected the
   same NMC event the flightline would emit — nothing scripted."* Then
   point at the **Agent Operations wall**: the orchestrator's lane pulses
   first (decomposing), then maintenance and supply light up *in
   parallel*, then safety — the org chart executing live. One line to say:
   *"Each lane renders only governed state — claims, work packages, the
   audit trail. The wall can't show anything the audit trail can't
   prove."* The Live Agent Activity feed below carries the merged stream
   (click a lane to filter it to that agent).
2. **Inject the poisoned bulletin** — open the workflow, click **Inject
   poisoned bulletin**. Narrate the audit trail rows: DOCUMENT_QUARANTINED
   first (quarantine-first, SEC-1), then the screening verdict. Say it
   precisely: *"Model Armor returned clean — the diluted injection evades
   the inline filter — and the second, tool-less classifier caught it.
   Neither layer alone was enough; the two in series were. The raw document
   never left quarantine."* Safety's veto shows `SP-SEC-004`/`SP-PART-001`
   from the trusted registry, never the document.
3. **Approve gate 1** — the schedule_override card appears
   (AWAITING_SCHEDULE_APPROVAL). Read FACTS/RULES aloud, click
   **Approve** — your authenticated identity lands in the audit row.
4. **Advance the clock** — 21 days, click **Advance**. The Day chip jumps,
   the due event fires, the workflow resumes unattended:
   PART_ETA_REACHED → ASSEMBLY_RESUMED → AWAITING_RELEASE_APPROVAL.
5. **Approve release** — second HUM-1 gate → RELEASED. Point at the audit
   trail: every hop, one trace, reconstructed from Firestore alone.
6. *(Optional anomaly beat, if time allows)* — click **Fail** on
   `agent-workforce-01` in the catalog (audited induction; the tag flips to
   FAILED), **Report NMC** again: the plan blocks with an audited
   NO_CAPABLE_AGENT escalation — the registry gates real work. **Cancel**
   the blocked workflow (audited terminal CANCELLED), **Restore** the
   instance.
7. Closer: refresh the Cloud Trace tab on the workflow's trace id — one
   waterfall spanning all services, metadata only.

## Script-driven flow (fallback / acceptance harness)

```bash
PROJECT_ID=$PROJECT_ID ./.venv/bin/python -u scripts/demo_scene1_live.py
PROJECT_ID=$PROJECT_ID ./.venv/bin/python -u scripts/demo_spine_live.py
```

Same narration beats; the spine driver prints the state line as it
advances and performs the approvals through the deployed dashboard API.

## What to claim, precisely

- Gemini 3.5 Flash on Vertex, Google ADK, seven Cloud Run services — all live.
- Model Armor **missed the diluted injection**; the Cyber Trust classifier
  caught it (defense in depth). Do not claim Model Armor blocked it.
- Human approvals AND operator actions are recorded from the
  **authenticated principal**, not client input.
- Operator failure induction marks the registry; it never fabricates
  failure events — detection stays with the monitor and the agents.
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
