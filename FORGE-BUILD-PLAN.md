# FORGE Build Plan
**Fleet Operational Readiness & Governed Execution — Execution Schedule**
Plan v1.0 · 2026-08-21 · Subordinate to FORGE-REQUIREMENTS.md v1.1.2

This plan schedules the build; it does not amend the baseline. If this plan and FORGE-REQUIREMENTS.md v1.1.2 disagree, the requirements document wins. The spec's relative gates ("Day 6", "Day 8") are hereby anchored to calendar dates with Day 1 = Friday, August 21, 2026.

---

## 1. Verified Competition Facts (Devpost, checked 2026-08-21)

- Submission window: **Aug 3, 9:00 AM PT → Aug 31, 5:00 PM PT**. Project work must be newly created during this window — our Aug 21 start is safely inside it. The Agentic Conway Effect pilot is conceptual design input and must be disclosed as pre-existing work, which is exactly what SUB-4 does.
- AI coding assistants: explicitly permitted, disclosure required (SUB-4 covers).
- Mandatory stack, all three: Gemini 3.5+ via Gemini API or Vertex AI (PLT-1 ✓), an agent framework including Google ADK (PLT-2 ✓), Google Cloud infrastructure (PLT-3 ✓ — Cloud Run, Firestore, Pub/Sub).
- Track: **Fortified Enterprise Fleet** — rules call for zero-trust access controls and guardrails against prompt injection, tool poisoning, and PII leaks. FORGE's AGT-6 IAM-per-role and SEC-1..4 quarantine pipeline are direct hits on the track's stated criteria.
- Judging weights: Innovation & Operational Utility **40%**, Architectural Discipline & Tech Stack **30%**, Demo & Production Readiness **30%**.
- Video: ~4 minutes; must show problem, value proposition, live demo, and visible proof the backend runs on Google Cloud (SUB-1 ✓).
- Devpost form additionally requires a **text description** (features, technologies, data sources, learnings) — not itemized in the spec's SUB list; scheduled Day 9–10 below.
- Hosted URL optional; app need not be publicly live. Optional bonus: blog/video or #AllThingsAgenticHackathon social post.
- Employer-entry rule: entering on behalf of Anansi Labs requires consent — SUB-6's written authorization satisfies this. **Human dependency: request it Day 1, hold it before submission.**

## 2. Calendar

| Day | Date | Focus | Gate |
|-----|------|-------|------|
| 1 | Fri Aug 21 | Foundations: repo, contracts, CI Lane 1 seed, GCP project | — |
| 2 | Sat Aug 22 | Messaging backbone: state, inbox/outbox, audit, Logical Clock | — |
| 3 | Sun Aug 23 | Orchestrator + Maintenance & Supply agents | — |
| 4 | Mon Aug 24 | Workforce & Safety agents + monitoring/repair loop | — |
| 5 | Tue Aug 25 | SEC quarantine pipeline + Cyber Trust | — |
| 6 | Wed Aug 26 | Human gate, dashboard, IAM hardening, deploy.sh | **Gemma go/no-go (PLT-5)** |
| 7 | Thu Aug 27 | Full spine on emulators, CI lanes complete, clean-project deploy test | — |
| 8 | Fri Aug 28 | Live dress rehearsal + rough-cut recording | **Freeze `main`, tag `v1.1-demo` (DFT-4)** |
| 9 | Sat Aug 29 | CI-8 acceptance (10/10), final video, README/disclosures | **CI-8 before recording** |
| 10 | Sun Aug 30 | Submission assembly; submit by evening | **SUB-7 with ~20 h margin** |
| — | Mon Aug 31 | Pure buffer; edit/resubmit window only | Deadline 5:00 PM PT |

## 3. Day Detail

