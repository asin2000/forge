# FORGE Requirements Specification
**Fleet Operational Readiness & Governed Execution**
Version 1.2 · Baseline for All Things Agentic Hackathon submission (ratified 2026-08-21)
Entrant: Armond E. Sinclair — individual/solo entry; Anansi Labs is company affiliation only, disclosed where the form asks, and is not the submitting organization. Any prize is payable to the entrant personally. · Category: Fortified Enterprise Fleet · Prize targets: Fortified Enterprise Fleet, Individual/Hobbyist (Best Team/Solo Build)

This document is the governing baseline. All build sessions, human or AI-assisted, conform to it. Changes require a version bump and a one-line rationale in the Change Log. If code and this document disagree, this document wins until formally amended. Scope is FROZEN at v1.2. This is the build baseline.

> **v1.2 amendment summary** (from the 2026-08-21 crosswalk of v1.1.2 against the official hackathon rules, revised per entrant review the same day; **ratified 2026-08-21**): adds §3.6 Agent Registry & Discovery (REG — definition/instance/health state split), §3.7 Observability (OBS — W3C context authoritative, metadata-only spans), §3.8 Data Governance (DAT — jurisdiction + approved location map, `data_origin`/`trust_state`, Cloud Trace residency disclosure); extends ICD-4, CI (CI-9), DFT-5, §10; tightens SUB-1/SUB-3 to the rule text and adds SUB-8; rewords PLT-5 forfeiture; spine delay changed 10 → 21 days (the only spine change); entrant corrected to an individual/solo entry; SUB-6 replaced with an objective preexisting-rights requirement (Startup Excellence not pursued). No new reasoning agents; the operational scenario is otherwise unchanged.

---

## 1. Scope and Design Basis

FORGE is a multi-agent system that coordinates the recovery of non-mission-capable (NMC) support equipment for a fictional installation operating twelve GX-12 Ground Support Vehicles. All data is synthetic. No controlled technical information, no real platforms, no real technical orders, no actual military identifiers, no Anansi CALM-CFR+ solver code.

Design basis: the Agentic Conway Effect pilot (Sinclair 2026, v2.2). The architecture applies the managed-arm findings: exclusive ownership, pre-written interface contracts, manager-held reserves, monitoring, and a targeted repair channel. The Readiness Orchestrator is the manager agent; it performs no domain work.

## 2. Definitions

- **Work package**: a bounded task created and assigned by the Orchestrator to exactly one specialist agent.
- **Domain output**: the artifact a specialist produces in response to its work package (action plan, sourcing report, roster assignment, validation verdict, screening verdict).
- **Contract**: the versioned JSON schema governing a message type between two agents.
- **Reserve worker**: a fresh agent session/invocation of the same specialist role with empty conversational state (container reuse permitted), held unassigned until deployed by the Orchestrator.
- **Registry record**: the catalog entry describing one deployable agent (REG-1), defined declaratively in `agents/registry.yaml` and loaded into the Firestore `agent_registry` collection at deployment.
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

### 3.6 Agent Registry & Discovery (REG) — *new in v1.2*

The registry separates three records that MUST NOT be conflated: the **agent definition** (organizational approval of a role at a version — states `DRAFT`, `APPROVED`, `DEPRECATED`, `RETIRED`), the **runtime instance** (operational assignment — states `IDLE`, `RESERVE`, `ACTIVE`, `FAILED`), and **derived health** (`HEALTHY`, `STALE`, `UNKNOWN` — computed, never stored as authoritative fact).

