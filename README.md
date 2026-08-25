# FORGE — Fleet Operational Readiness & Governed Execution

Multi-agent system coordinating recovery of non-mission-capable (NMC) support
equipment for a **fictional** installation operating twelve GX-12 Ground
Support Vehicles. Entry for the All Things Agentic Hackathon, **Fortified
Enterprise Fleet** track.

**Entry basis:** solo individual entry by Dr. Armond E. Sinclair. "Anansi
Labs" appears only as the entrant's professional affiliation; this is not a
company or team entry, no Anansi Labs materials are included (see
`PREEXISTING_WORK.md`), and any prize is payable to the entrant personally.

**Governing baseline:** `FORGE-REQUIREMENTS.md` v1.3 (repo root; v1.2
ratified 2026-08-21, v1.3 operator-console amendment 2026-08-23 per entrant
directive). If code and that document disagree, the document wins. Schedule:
`FORGE-BUILD-PLAN.md` (repo root). AI-assisted sessions bootstrap from
`CLAUDE.md` (DFT-2). Every module traces to a requirement ID via
`requirements/traceability.yaml` (DFT-1).

## Clean statement (SUB-5)

This repository contains **no** CALM-CFR+ solver code, **no** controlled
technical information, **no** real platform data, **no** real technical
orders, and **no** actual military identifiers. All equipment, parts,
procedures, personnel, and vendor data are synthetic and fictional.

## Architecture (summary)

A Readiness Orchestrator (manager agent — performs no domain work) decomposes
NMC events into exclusively-owned work packages for five specialists:
Maintenance, Supply, Workforce, Safety & Policy, and Cyber Trust. Messages are
versioned JSON contracts (`/contracts`) validated at publish and subscribe.
State, transactional inbox/outbox, audit trail, and the Logical Clock live in
Firestore; the bus is Pub/Sub with per-workflow ordering keys. Each role
deploys as its own Cloud Run service with its own service account; boundaries
are enforced at IAM. External documents pass a quarantine-first pipeline
(bounded parser → Model Armor → tool-less classifier) and only a structured
verdict plus tightly-typed safe metadata ever reaches the bus. A hosted
operator console (read-only except the audited HUM-1 approvals and HUM-3
operator controls) lets an authenticated operator start and cancel
workflows, advance the Logical Clock, and inject anomalies — the poisoned
bulletin through the live quarantine path, and audited agent-instance
failure/restore — with a fleet-readiness strip (per-vehicle status derived
from workflow state alone), a per-agent Agent Operations dock whose lanes
render live claims, assignments, and audit events — nothing the trail
cannot prove — and a fleet-wide activity feed, so a judge can drive the
running system and watch every agent work, not just take the video's word. Reasoning:
`gemini-3.5-flash` on Vertex AI at the DAT-1 map's `us` multi-region
(everything else pins `us-central1`; see Data residency below).

**Security result (defense in depth), observed live.** In the demonstration
scene a poisoned vendor bulletin carries a *diluted* prompt injection.
**Model Armor returned `clean`** on the full bulletin — the dilution evades
the inline filter — but the **second layer, a tool-less Cyber Trust
classifier, caught it** (`malicious`, candidate `VND-ACT-9901`), Safety then
vetoed on the trusted registry (`SP-SEC-004`, `SP-PART-001`), and the raw
document never left quarantine. Neither layer alone is sufficient; the two in
series are. This is the headline finding and is reproduced in
`scripts/demo_scene1_live.py` against the deployed Cyber Trust service.

## Repository layout

    contracts/        versioned message schemas + examples (ICD-1..4)
    src/forge_common/ shared bus, state, audit, clock, OTel, contracts (ICD-2, OBS)
    services/         agent roles + dashboard; worker.py = Cloud Run push runtime
    prompts/          versioned prompt files (PLT-1)
    requirements/     traceability.yaml (DFT-1, strict)
    architecture/     manifest.yaml — service → module → diagram node (DFT-5)
    scripts/          CI gates, live smokes, acceptance + dress-rehearsal harness
    infra/            setup-gcp.sh + deploy.sh (one-command PLT-6) + Dockerfile
    docs/verification/ verbatim live-run evidence (Days 5-8)
    tests/            unit + contract + real-emulator integration (CI-3/CI-4)

## Spin-up

Local (no cloud needed for Lane-1 gates):

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.lock && pip install -e . --no-deps
    python scripts/validate_contracts.py
    python scripts/check_traceability.py
    ruff check . && pytest

Full environment stand-up is ONE command (PLT-6) — it chains the API/
Firestore bootstrap, per-role service accounts and scoped IAM, the bus with
its dead-letter queue, the regional log bucket, the registry load, one
container build, six agent services (five reasoning agents + Cyber Trust)
plus the dashboard on Cloud Run (`--no-allow-unauthenticated`), filtered
push subscriptions, and the per-minute heartbeat:

    PROJECT_ID=<id> PYTHON=./.venv/bin/python bash infra/deploy.sh