### Day 1 — Fri Aug 21 · Foundations & Contracts
Repo created; branch protection — PRs only, no direct commits to `main` (CI preamble). Directory scaffold: `/contracts` (+`/contracts/examples`), `/prompts`, `/requirements`, `/architecture`, one service directory per role. **All inter-agent JSON schemas written today, before any endpoint code (ICD-1), each message carrying `workflow_id`, `work_package_id`, `event_id`, `trace_id`, `idempotency_key`, `schema_version` (ICD-4).** Contract validation harness at publish and subscribe (ICD-2 groundwork); schema versioning rule wired into CI (ICD-3). `requirements/traceability.yaml` and `architecture/manifest.yaml` skeletons (DFT-1, DFT-5). CI Lane 1 seed: Ruff (CI-1), contract gate (CI-2), secrets scan (CI-7). GCP: project, billing, APIs enabled, region pinned; **Model Armor and Vertex `gemini-3.5-flash` smoke calls today** (see risk R2). Send SUB-6 authorization request to both Anansi Labs members.
**Exit: CI green on scaffold; every contract schema + example validating; GCP smoke calls succeed.**

### Day 2 — Sat Aug 22 · Messaging Backbone & State
Firestore workflow documents keyed by `workflow_id` with `logical_time` and `due_at` (ORC-5). Transactional inbox/outbox; idempotent side effects under duplicate delivery; Pub/Sub ordering keys per workflow; DLQ after 5 attempts (ICD-5). Append-only audit writer: timestamp, agent identity, input hash, output hash, reason code (AUD-1); state transition + audit/outbox in one Firestore transaction (AUD-2); dual `observed_at`/`effective_at` stamps (AUD-3). Logical Clock document; advancing it creates a due event; due-event processing idempotent (ORC-5). Firestore + Pub/Sub emulator harness; duplicate-delivery fault-injection test (ICD-6).
**Exit: double-fire test passes on emulators; audit trail reconstructs a toy workflow from Firestore alone.**

### Day 3 — Sun Aug 23 · Orchestrator & First Specialists
Orchestrator service: decomposes NMC event into work packages with exclusive ownership and an assignment log (ORC-1); **no domain tools bound to it — enforced by construction, not convention (ORC-2)**. ADK agent base: schema-constrained structured output, no raw-output-to-tool path, retry ≤2 then failure event (AGT-7). Maintenance Agent — action plan, cannot release equipment (AGT-1). Supply Agent — approved-parts lookup + shipment status, cannot approve substitutions (AGT-2). Prompts as versioned files in `/prompts` (PLT-1). Per-agent unit tests: one happy path + one boundary-violation rejection, stubbed model responses (CI-3).
**Exit: Orchestrator assigns; both agents return contract-valid outputs on emulators.**

### Day 4 — Mon Aug 24 · Remaining Specialists & Repair Loop
Workforce Agent — technicians matched to task-code qualifications only, no waivers (AGT-3). Safety & Policy Agent — validates against synthetic procedures library, veto non-overridable by humans (AGT-4). Monitoring loop at 5 s cycle detecting timeout, malformed-after-retries, and contract violation (ORC-4). Reserve worker held for Workforce; atomic ownership transfer + audit event on reassignment (ORC-3); `BLOCKED_AGENT_FAILURE` + audited escalation for other roles (ORC-4). Repair-loop fault-injection test.
**Exit: injected Workforce failure detected within one cycle and reassigned exactly once, on emulators.**

### Day 5 — Tue Aug 25 · SEC Pipeline & Cyber Trust
Quarantine storage; bounded parser; explicit Model Armor screening call (PLT-4); tool-less structured classifier (SEC-1, SEC-3). **Classifier is built on `gemini-3.5-flash` first so SEC works regardless of the Gemma gate; Gemma integration attempted in parallel for the bonus (PLT-5).** Any parse/screen/availability error leaves the document quarantined — fail closed (SEC-2). Publication is verdict-only plus safe extracted metadata (candidate-part identifier); raw document never reaches the bus; downstream agents evaluate identifiers against trusted registries (SEC-4, AGT-5). Author the synthetic vendor bulletin with embedded prompt-injection attempt (demo asset). Negative tests: non-CyberTrust agents cannot read quarantine.
**Exit: poisoned bulletin quarantined; Safety rejects the substitute identifier; raw text provably absent from the bus.**