- **REG-1**: Every deployable agent, including the Orchestrator, SHALL have a definition record containing: agent ID, version, department owner, capabilities, definition lifecycle status, input/output contract schemas and versions, service identity (service account), endpoint, and permitted tools. Definitions are declared in the version-controlled `agents/registry.yaml` and loaded into a Firestore `agent_registry` collection by the PLT-6 deployment step; runtime instances are tracked in Firestore with their instance state. Health SHALL be derived: active instances stamp `last_heartbeat_at` on the monitoring cycle and health derives from staleness; an idle service scaled to zero SHALL display `UNKNOWN` until invoked (or until an authenticated Orchestrator health probe), never a fabricated `HEALTHY`. The ORC-4 one-monitoring-cycle detection guarantee applies to active assignments only. *Verify: inspection of `agents/registry.yaml` and the loaded collections; staleness-derivation test including the scaled-to-zero `UNKNOWN` case.*
- **REG-2**: Work packages SHALL be assigned only to runtime instances of definitions whose lifecycle status is `APPROVED`. Assignment referencing a `DRAFT`, `DEPRECATED`, or `RETIRED` definition SHALL be rejected and audited. In the repair loop (ORC-3), the definition remains `APPROVED` throughout; the failed instance transitions `ACTIVE` → `FAILED` and the reserve instance transitions `RESERVE` → `ACTIVE` atomically with the work-package ownership transfer. *Verify: negative assignment test; repair-loop scene shows the instance transitions with the definition unchanged.*
- **REG-3**: The Orchestrator SHALL discover specialists by required capability and `APPROVED` definition status via the registry, not hard-coded service addresses. Discovery yields identity and endpoint only; authorization SHALL continue to fail at IAM per AGT-6, never at the registry. Absence of any `APPROVED` definition with a required capability SHALL be a distinct audited outcome feeding the ORC-4 `BLOCKED_AGENT_FAILURE` disposition. *Verify: inspection of the resolver; negative discovery test.*
- **REG-4**: The dashboard SHALL provide a read-only Agent Catalog rendering the registry: every definition with version, department owner (the REG-1 field is the source of truth for departments), capabilities, and lifecycle status, plus its instances with runtime state and derived health. *Verify: demonstration (catalog visible in the demo).*
- **REG-5**: Definition lifecycle changes (additions, version changes, approvals, deprecations, retirements) and instance state transitions SHALL emit AUD-1 audit events. CI SHALL verify that every deployed service has exactly one valid definition record and that its declared contracts, service account, and endpoint exist. *Verify: CI-9.*

### 3.7 Observability (OBS) — *new in v1.2*

- **OBS-1**: Every service SHALL emit OpenTelemetry traces to Cloud Trace using the OTel SDK with the Google Cloud Trace exporter, relying on Google ADK's built-in instrumentation for agent, model-call, and tool-call spans. The W3C trace context is authoritative: `traceparent` and `tracestate` SHALL propagate across Pub/Sub hops as message attributes, the ICD-4 `trace_id` SHALL be a read-only mirror of the 32-character OpenTelemetry trace ID, and no second application trace identifier SHALL be generated. Instrumentation SHALL NOT duplicate Cloud Run's automatic HTTP spans; ADK spans parent under them. Sampling is 100% in demo and staging acceptance environments only. *Verify: Cloud Trace waterfall of one full demo-spine run showing Orchestrator and all specialist spans under a single trace ID matching the workflow's `trace_id`; unit test of traceparent/tracestate inject/extract across a stubbed Pub/Sub hop (CI-3).*
- **OBS-2**: Spans SHALL carry only metadata attributes: AUD-1 event IDs and reason codes, agent IDs, schema versions, statuses, and latency. Spans SHALL NOT contain prompts, model responses, decision-record prose, bulletin or other document text, quarantined content, raw model chain-of-thought, or unhashed payloads. *Verify: inspection of exported spans for one spine run.*

### 3.8 Data Governance (DAT) — *new in v1.2*

