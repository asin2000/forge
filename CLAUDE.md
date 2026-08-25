# FORGE — build session bootstrap

## Read first, every session (DFT-2)

`FORGE-REQUIREMENTS.md` **v1.3** (repo root; v1.2 ratified 2026-08-21, v1.3
operator-console amendment 2026-08-23 per entrant directive) is the
**governing baseline**. If code and that document disagree, the document wins
until formally amended (version bump + Change Log line). Decision rationale:
`docs/decisions.md`. Current tag, test counts, and acceptance evidence:
README → *Acceptance & evidence* (kept pinned; do not trust this file for
those numbers — it is deliberately not a status diary). Work not traceable to
a requirement ID is drift — it gets rejected in review, so don't write it.

## Non-negotiables

- **PRs only.** Never commit or push to `main` directly. Post-freeze (DFT-4),
  only demo-spine defects and entrant-directed amendments merge — and **any
  code change resets the acceptance count**: re-tag (`v1.3-demo.N` series)
  and re-run the CI-8 10/10 live acceptance before anything is recorded.
- **The Logical Clock is monotonic. NEVER reset it.** Viewer-facing day
  counts are relative per recovery; the absolute sim day is operator-side
  bookkeeping. (A backward reset once stranded a live workflow 357 sim-days
  from its part — entrant defect, 2026-08-25.)
- **New dependency ⇒ one-line justification** in the PR description (DFT-3).
- **Merged contract schemas are immutable** (ICD-3). Shape change ⇒ new
  `<type>.v<N+1>.schema.json` + example. `scripts/check_schema_versions.py`
  enforces this. The live state schema is `workflow_state.v3` (adds the
  terminal `CANCELLED`); `layout._STATE_SCHEMA_PATH` pins it.
- **Every production module maps to a requirement ID** in
  `requirements/traceability.yaml` (DFT-1, strict). `pending` survives only
  as a **dated** exemption with a reason; expired exemptions fail the gate.
- **Boundary rules that must never regress:** Orchestrator does no domain
  work (ORC-2); Safety vetoes are not human-overridable (AGT-4) and a veto's
  WHY is recorded verbatim in the audit detail; raw quarantined documents
  never reach the bus — verdict + typed safe metadata only (SEC-4); no
  raw-model-output-to-tool path, retries ≤ 2 then failure event (AGT-7);
  approvals AND HUM-3 operator actions derive identity from the
  authenticated principal, never client input (HUM-1/HUM-3).
- **Terminal-state discipline:** `state.TERMINAL_STATES` = RELEASED +
  CANCELLED. Late bus traffic for a finished workflow drains as audited
  stale no-ops BEFORE any model call — never an illegal-edge 500 to the DLQ.
- **Registry truth:** instances are ACTIVE only while they own assigned
  work — a COMPLETED package returns its owner to IDLE in the same
  transaction (audited `AGENT_DEACTIVATED`); a FAILED owner's late result is
  REFUSED at commit (audited `RESULT_REFUSED_OWNER_FAILED`) and the package
  is left for the monitor's normal disposition. The console permits ONE
  active recovery per vehicle (`create_workflow(..., exclusive=True)`).
- **All data synthetic** (SUB-5); entrant-owned work only (SUB-4/SUB-6,
  `PREEXISTING_WORK.md`). Trust gate DAT-2; registry/discovery REG-2/3 with
  IAM enforcement AGT-6; spans metadata-only OBS-2; residency per
  `infra/residency.yaml` (DAT-1, CI-9-validated).
- **Console honesty:** every pixel derives from governed state — audit
  events, claims, work packages, registry. No fabricated failure events, no
  prompts/CoT/document text, no operator emails in the DOM, banners fire only
  on newly observed audit events.

## Environment & commands

Python 3.11+ (3.13 verified) · `gemini-3.5-flash` on Vertex AI at the `us`
multi-region (DAT-1) · `google-adk` pinned in `requirements.lock`.

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.lock && pip install -e . --no-deps

    python scripts/validate_contracts.py                 # CI-2
    python scripts/check_schema_versions.py origin/main  # ICD-3
    python scripts/check_traceability.py                 # DFT-1 (strict)
    python scripts/check_config.py                       # CI-9
    ruff check . && ruff format --check .                # CI-1
    pytest                                               # CI-3 + emulator via env
    vulture                                              # CI-5

Run all of these green **before** opening any PR. Emulator gate locally:
Firestore on 8925 / Pub/Sub on 8926 (Java 21), then
`FIRESTORE_EMULATOR_HOST=127.0.0.1:8925 PUBSUB_EMULATOR_HOST=127.0.0.1:8926
pytest tests/emulator`. Live acceptance: `scripts/acceptance_run.sh 10` +
`scripts/dress_rehearsal.sh` against the deployed fleet
(`PROJECT_ID=$(cat .gcp-project)`); verbatim records land in
`docs/verification/`. CI-6 auto-deploy: `.github/workflows/deploy.yml` (WIF,
no SA keys) on version tags or manual dispatch, followed by the live smoke.

## Operational practices (learned, do not relearn)

- The entrant browses the deployed console via the local auth forwarder
  (`~/dev/forge-tools/auth_forwarder.py`, port 8090). **Leave it running.**
- The entrant's own live workflows are his; never clean them up silently.
- Recording precondition is a CLEAN workflow list (cancel/delete residue),
  never a clock reset. The demo reads per-recovery days.
- Every merged change: PR through all 8 gates → re-tag → redeploy → fresh
  10/10 + rehearsal → evidence doc in `docs/verification/` → pin README.

## Key design decisions (details in docs/decisions.md)

Bus messages are `{"envelope": ..., "payload": ...}`; envelope v2 carries the
eight ICD-4 fields incl. `data_origin`/`trust_state` (DAT-2) and a `trace_id`
mirroring the 32-hex OTel trace (OBS-1); each schema const-pins its
`schema_version`. Active bus contracts: the `.v2` set plus
`sourcing_report.v3` and `quarantine_verdict.v3`; internal state is
`workflow_state.v3` (not a bus message). Logical Clock time is an integer day
counter; audits carry `observed_at` + `effective_at` (AUD-3). SEC-3
classifier runs on `gemini-3.5-flash` (Gemma route: NO-GO on Day-6 live
probes, forfeited per PLT-5).

## Where we are

Do not encode status here — it drifts. Current truth lives in:
`git log --oneline` · README *Acceptance & evidence* (tag + counts) ·
`docs/verification/` (dated live evidence) · `docs/decisions.md` (rationale)
· open items in `docs/SUBMISSION.md`. Deadline: **Aug 31, 2026, 5:00 PM PT**.