### Day 6 — Wed Aug 26 · Human Gate, Dashboard, IAM · **GEMMA GATE**
Approval surface behind Cloud Run IAM/IAP; approver identity from the authenticated principal, never client input (HUM-1). Decision record: source references, extracted facts, applicable rules, constraints, confidence, alternatives, recommendation, agent/model/schema versions — no claimed chain-of-thought (HUM-2). Dashboard: read-only except approve/reject (Non-Req §10); audit trail rendered from Firestore alone (AUD-2). Cloud Run service-per-role with per-service service accounts; inter-service calls IAM-authenticated; cross-boundary tool calls fail at IAM — one negative test per agent (AGT-6). First full `deploy.sh` pass from documented prerequisites (PLT-6).
**Exit: gated transition blocked before approval, succeeds after, on deployed infra. END OF DAY: Gemma integrated → keep; else flip SEC-3 to `gemini-3.5-flash` permanently and forfeit the bonus (PLT-5). Decision is final either way — no revisiting.**

### Day 7 — Thu Aug 27 · Full Spine + CI Complete
Demo spine steps 1–8 end-to-end on emulators with stubbed models — the CI-4 drift tripwire with all five explicit assertions (HUM-1 gate, SEC-4 isolation, ICD-5/6 idempotency, ORC-3/4 single reassignment, AUD-2 reconstruction). Time-skip scene: Logical Clock +10 days, due event double-fired, unattended resume, halt at `AWAITING_RELEASE_APPROVAL`, `RELEASED` requires second approval (ORC-5, Spine 7). Lane 2: Workload Identity Federation deploy + live-Gemini smoke of one agent round-trip and one Pub/Sub publish/consume (CI-6). Dead-code gate with reviewed allowlist (CI-5). Clean-project deployment test (PLT-6 verify). Traceability complete and CI-enforced: every requirement mapped, every module referenced (DFT-1); manifest checked against deploy config (DFT-5).
**Exit: all three CI lane-1 gates + CI-4 green; clean-project deploy documented; traceability passing.**

### Day 8 — Fri Aug 28 · Dress Rehearsal · **FREEZE**
Full spine on deployed infrastructure with live Gemini; fix defects on sight. Dress-rehearsal recording (rough cut) — validates the ≤4-minute pacing across Scenes 1–4 + closing audit shot. Architecture diagram drawn from `architecture/manifest.yaml` (SUB-2, DFT-5). Script the final video: problem → value proposition → live scenes → GCP console proof.
**Exit: END OF DAY freeze `main`, tag `v1.1-demo` (DFT-4). Thereafter demo-spine defects only.**

### Day 9 — Sat Aug 29 · Acceptance & Final Video
CI-8: 10 consecutive clean spine runs on deployed infrastructure with live models — required before final recording (ORC-4 verify rides on this). Record and edit the final video ≤4 min with visible Google Cloud Console proof (SUB-1); upload public YouTube/Vimeo. README: reproducible spin-up, judge access procedure if anything is private (SUB-3); clean statement — no CALM-CFR+, no controlled information, no real platforms/TOs/identifiers (SUB-5). `PREEXISTING_WORK.md`: Conway Effect pilot, AI assistants used, reused assets + licenses (SUB-4). Draft Devpost text description.
**Exit: CI-8 10/10 logged; video uploaded; repo submission-ready.**

### Day 10 — Sun Aug 30 · Submit
Confirm SUB-6 written authorization from both members is in hand (requested Day 1). Complete Devpost: description, video link, repo link, architecture diagram, judge access instructions. **Submit by Sunday evening Aug 30 — roughly a day of margin (SUB-7).**

### Mon Aug 31 · Buffer
No planned work. Devpost edit/resubmit window only, closes 5:00 PM PT.

## 4. Risk Register