- **DAT-1**: Data residency SHALL be governed by a declared residency jurisdiction (`US`) plus an approved location map in the deploy config, against which CI-9 validates every resource — not by one repeated region literal, because Google services use different location abstractions (Gemini via the `us` multi-region; Firestore a regional location such as `us-central1`, chosen once and immutable; Model Armor and Cloud Run regional). The map SHALL cover: Cloud Run services, the Firestore database location, Pub/Sub, quarantine storage (SEC-1), Model Armor templates (PLT-4), Vertex AI endpoints (PLT-1), and Cloud Logging buckets (`_Required`/`_Default` regionalized, not global). Pub/Sub SHALL use the regional endpoint (`pubsub.<region>.rep.googleapis.com`) with `allowed_persistence_regions` set to the mapped region and `enforce_in_transit: true`. *Verify: inspection of deploy config plus CI-9's location-map validation.*

  ```yaml
  residency_jurisdiction: US
  locations:
    gemini: us
    cloud_run: us-central1
    firestore: us-central1
    pubsub: us-central1
    quarantine_storage: us-central1
    model_armor: us-central1
    cloud_logging: us-central1
  ```

- **DAT-2**: Every agent-bus message envelope SHALL carry `data_origin` (constant: `SYNTHETIC`) and `trust_state` (enum: `TRUSTED`, `UNSCREENED`, `QUARANTINED`). Only Cyber Trust MAY consume `UNSCREENED` or `QUARANTINED` messages; contract validation (ICD-2) SHALL reject anything but `TRUSTED` at subscribe for every other agent, and rejections are audited. A safe Cyber Trust verdict is published as `TRUSTED` while the underlying bulletin remains `QUARANTINED`. Both fields SHALL be displayed in the dashboard and the rendered audit trail. *(The label deliberately avoids `UNCLASS`, which resembles a real defense classification marking — trust state and classification are different concepts.)* *Verify: CI-4 negative assertion; inspection of the closing-shot audit render.*
- **DAT-3**: Operational payload data SHALL remain within the configured residency jurisdiction. Cloud Trace SHALL receive metadata-only telemetry (per OBS-2) and no prompts, responses, document content, decision-record prose, or domain payloads. FORGE does not claim Assured Workloads or IL4 compliance; this exception SHALL be disclosed in the README alongside SUB-5. *Verify: inspection (OBS-2 span inspection doubles as the evidence); README statement.*

## 4. Interface Contracts & Messaging (ICD)

- **ICD-1**: All inter-agent messages SHALL conform to versioned JSON schemas stored in `/contracts`. Schemas are written before implementing either endpoint.
- **ICD-2**: Contract validation SHALL occur at both publish and subscribe. Nonconforming messages are rejected and audited, never silently coerced.
- **ICD-3**: Contract changes SHALL bump the schema version and pass CI before any dependent code merges.
- **ICD-4**: Every message SHALL carry `workflow_id`, `work_package_id`, `event_id`, `trace_id` (a read-only mirror of the OpenTelemetry trace ID per OBS-1), `idempotency_key`, `schema_version`, `data_origin`, and `trust_state` (DAT-2). *(v1.2: added `data_origin` and `trust_state`; `trace_id` semantics bound to OBS-1.)*
- **ICD-5**: Message consumption SHALL use a Firestore transactional inbox/outbox; side effects SHALL be idempotent under duplicate delivery. Pub/Sub ordering keys SHALL be set per workflow. Undeliverable messages route to a dead-letter topic after 5 attempts.
- **ICD-6**: CI SHALL include a duplicate-delivery fault-injection test. *Verify: CI-4.*

## 5. Platform Requirements (PLT)

- **PLT-1**: `gemini-3.5-flash` via Vertex AI, at the location the DAT-1 map assigns it (the `us` multi-region), SHALL perform all Orchestrator and specialist reasoning except the optional SEC-3 classifier (PLT-5). Prompts are versioned files in `/prompts`. *Hackathon mandatory (Gemini 3.5+).* *(v1.2: "single pinned region" superseded by the DAT-1 jurisdiction + location map.)*
- **PLT-2**: Google ADK, pinned version in lockfile. *Hackathon mandatory.*
- **PLT-3**: Cloud Run (one service per specialist role + Orchestrator), Firestore, Pub/Sub. *Hackathon mandatory (≥1 GCP service; we use three).*
- **PLT-4**: Model Armor in the SEC pipeline per SEC-1..SEC-2.
- **PLT-5**: Gemma SHOULD serve as the SEC-3 classifier. Decision gate: end of Day 6. If not integrated by then, a tool-less `gemini-3.5-flash` classifier satisfies SEC-3 and the Gemma route to the model-integration bonus is forfeited. *(v1.2: wording — forfeiting the Gemma route does not by itself forfeit other model-integration routes the rules may allow; any such route still requires a traceable requirement before work begins.)*
- **PLT-6**: A single documented deployment command (`deploy.sh` or `terraform apply`) SHALL stand up the environment from a clean project, given documented prerequisites: authenticated principal, project ID, billing enabled, and CI trust bootstrap. The deployment step SHALL load `agents/registry.yaml` into the `agent_registry` collection (REG-1). *Verify: clean-project deployment test before recording.*

