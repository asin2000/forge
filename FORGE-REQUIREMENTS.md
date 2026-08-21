# FORGE Requirements Specification
**Fleet Operational Readiness & Governed Execution**
Version 1.1.2 · Baseline for All Things Agentic Hackathon submission
Entrant: Anansi Labs · Category: Fortified Enterprise Fleet

This document is the governing baseline. All build sessions, human or AI-assisted, conform to it. Changes require a version bump and a one-line rationale in the Change Log. If code and this document disagree, this document wins until formally amended. Scope is FROZEN at v1.1.2. This is the build baseline.

---

## 1. Scope and Design Basis

FORGE is a multi-agent system that coordinates the recovery of non-mission-capable (NMC) support equipment for a fictional installation operating twelve GX-12 Ground Support Vehicles. All data is synthetic. No controlled technical information, no real platforms, no real technical orders, no actual military identifiers, no Anansi CALM-CFR+ solver code.

Design basis: the Agentic Conway Effect pilot (Sinclair 2026, v2.2). The architecture applies the managed-arm findings: exclusive ownership, pre-written interface contracts, manager-held reserves, monitoring, and a targeted repair channel. The Readiness Orchestrator is the manager agent; it performs no domain work.

## 2. Definitions

- **Work package**: a bounded task created and assigned by the Orchestrator to exactly one specialist agent.
- **Domain output**: the artifact a specialist produces in response to its work package (action plan, sourcing report, roster assignment, validation verdict, screening verdict).
- **Contract**: the versioned JSON schema governing a message type between two agents.
- **Reserve worker**: a fresh agent session/invocation of the same specialist role with empty conversational state (container reuse permitted), held unassigned until deployed by the Orchestrator.
- **Monitoring cycle**: 5 seconds in the demo environment.
- **Demo spine**: the frozen end-to-end scenario recorded in the submission video (Section 8).
- **Logical Clock**: a persisted Firestore document representing simulated time. System time is never modified.
- **Drift**: any code, feature, or dependency not traceable to a requirement in this document.

## 3. Functional Requirements

### 3.1 Orchestration (ORC)