- **R1 — SUB-6 authorization is a human dependency.** Mitigation: request in writing Day 1; track daily; submission blocks without it.
- **R2 — Model Armor availability/quota in the pinned region.** Least-familiar service in the stack; discovering an enablement problem on Day 5 is too late. Mitigation: smoke call Day 1 during GCP setup; fallback discussion forced Day 2 if it fails.
- **R3 — CI-8 flakiness with live models.** 10/10 consecutive is unforgiving. Mitigation: AGT-7 schema-constrained outputs + bounded retries are the design answer; Day 9 morning reserved; repeated failure is a demo-spine defect, permitted under the freeze.
- **R4 — Schedule slip.** The backbone and heaviest agent build (Days 2–4) run through the weekend, and the acceptance/video push (Days 9–10) lands on the final weekend. Jettison order: Gemma bonus first (already priced by PLT-5), then Day 10 slack. The spine itself is not negotiable.
- **R5 — Vertex quota / billing limits** for `gemini-3.5-flash` in the pinned region. Mitigation: verify quotas Day 1; request increases immediately if low — approvals take days.
- **R6 — Video overrun.** Two HUM-1 approvals, four scenes, console proof in ≤4 min. Mitigation: pacing validated at Day 8 rough cut, not discovered on Day 9.

## 5. Judging Alignment (how the plan spends its effort)

- **Innovation & Operational Utility (40%)** — the narrative is the managed-arm findings made production-real: exclusive ownership, pre-written contracts, manager-held reserves, targeted repair. Scenes 2 (repair loop) and 4 (time-skip resume) carry this; the video script leads with the operational problem (NMC equipment recovery), not the tech.
- **Architectural Discipline (30%)** — IAM-per-role enforcement (AGT-6), contracts-before-code (ICD-1), transactional audit (AUD-2), traceability CI (DFT-1). The architecture diagram and repo structure must make this legible to judges in minutes.
- **Demo & Production Readiness (30%)** — CI-8 10/10 before recording, visible console proof, single-command deploy (PLT-6), reproducible README (SUB-3).

## 6. Requirement Coverage Map

| Requirement IDs | Primary day(s) |
|---|---|
| ORC-1, ORC-2 | 3 |
| ORC-3, ORC-4 | 4 |
| ORC-5 | 2, 7 |
| AGT-1, AGT-2 | 3 |
| AGT-3, AGT-4 | 4 |
| AGT-5 | 5 |
| AGT-6 | 6 |
| AGT-7 | 3 |
| SEC-1, SEC-2, SEC-3, SEC-4 | 5 |
| HUM-1, HUM-2 | 6 |
| AUD-1, AUD-2, AUD-3 | 2 (verified 6–7) |
| ICD-1, ICD-2, ICD-3, ICD-4 | 1 |
| ICD-5, ICD-6 | 2 |
| PLT-1 | 1 (pin), 3 (prompts) |
| PLT-2 | 1 (lockfile pin) |
| PLT-3 | 1 (project), 6 (service-per-role) |
| PLT-4 | 1 (smoke), 5 (pipeline) |
| PLT-5 | 5 (parallel attempt), 6 (gate) |
| PLT-6 | 6 (first pass), 7 (clean-project verify) |
| CI-1, CI-2, CI-7 | 1 |
| CI-3 | 3 |
| CI-4 | 7 (seeded Day 2) |
| CI-5 | 7 |
| CI-6 | 7 |
| CI-8 | 9 |
| DFT-1 | 1 (skeleton), 7 (enforced) |
| DFT-2 | every session start (protocol §7) |
| DFT-3 | every PR (protocol §7) |
| DFT-4 | 8 |
| DFT-5 | 1 (skeleton), 7 (checked), 8 (diagram) |
| SUB-1 | 8 (script), 9 (record) |
| SUB-2 | 8 |
| SUB-3, SUB-4, SUB-5 | 9 |
| SUB-6 | 1 (request), 10 (confirm) |
| SUB-7 | 10 |

## 7. Session Protocol (standing, every build session)

Each AI-assisted session starts pointed at FORGE-REQUIREMENTS.md v1.1.2 (DFT-2). All changes land as PRs — never direct to `main`. New dependencies carry a one-line justification in the PR description (DFT-3). Work not traceable to a requirement ID is rejected in review, not merged and cleaned up later. After the Day-8 freeze, only demo-spine defects merge (DFT-4).

## 8. Plan Change Log

- Plan v1.0 (2026-08-21) — Initial schedule anchored Day 1 = Aug 21; Devpost rules verified same day; Devpost text description added to Day 9–10 as a submission item not itemized in the spec's SUB list.