Prerequisites: authenticated principal WITH Application Default Credentials
(`gcloud auth application-default login`), billing enabled, CWD = repo
root, and the local spin-up above (the deploy uses your interpreter for the
registry load and bus provisioning).

## Data residency & disclosures (DAT-1, DAT-3)

Residency jurisdiction is `US`, governed by the approved location map in
`infra/residency.yaml` and CI-validated on every PR (CI-9): Cloud Run,
Firestore, Pub/Sub (regional endpoint, `allowed_persistence_regions`,
`enforce_in_transit: true`), quarantine storage, Model Armor, and Cloud
Logging (operational logs in the regional `forge-logs` bucket; the
`_Default` sink is redirected there) all pin `us-central1`; Gemini runs in
the `us` multi-region per the map.

Firestore IAM scoping limitation (AGT-6, stated rather than pretended
away): `roles/datastore.user` is project-scoped — Firestore has no
per-collection IAM primitive. Compensating controls: state changes flow
through single sanctioned writer paths (`state.apply_transition`,
`state.record_approval_decision`), work products commit under transactional
ownership guards, and every mutation is audited (AUD-1); Pub/Sub, storage,
Vertex, and Model Armor are scoped per role/resource.

Disclosed exceptions: **Cloud Trace** receives metadata-only telemetry
(span attributes carry envelope routing fields and outcomes — never
prompts, model responses, decision-record prose, or document content; see
`src/forge_common/otel.py` and its OBS-2 tests). The **`_Required`** log
bucket predates configuration (created with the project, location fixed)
and holds only Google admin-activity audit entries, never operational
payload data. FORGE claims no Assured Workloads or IL4 compliance. All
data is synthetic (SUB-5).

## Acceptance & evidence

Frozen at tag **`v1.3-demo.4`** (DFT-4; commit `6eddd34`; series began
at `v1.3-demo` = `7e19d47`). The v1.3
operator-console amendment (HUM-3) reset the acceptance count per the
entrant's rule, and the full live acceptance re-ran on the new tag
(baseline history: `v1.2-demo` … `v1.2-demo.3`). CI enforces eight gates on
every PR (lint, contracts+ICD-3, unit, real-emulator integration, secrets,
dead-code, config/registry+residency, traceability); the suite is **312
tests (291 unit + 21 real-client emulator)** and traceability runs in
strict mode.

Live acceptance on the frozen commit, verbatim captures in
`docs/verification/`:

- **CI-8 — 10/10 consecutive** live spines, NMC → RELEASED, real
  `gemini-3.5-flash` reasoning with two human-gated approvals each — run
  fresh on BOTH v1.3 tags, first attempt, zero resets each time
  (`scripts/acceptance_run.sh`; records in
  `docs/verification/2026-08-23-v13-acceptance.md`,
  `docs/verification/2026-08-24-day10-console.md`, and
  `docs/verification/2026-08-24-v134-presentation.md`; the `v1.2-demo` series
  record remains at `docs/verification/2026-08-22-acceptance.md`).
- **Dress rehearsal** (Scene 1 veto + full spine) in 69 s, under the
  four-minute budget (same record).
- **HUM-3 console rehearsal** on the deployed fleet: start → bulletin
  injection → both approvals → 21-day clock advance → RELEASED, plus
  cancel (terminal, inert under a live tick) and instance fail/restore
  (reserve topology preserved) — every action recorded with the
  authenticated operator identity (same record).
- **Clean-project deploy**: `deploy.sh` stood up the whole environment on a
  fresh billed project (then deleted) — the PLT-6 criterion
  (`2026-08-21-day7-live.md` §9).
- **Three security proofs** (`2026-08-21-day7-live.md` §6–§8): the
  defense-in-depth screening result above; an IAM negative-test matrix where
  each identity is denied a prohibited operation *by Google IAM*
  (`scripts/iam_matrix_live.py`, 6/6); and live dead-letter forwarding of a
  poison message.

Traceability and the service map: `requirements/traceability.yaml` (DFT-1,
strict) and `architecture/manifest.yaml` (DFT-5). The CI-4 assertion list is
in `.github/workflows/pr-gate.yml`.

## Process rules

PRs only — no direct commits to `main` (branch protection). New dependencies
need a one-line justification in the PR description (DFT-3). Work not
traceable to a requirement ID is rejected in review (DFT-2). After the Day-8
freeze, only demo-spine defects merge (DFT-4); the v1.3 amendment is the one
entrant-directed scope change since, and it resets the acceptance count
(fresh CI-8 10/10 before recording).