- **ORC-1**: The Readiness Orchestrator SHALL decompose each NMC event into work packages with exclusive agent ownership. *Verify: inspection of assignment log.*
- **ORC-2**: The Orchestrator SHALL NOT execute domain work (diagnosis, parts search, scheduling, screening). *Verify: audit log shows zero domain tool calls by Orchestrator across acceptance runs.*
- **ORC-3**: The Orchestrator SHALL hold at least one reserve worker for the Workforce role (the demo's designated critical role; other roles MAY have reserves) and deploy reserves only on failure detection. Reassignment SHALL atomically transfer work-package ownership and emit an audit event. *Verify: repair-loop scene + audit inspection.*
- **ORC-4**: The Orchestrator SHALL detect any specialist failure (timeout, malformed output after retry exhaustion, contract violation) within one monitoring cycle. For Workforce failures, it SHALL issue targeted reassignment to the held reserve. For other specialist failures, it SHALL place the affected workflow in `BLOCKED_AGENT_FAILURE` and emit an audited escalation, unless an optional same-role reserve is configured. *Verify: fault-injection test, 10/10 consecutive passes in acceptance lane.*
- **ORC-5**: Workflow state SHALL persist in Firestore keyed by `workflow_id` and SHALL include `logical_time` and any `due_at` value, such that a workflow suspended N simulated days resumes without human re-initiation. Advancing the Logical Clock creates a due event; processing of due events SHALL be idempotent. *Verify: time-skip test, including double-fire of the due event.*

### 3.2 Specialist Agents (AGT)

- **AGT-1**: Maintenance Agent SHALL produce a maintenance action plan in response to its assigned work package. It SHALL NOT release equipment.
- **AGT-2**: Supply Agent SHALL locate approved parts and track shipment status. It SHALL NOT approve substitutions or purchases.
- **AGT-3**: Workforce Agent SHALL assign only technicians whose qualification records match the task code. It SHALL NOT waive qualifications.
- **AGT-4**: Safety & Policy Agent SHALL validate every proposed action against the synthetic procedures library and SHALL veto noncompliant actions. A Safety veto SHALL NOT be human-overridable; humans may approve only otherwise-compliant actions.
- **AGT-5**: Cyber Trust Agent SHALL implement the quarantine-first pipeline (SEC-1..SEC-3). No other agent SHALL read an external document that has not been released from quarantine.
- **AGT-6**: Every agent SHALL operate only within its scoped toolset, enforced at the platform layer: each specialist role runs as its own Cloud Run service with its own service account; inter-service calls are IAM-authenticated with identity tokens. Cross-boundary tool calls SHALL fail at IAM, not by convention. *Verify: negative test per agent.*
- **AGT-7**: Every agent SHALL produce schema-constrained structured output. There SHALL be no direct raw-model-output-to-tool path. Malformed output SHALL be retried at most 2 times, then emit a failure event.

### 3.3 Security & Ingestion (SEC)

- **SEC-1**: External documents SHALL enter quarantine storage first. A bounded parser, an explicit Model Armor screening call, and a tool-less structured classifier SHALL all complete before anything derived from the document is published to the agent bus.
- **SEC-2**: Any parsing, screening, or availability error SHALL leave the document quarantined (fail closed).
- **SEC-3**: The classifier SHALL have no tools and SHALL NOT authorize execution. (Gemma SHOULD serve as the classifier — see PLT-5.)
- **SEC-4**: The raw document SHALL never be published to the agent bus. Cyber Trust publishes only a non-executable structured quarantine verdict plus safe extracted metadata (e.g., the externally supplied candidate-part identifier). Downstream agents evaluate identifiers against trusted registries, never the source document.

### 3.4 Human Gate (HUM)

- **HUM-1**: No equipment release, substitution approval, or schedule override SHALL execute without explicit human approval recorded in the audit trail with `approver_identity`, action, decision, and timestamps per AUD-3. The approval surface SHALL be protected by Cloud Run IAM or IAP; approver identity derives from the authenticated principal, not client input.
- **HUM-2**: The approval request SHALL present an inspectable decision record: source references, extracted facts, applicable rules, constraints, confidence, alternatives considered, the recommended action, and agent/model/schema versions. It SHALL NOT expose or claim private model chain-of-thought.

### 3.5 Audit (AUD)

- **AUD-1**: Every agent decision, blocked action, veto, approval, and state change SHALL emit an append-only structured audit event with timestamp, agent identity, triggering input hash, output hash, and reason code.
- **AUD-2**: Each state transition and its audit/outbox record SHALL commit in one Firestore transaction. The audit trail SHALL be reconstructable for any workflow from Firestore alone. The log is append-only; it is not claimed to be tamper-proof.
- **AUD-3**: Every audit event SHALL carry both `observed_at` (real system time) and `effective_at` (Logical Clock time), so reconstruction across simulated time jumps is unambiguous.

## 4. Interface Contracts & Messaging (ICD)

- **ICD-1**: All inter-agent messages SHALL conform to versioned JSON schemas stored in `/contracts`. Schemas are written before implementing either endpoint.
- **ICD-2**: Contract validation SHALL occur at both publish and subscribe. Nonconforming messages are rejected and audited, never silently coerced.
- **ICD-3**: Contract changes SHALL bump the schema version and pass CI before any dependent code merges.
- **ICD-4**: Every message SHALL carry `workflow_id`, `work_package_id`, `event_id`, `trace_id`, `idempotency_key`, and `schema_version`.
- **ICD-5**: Message consumption SHALL use a Firestore transactional inbox/outbox; side effects SHALL be idempotent under duplicate delivery. Pub/Sub ordering keys SHALL be set per workflow. Undeliverable messages route to a dead-letter topic after 5 attempts.
- **ICD-6**: CI SHALL include a duplicate-delivery fault-injection test. *Verify: CI-4.*

## 5. Platform Requirements (PLT)

- **PLT-1**: `gemini-3.5-flash` via Vertex AI, single pinned region, SHALL perform all Orchestrator and specialist reasoning except the optional SEC-3 classifier (PLT-5). Prompts are versioned files in `/prompts`. *Hackathon mandatory (Gemini 3.5+).*
- **PLT-2**: Google ADK, pinned version in lockfile. *Hackathon mandatory.*
- **PLT-3**: Cloud Run (one service per specialist role + Orchestrator), Firestore, Pub/Sub. *Hackathon mandatory (≥1 GCP service; we use three).*
- **PLT-4**: Model Armor in the SEC pipeline per SEC-1..SEC-2.
- **PLT-5**: Gemma SHOULD serve as the SEC-3 classifier. Decision gate: end of Day 6. If not integrated by then, a tool-less `gemini-3.5-flash` classifier satisfies SEC-3 and the bonus is forfeited.
- **PLT-6**: A single documented deployment command (`deploy.sh` or `terraform apply`) SHALL stand up the environment from a clean project, given documented prerequisites: authenticated principal, project ID, billing enabled, and CI trust bootstrap. *Verify: clean-project deployment test before recording.*

## 6. CI/CD Requirements (CI)

Pipeline: GitHub Actions. No direct commits to `main`; PRs only, including from AI-assisted sessions. Validation runs on every PR; deployment runs only on merge to `main`.

**Lane 1 — PR gate (deterministic, no live model calls):**
- **CI-1**: Lint & format (Ruff). Fail on violation.
- **CI-2**: Contract gate: all schemas validate; all examples in `/contracts/examples` validate against their schemas.
- **CI-3**: Unit gate: per-agent tests, minimum one happy path and one boundary-violation rejection per agent, using recorded/stubbed model responses.
- **CI-4**: Spine gate: demo spine end to end against Firestore + Pub/Sub emulators with stubbed model responses. This is the drift tripwire. Explicit assertions:
  - The gated transition is blocked before approval and succeeds after approval (HUM-1).
  - Quarantined raw content never reaches any agent other than Cyber Trust (SEC-4).
  - Duplicate event delivery produces no duplicate side effects (ICD-5/ICD-6).
  - Repair-loop reassignment transfers ownership exactly once (ORC-3/ORC-4).
  - The final workflow state and full audit trail reconstruct from Firestore alone (AUD-2).
- **CI-5**: Dead-code gate: zero high-confidence findings (Vulture) outside a reviewed allowlist.
- **CI-7**: Secrets scanning. Fail closed.

**Lane 2 — Staging gate (on merge to `main`):**
- **CI-6**: Auto-deploy to Cloud Run via Workload Identity Federation (no SA keys in repo), then live-Gemini smoke test of one agent round-trip and one Pub/Sub publish/consume.

**Lane 3 — Acceptance gate (manual trigger, pre-recording):**
- **CI-8**: 10 consecutive clean runs of the full spine on deployed infrastructure with live models. Required before the final video.

## 7. Drift Prevention (DFT)

- **DFT-1**: `requirements/traceability.yaml` SHALL map every requirement ID via `implemented_by`, `verified_by`, and `verification_method: test | inspection | demonstration | document`. CI enforces: every requirement mapped, every production module referenced by at least one requirement.
- **DFT-2**: AI-assisted sessions SHALL be pointed at this document at session start. Proposed work outside these requirements is rejected in review, not merged and cleaned up later.
- **DFT-3**: New dependencies require a one-line justification in the PR description.
- **DFT-4**: After the dress-rehearsal recording (Day 8), `main` is frozen except for defects on the demo spine. Tag `v1.1-demo` at freeze.
- **DFT-5**: `architecture/manifest.yaml` SHALL map each deployed service to its source modules and to its node in the architecture diagram. CI checks the manifest against the deploy config.

## 8. Demo Spine (Frozen Scenario)

1. Discrepancy ingested: GX-12 #7 NMC, failed hydraulic actuator.
2. Orchestrator decomposes; Maintenance Agent produces action plan; Supply Agent reports approved part delayed 10 days.
3. Synthetic vendor bulletin arrives recommending unauthorized substitute; contains embedded prompt-injection attempt.
4. Bulletin enters quarantine; classifier + Model Armor screen it; quarantine verdict and candidate-part identifier published per SEC-4; Safety Agent rejects the identifier against the approved-parts registry. Raw bulletin never leaves quarantine. **(Scene 1: fail-closed quarantine)**
5. Fault injection: Workforce Agent returns malformed assignment past retry limit. Orchestrator detects within one monitoring cycle, atomically reassigns to reserve. **(Scene 2: repair loop)**
6. Compliant recovery plan assembled; human approval of the 10-day schedule override requested with decision record; approved by authenticated approver via dashboard control. **(Scene 3: human gate)**
7. Logical Clock advances 10 days; due event fires (and is double-fired in test); part arrives; workflow resumes unattended and halts at `AWAITING_RELEASE_APPROVAL`. Equipment release requires a second HUM-1 approval; the workflow never reaches `RELEASED` unattended. **(Scene 4: time-skip)**
8. Audit trail rendered from Firestore alone: every decision, veto, block, and approval, with `observed_at`/`effective_at` distinguishing the time jump. **(Closing shot)**

Acceptance: CI-8 passed before final recording.

## 9. Submission Requirements (SUB)

- **SUB-1**: Public demo video ≤ 4 minutes on YouTube/Vimeo, English, showing live execution and visible Google Cloud Console proof of deployment.
- **SUB-2**: Architecture diagram consistent with `architecture/manifest.yaml`.
- **SUB-3**: Public (or judge-accessible) repo with reproducible README spin-up instructions; judge access procedure documented if any surface is private.
- **SUB-4**: `PREEXISTING_WORK.md` disclosing: the Agentic Conway Effect pilot (Sinclair 2026) as conceptual design input; AI coding assistants used during the Submission Period; any reused assets (fonts, icons, libraries) with licenses.
- **SUB-5**: A clean statement in the README: no CALM-CFR+ code, no controlled information, no real platform data, no real technical orders, no actual military identifiers.
- **SUB-6**: Written authorization from Anansi Labs (both members) for the entry, the use of the research, and the grant of the hackathon's promotional license, obtained before submission. Building may proceed prior; submission may not.
- **SUB-7**: Devpost submission completed with margin before Aug 31, 5:00 PM PT.

## 10. Explicit Non-Requirements

OUT of scope: multi-vehicle concurrent recoveries, real supplier APIs, authentication/multi-tenancy beyond IAM scoping, mobile UI, configurable fleets, tamper-proof logging, and any CALM-CFR+ solver integration. The dashboard is read-only except for approve/reject controls (HUM-1).

## 11. Change Log

- v1.0 — Initial baseline.
- v1.1 — Review integration: resolved ORC-1/AGT-1 ownership conflict; defined reserve worker, monitoring cycle, Logical Clock; HUM-2 decision record replaces reasoning chain; Safety veto non-overridable; AGT-6 IAM architecture explicit (service-per-role); added SEC quarantine-first fail-closed pipeline; added ICD-4..6 messaging reliability; pinned model/region/versions (PLT-1); Gemma demoted to SHOULD with Day-6 gate; CI split into three lanes; DFT-1/DFT-5 machine-readable traceability and manifest; added SUB section; dashboard contradiction fixed. Scope frozen.
- v1.1.1 — Defect corrections, no scope change: SEC-4 verdict-only publication resolves quarantine contradiction; spine names the approved action (schedule override) and post-skip halt state (AWAITING_RELEASE_APPROVAL); ORC-5 keyed by workflow_id; reserve worker defined as fresh session, critical role = Workforce; PLT-1 Gemma exception; HUM-1 attributable approval behind IAM/IAP; AUD-3 dual timestamps; CI-4 explicit assertions; DFT-1 verification methods; scene renumbering; DFT-4 tag corrected. BUILD BASELINE.
- v1.1.2 — Failure disposition clarified (ORC-4: Workforce reassigns to reserve; other roles enter `BLOCKED_AGENT_FAILURE` with audited escalation); no scope change. Copyedits.