## 6. CI/CD Requirements (CI)

Pipeline: GitHub Actions. No direct commits to `main`; PRs only, including from AI-assisted sessions. Validation runs on every PR; deployment runs only on merge to `main`.

**Lane 1 — PR gate (deterministic, no live model calls):**
- **CI-1**: Lint & format (Ruff). Fail on violation.
- **CI-2**: Contract gate: all schemas validate; all examples in `/contracts/examples` validate against their schemas.
- **CI-3**: Unit gate: per-agent tests, minimum one happy path and one boundary-violation rejection per agent, using recorded/stubbed model responses. Includes the OBS-1 traceparent inject/extract test across a stubbed Pub/Sub hop.
- **CI-4**: Spine gate: demo spine end to end against Firestore + Pub/Sub emulators with stubbed model responses. This is the drift tripwire. Explicit assertions:
  - The gated transition is blocked before approval and succeeds after approval (HUM-1).
  - Quarantined raw content never reaches any agent other than Cyber Trust (SEC-4).
  - Duplicate event delivery produces no duplicate side effects (ICD-5/ICD-6).
  - Repair-loop reassignment transfers ownership exactly once (ORC-3/ORC-4).
  - The final workflow state and full audit trail reconstruct from Firestore alone (AUD-2).
  - An injected non-`TRUSTED` message on the bus is rejected and audited by every non-Cyber-Trust subscriber (DAT-2). *(new in v1.2)*
- **CI-5**: Dead-code gate: zero high-confidence findings (Vulture) outside a reviewed allowlist.
- **CI-7**: Secrets scanning. Fail closed.
- **CI-9**: Config gate *(new in v1.2)*: every deployed service has exactly one valid registry definition record whose contracts, service account, and endpoint exist (REG-5), and every resource declaration in the deploy config validates against the DAT-1 approved location map — including the Pub/Sub regional endpoint, persistence regions, and Cloud Logging bucket locations. Extends the DFT-5 manifest check.

**Lane 2 — Staging gate (on merge to `main`):**
- **CI-6**: Auto-deploy to Cloud Run via Workload Identity Federation (no SA keys in repo), then live-Gemini smoke test of one agent round-trip and one Pub/Sub publish/consume.

**Lane 3 — Acceptance gate (manual trigger, pre-recording):**
- **CI-8**: 10 consecutive clean runs of the full spine on deployed infrastructure with live models. Required before the final video.

## 7. Drift Prevention (DFT)

- **DFT-1**: `requirements/traceability.yaml` SHALL map every requirement ID via `implemented_by`, `verified_by`, and `verification_method: test | inspection | demonstration | document`. CI enforces: every requirement mapped, every production module referenced by at least one requirement.
- **DFT-2**: AI-assisted sessions SHALL be pointed at this document at session start. Proposed work outside these requirements is rejected in review, not merged and cleaned up later.
- **DFT-3**: New dependencies require a one-line justification in the PR description.
- **DFT-4**: After the dress-rehearsal recording (Day 8), `main` is frozen except for defects on the demo spine. Tag `v1.2-demo` at freeze.
- **DFT-5**: `architecture/manifest.yaml` SHALL map each deployed service to its source modules, to its node in the architecture diagram, and to its `agent_id` in `agents/registry.yaml` (REG-1). CI checks the manifest against the deploy config.

