# FORGE — build session bootstrap

## Read first, every session (DFT-2)

`FORGE-REQUIREMENTS.md` v1.2 (repo root, ratified 2026-08-21) is the
**governing baseline**.
Scope is FROZEN. If code and that document disagree, the document wins until
formally amended (version bump + Change Log line). Schedule and calendar
gates: `FORGE-BUILD-PLAN.md` (repo root). Decision rationale log:
`docs/decisions.md`. Work not traceable to a requirement ID is drift — it
gets rejected in review, so don't write it.

## Non-negotiables

- **PRs only.** Never commit or push to `main` directly. After the Day-8
  freeze (Fri Aug 28 EOD, tag `v1.2-demo`), only demo-spine defects merge (DFT-4).
- **New dependency ⇒ one-line justification** in the PR description (DFT-3).
- **Merged contract schemas are immutable** (ICD-3). Shape change ⇒ new
  `<type>.v<N+1>.schema.json` file. `scripts/check_schema_versions.py` enforces this.
- **Every production module maps to a requirement ID** in
  `requirements/traceability.yaml` or the gate fails (DFT-1). `strict: false`
  flips to `true` on Day 7 (Thu Aug 27); from then, no `pending` left.
- **Boundary rules that must never regress:** Orchestrator does no domain work
  (ORC-2); Safety vetoes are not human-overridable (AGT-4); raw quarantined
  documents never reach the bus — verdict + typed safe metadata only (SEC-4);
  no raw-model-output-to-tool path, retries ≤ 2 then failure event (AGT-7);
  approvals carry an authenticated `approver_identity` (HUM-1).
- **All data synthetic.** No CALM-CFR+ code, no controlled information, no real
  platforms, technical orders, or military identifiers (SUB-5). No Anansi Labs
  code or company-owned materials — entrant-owned work only (SUB-6);
  disclosures live in `PREEXISTING_WORK.md` (SUB-4).
- **Trust gate (DAT-2):** every envelope carries `data_origin` + `trust_state`;
  only Cyber Trust may consume non-`TRUSTED` messages — everyone else rejects
  at subscribe, audited. A safe verdict publishes as `TRUSTED` while the
  bulletin stays `QUARANTINED`.
- **Registry (REG):** `agents/registry.yaml` = declarative definitions
  (DRAFT/APPROVED/DEPRECATED/RETIRED); runtime instances
  (IDLE/RESERVE/ACTIVE/FAILED) and derived health live in Firestore. Work goes
  only to instances of APPROVED definitions; discovery by capability, never
  hard-coded addresses (REG-2/3); authorization still fails at IAM (AGT-6).
- **Spans are metadata-only (OBS-2):** audit event IDs, agent IDs, schema
  versions, statuses, latency. Never prompts, responses, decision-record
  prose, document text, or quarantined content.
- **Residency (DAT-1):** `infra/residency.yaml` is the approved location map
  (jurisdiction US; Gemini `us`, everything else `us-central1`). CI-9
  validates deploy config against it. Cloud Trace is a disclosed
  metadata-only exception (DAT-3).

## Environment & commands

Python 3.11+ (3.13 works; lockfile verified) · model `gemini-3.5-flash` on
Vertex AI at the `us` multi-region per `infra/residency.yaml` (DAT-1) ·
`google-adk` pinned in `requirements.lock` (PLT-1..3).

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.lock && pip install -e . --no-deps

    python scripts/validate_contracts.py            # CI-2 contract gate
    python scripts/check_schema_versions.py origin/main  # ICD-3 immutability
    python scripts/check_traceability.py            # DFT-1 gate
    ruff check . && ruff format --check .           # CI-1
    pytest                                          # CI-3 (stubbed models only)
    vulture                                         # CI-5 (Day 7 in CI)

Run all of these green **before** opening any PR. GCP bootstrap:
`PROJECT_ID=<id> ./infra/setup-gcp.sh`, then `scripts/smoke_vertex.py` and
`scripts/smoke_model_armor.py` (Day-1 risk checks R2/R5).

## Key design decisions (details in docs/decisions.md)

Bus messages are `{"envelope": ..., "payload": ...}`; the envelope (v2) carries
the eight mandatory ICD-4 fields — including `data_origin`/`trust_state`
(DAT-2) and a `trace_id` that is a read-only mirror of the 32-hex OTel trace
ID (OBS-1) — and each schema const-pins its own `schema_version`, so a message
cannot validate against the wrong contract. Active bus contracts are the `.v2`
set; `.v1` files are immutable history (ICD-3).
`work_package_id: "wp-none"` for workflow-scoped messages. Logical Clock time
is an integer day counter (`logical_time`, `due_at`, `effective_at`); audit
events always carry `observed_at` (real) + `effective_at` (logical) (AUD-3).
`contracts/state/workflow_state.v1.schema.json` is internal Firestore state,
deliberately not a bus message type. SEC-3 classifier runs on
`gemini-3.5-flash` first; Gemma is a parallel bonus attempt with a hard
go/no-go Wed Aug 26 EOD (PLT-5).

## Where we are

Check `git log --oneline` against the calendar in `FORGE-BUILD-PLAN.md` §2.
Day 1 (Fri Aug 21) is complete: contracts, validation harness, CI Lane 1,
traceability, GCP bootstrap scripts — plus the **v1.2 baseline landing**
(envelope/messages at v2, `agents/registry.yaml`, `infra/residency.yaml`,
`PREEXISTING_WORK.md`, traceability extended with REG/OBS/DAT/CI-9/SUB-8).
Still Day 1: GCP bootstrap + Vertex/Model Armor smoke calls (R2). Day 2+ must
also build: the registry loader + Firestore instance records (REG-1/2), OTel
init + traceparent propagation (OBS-1), and the CI-9 config gate.
**Day 2 (Sat Aug 22):** Firestore
workflow state (ORC-5), transactional inbox/outbox + ordering keys + DLQ
(ICD-5), append-only audit writer with dual timestamps committed in one
transaction with the state change (AUD-1..3), Logical Clock + idempotent due
events, Firestore/Pub/Sub emulator harness, duplicate-delivery fault-injection
test (ICD-6). Exit: double-fire test passes; a toy workflow's audit trail
reconstructs from Firestore alone.