## 8. Demo Spine (Frozen Scenario)

1. Discrepancy ingested: GX-12 #7 NMC, failed hydraulic actuator.
2. Orchestrator decomposes; Maintenance Agent produces action plan; Supply Agent reports approved part delayed 21 days (three weeks). *(v1.2: was 10 days)*
3. Synthetic vendor bulletin arrives recommending unauthorized substitute; contains embedded prompt-injection attempt.
4. Bulletin enters quarantine; classifier + Model Armor screen it; quarantine verdict and candidate-part identifier published per SEC-4; Safety Agent rejects the identifier against the approved-parts registry. Raw bulletin never leaves quarantine. **(Scene 1: fail-closed quarantine)**
5. Fault injection: Workforce Agent returns malformed assignment past retry limit. Orchestrator detects within one monitoring cycle, atomically reassigns to reserve. **(Scene 2: repair loop)**
6. Compliant recovery plan assembled; human approval of the 21-day schedule override requested with decision record; approved by authenticated approver via dashboard control. **(Scene 3: human gate)** *(v1.2: was 10-day)*
7. Logical Clock advances 21 days; due event fires (and is double-fired in test); part arrives; workflow resumes unattended and halts at `AWAITING_RELEASE_APPROVAL`. Equipment release requires a second HUM-1 approval; the workflow never reaches `RELEASED` unattended. **(Scene 4: time-skip)** *(v1.2: was 10 days)*
8. Audit trail rendered from Firestore alone: every decision, veto, block, and approval, with `observed_at`/`effective_at` distinguishing the time jump. **(Closing shot)**

Acceptance: CI-8 passed before final recording.

## 9. Submission Requirements (SUB)

- **SUB-1**: Public demo video ≤ 4 minutes on YouTube/Vimeo, English, containing, in order: (a) a scripted problem overview and value proposition (≈30–45 seconds) before Scene 1; (b) the Section 8 demo spine recorded as a **single-take, unedited demo segment** showing live execution; (c) visible Google Cloud Console proof of deployment inside the same take. *(v1.2: enumerates the rules' four required video elements and freezes the single-take recording protocol.)* *Verify: inspection at dress rehearsal, time-boxed ≤ 4:00.*
- **SUB-2**: Architecture diagram consistent with `architecture/manifest.yaml`.
- **SUB-3**: Public (or judge-accessible) repo with reproducible README spin-up instructions. If the repo is private at submission, it SHALL be shared with `testing@devpost.com` and `cloudhackathons@google.com` before SUB-7 completion. Judge access procedure SHALL be documented for **any** private surface, including the hosted dashboard if a hosted URL is submitted. The README SHALL include an Evidence section linking passing CI runs (the CI-4 assertion list, the CI-8 acceptance record) and the DFT-1/DFT-5 traceability and manifest files. *(v1.2: names the rule-mandated sharing addresses; extends "any surface"; adds Evidence section.)*
- **SUB-4**: `PREEXISTING_WORK.md` disclosing, with cited conceptual influences distinguished from materials actually incorporated (SUB-6): the Agentic Conway Effect pilot (Sinclair 2026, v2.2) **cited as conceptual background only**; an explicit statement that no pilot repositories, experimental datasets, report text, or CALM-CFR+ code are incorporated; an explicit statement that all FORGE code, schemas, prompts, fixtures, interface assets, and demonstration materials were created by the entrant during the Submission Period; AI coding assistants used during the Submission Period; any reused assets (fonts, icons, libraries) with licenses.
- **SUB-5**: A clean statement in the README: no CALM-CFR+ code, no controlled information, no real platform data, no real technical orders, no actual military identifiers.
- **SUB-6 — Preexisting rights** *(replaced in v1.2)*: The entrant SHALL submit only materials he owns or is documented as authorized to use. `PREEXISTING_WORK.md` SHALL distinguish cited conceptual influences from materials actually incorporated into FORGE. The submission SHALL NOT include Anansi Labs code, confidential information, unpublished research artifacts, trademarks, datasets, or other company-owned materials without documented authorization. Citation of publicly available background research does not incorporate that research into the submission. *Verify: inspection of `PREEXISTING_WORK.md` and the repository against this rule before SUB-7 completion.*
- **SUB-7**: Devpost submission completed with margin before Aug 31, 5:00 PM PT.
- **SUB-8** *(new in v1.2)*: The Devpost text description SHALL cover all four rule-mandated sub-parts: features and functionality; technologies used; other data sources (stating all data is synthetic per SUB-5); and findings and learnings. The findings-and-learnings section SHALL be drafted no later than the day after the Day-8 dress rehearsal. *Verify: document (DFT-1, verification_method: document).*

## 10. Explicit Non-Requirements

OUT of scope: multi-vehicle concurrent recoveries, real supplier APIs, authentication/multi-tenancy beyond IAM scoping, mobile UI, configurable fleets, tamper-proof logging, and any CALM-CFR+ solver integration. The dashboard is read-only except for approve/reject controls (HUM-1). *(v1.2 additions:)* Also OUT: trust-state or classification schemes beyond DAT-2's `data_origin` + three-value `trust_state`; DLP, CMEK, VPC Service Controls, or Assured Workloads; OTel Collector deployments, custom metrics, or dashboards beyond Cloud Trace's native rendering; any registry write surface beyond the deploy-time load and audited lifecycle/instance transitions (the Agent Catalog stays read-only).

## 11. Change Log

- v1.0 — Initial baseline.
- v1.1 — Review integration: resolved ORC-1/AGT-1 ownership conflict; defined reserve worker, monitoring cycle, Logical Clock; HUM-2 decision record replaces reasoning chain; Safety veto non-overridable; AGT-6 IAM architecture explicit (service-per-role); added SEC quarantine-first fail-closed pipeline; added ICD-4..6 messaging reliability; pinned model/region/versions (PLT-1); Gemma demoted to SHOULD with Day-6 gate; CI split into three lanes; DFT-1/DFT-5 machine-readable traceability and manifest; added SUB section; dashboard contradiction fixed. Scope frozen.
- v1.1.1 — Defect corrections, no scope change: SEC-4 verdict-only publication resolves quarantine contradiction; spine names the approved action (schedule override) and post-skip halt state (AWAITING_RELEASE_APPROVAL); ORC-5 keyed by workflow_id; reserve worker defined as fresh session, critical role = Workforce; PLT-1 Gemma exception; HUM-1 attributable approval behind IAM/IAP; AUD-3 dual timestamps; CI-4 explicit assertions; DFT-1 verification methods; scene renumbering; DFT-4 tag corrected. BUILD BASELINE.
- v1.1.2 — Failure disposition clarified (ORC-4: Workforce reassigns to reserve; other roles enter `BLOCKED_AGENT_FAILURE` with audited escalation); no scope change. Copyedits.
- v1.2 — Category-evidence amendment from the 2026-08-21 crosswalk against the official hackathon rules, revised per member review: added REG (agent registry & discovery with definition/instance/health state separation, closing the Fleet category's cataloging clause), OBS (OpenTelemetry to Cloud Trace, W3C context authoritative, metadata-only spans), DAT (residency jurisdiction + approved location map, `data_origin`/`trust_state` envelope fields, Cloud Trace residency exception disclosed); ICD-4 gains `data_origin`/`trust_state`; CI-9 config gate; SUB-1/SUB-3 aligned to rule text (video elements, single-take protocol, named sharing addresses, Evidence section); SUB-8 text description; spine delay 10 → 21 days to match "weeks of asynchronous operations"; PLT-5 forfeiture scoped to the Gemma route; §10 guards against gold-plating the new sections; entrant corrected to individual/solo (Armond E. Sinclair; Anansi Labs affiliation-only), SUB-6 replaced with an objective preexisting-rights requirement (no organizational authorization or member non-objection; SUB-4 aligned), Startup Excellence dropped from prize targets. Ratified 2026-08-21. No new reasoning agents; scenario otherwise unchanged.
