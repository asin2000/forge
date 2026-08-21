# Engineering decisions (one line each, per DFT-3 spirit)

- 2026-08-21 · Region pinned `us-central1` (PLT-1): broadest availability for
  Vertex `gemini-3.5-flash` + Model Armor; verified by Day-1 smoke tests.
- 2026-08-21 · Python 3.11 (matches build environment; Ruff/Vulture per spec).
- 2026-08-21 · Message shape `{envelope, payload}` with per-type
  `schema_version` const-pin: one envelope schema, impossible to validate a
  message against the wrong contract (ICD-2/ICD-4).
- 2026-08-21 · Logical Clock time = integer day counter (`effective_at`,
  `logical_time`, `due_at` are logical days); unambiguous across jumps (AUD-3).
- 2026-08-21 · `work_package_id: "wp-none"` sentinel for workflow-scoped
  messages, keeping all six ICD-4 fields required on every message.
- 2026-08-21 · SEC-3 classifier built on `gemini-3.5-flash` first; Gemma
  attempted in parallel for the bonus only (de-risks the Day-6 PLT-5 gate).
- 2026-08-21 · Schema immutability enforced by git-diff gate
  (`check_schema_versions.py`) rather than convention (ICD-3).
- 2026-08-21 · Dependencies: `google-adk` (PLT-2 mandate), `jsonschema` +
  `referencing` (contract validation), `pyyaml` (traceability/manifest gates);
  exact pins in `requirements.lock`.

## 2026-08-21 — v1.2 baseline landed (branch `v1.2-baseline`)

- **Envelope v2 cascade.** DAT-2 adds `data_origin` (const `SYNTHETIC`) and
  `trust_state` (`TRUSTED|UNSCREENED|QUARANTINED`) to the ICD-4 envelope;
  OBS-1 makes `trace_id` a read-only mirror of the 32-hex OTel trace ID
  (pattern-enforced in v2). Merged schemas are immutable (ICD-3), so every bus
  message type was bumped to `.v2`; the `.v1` files remain as history.
  `MESSAGE_TYPES` in `forge_common.contracts` lists only the ACTIVE versions
  (v2) — a v1 message is rejected at runtime by design.
- **Registry model (REG).** `agents/registry.yaml` holds declarative agent
  DEFINITIONS only (lifecycle: DRAFT/APPROVED/DEPRECATED/RETIRED). Runtime
  instances (IDLE/RESERVE/ACTIVE/FAILED) and derived health
  (HEALTHY/STALE/UNKNOWN) live in Firestore. Scene 2 shows instance
  transitions; the definition never changes state during the repair loop.
- **Residency model (DAT-1).** `infra/residency.yaml` declares a jurisdiction
  plus an approved location map — NOT one region literal (Gemini is `us`
  multi-region; Firestore is regional and immutable once set). CI-9 will
  validate deploy config against the map.
- **Gate tweak.** `validate_contracts.py` example-exemption generalized from
  `envelope.v1` to any `envelope.v*` (the envelope is a $ref fragment, not a
  bus message type).
- Old `docs/SUB6-authorization-request.md` deleted: v1.2 SUB-6 is an objective
  preexisting-rights rule (see PREEXISTING_WORK.md); no member authorization
  artifact exists anymore.

## 2026-08-22 — Day 2 core (branch `day2-state-and-messaging`)

- **Transaction shape (AUD-2).** One Firestore transaction couples: inbox
  marker create (when consuming), state write (schema-validated), audit
  create, outbox creates. `layout.run_in_transaction` drives the real
  client's retry path and the buffered test double identically.
- **Idempotency (ICD-5/ORC-5).** Consumption dedupes on an inbox marker
  keyed by `idempotency_key`; due events use deterministic event IDs
  (uuid5 of workflow_id+due_at) so double-fire collides on `create`.
  Publishing is at-least-once; ordering key = workflow_id.
- **HUM-1 at the state layer.** `GATED_TARGETS` maps
  SUSPENDED_AWAITING_PART→schedule_override and RELEASED→equipment_release;
  transition without an approved decision raises GateBlocked. This is the
  CI-4 "blocked before approval" assertion's implementation.
- **Audit ordering (AUD-3).** Trail reconstruct orders by (effective_at,
  observed_at, event_id); observed_at at microsecond resolution.
- **No Java locally** → Lane 1 runs against the buffered fake; the CI-4
  emulator harness lands in CI where a JDK exists.
- **Clock scan outside the clock transaction.** advance_clock is a small
  read-modify-write txn; due-event emission is per-workflow idempotent
  enqueue, so a crash mid-scan re-runs safely (ORC-5).

## 2026-08-22 — Day 2 adversarial review fixes (same branch)

An 8-agent adversarial review (4 lenses + verifiers) confirmed 11 defects;
all fixed with regression tests:
- **Real `Transaction.get` returns a generator**, not a snapshot — every
  transactional read now goes through `layout.txn_get_dict`, and the fake's
  transaction.get yields a generator so tests exercise the real shape.
- **Inbox markers are consumer-scoped** (`idempotency_key--consumer`), so
  fan-out subscribers dedupe independently instead of locking each other out.
- **`effective_at` tracks the Logical Clock**: every transition reads
  `system/logical_clock` in-transaction and stamps state doc + audit event —
  the closing-shot time jump now renders correctly (AUD-3).
- **HUM-1 evidence persists verbatim**: approval_id/action/decision/
  approver_identity/decided_at recorded in the audit `detail`; approvals are
  consumed once via a transactional marker, so replay cannot re-authorize.
- **No unchecked state writes**: `TxnWrites.state_doc` replaced by
  `TxnWrites.transition`, applied through `state.apply_transition` (table +
  gates) inside the consumer's transaction.
- Suspension REQUIRES `due_at` (and due_at is rejected elsewhere); rejected
  release gets a rework exit (AWAITING_RELEASE_APPROVAL → ASSEMBLY_RESUMED).
- Outbox messages validated at enqueue AND publish (ICD-2); drain publishes
  in (enqueued_at, event_id) order so the ordering key means something.
- Contract/trust rejections write a `blocked_action` audit event (ICD-2
  "rejected and audited"); due-event identity includes `purpose`.
- Documented scope limit: re-arming an already-fired absolute due day will
  not re-fire (needs a suspension-epoch counter = state schema bump; §10).

## 2026-08-22 — Day 2 verification gate (branch `day2-verification-gate`)

Entrant review (read-only) correctly rejected "Day 2 complete": the emulator
exit gate had not run and the handler executed inside the transaction
callback. Both closed, plus five confirmed findings:
- **Retry boundary**: handlers now run OUTSIDE the transaction, exactly once
  per accepted delivery, producing a precomputed effect plan; only the plan
  (and the deterministic, read-only apply_transition) runs inside the
  callback Firestore may rerun. Day 3 model/tool calls live in handlers.
- **Emulator gate**: `tests/emulator/` runs the real clients against the
  Firestore + Pub/Sub emulators in a new pr-gate job — real Transaction
  semantics (the generator regression, on the real client), ordered
  publish/pull, outbox→publish→nack→redeliver→dedupe end-to-end, and the
  5-attempt DLQ policy on subscriptions (`forge_common.pubsub`). Emulator
  does not enforce DLQ forwarding — verified in Lane 2 (CI-6).
- **HUM-1 chain**: transitions take `approval_id`; the authoritative
  approval_decision.v2 record (contract-validated at write via
  `record_approval_decision`, production writer = the IAP surface) is
  retrieved transactionally. State layer now proves a recorded decision,
  with authentication enforced at the only writer.
- **Clock integrity**: `emit_due_events` reads the clock itself and
  transactionally rechecks status/due_at per workflow; `advance_clock`
  audits the jump under `wf-system-clock`.
- Unroutable malformed messages audit under `wf-security-unroutable`;
  inbox marker IDs are hashed (Firestore ID-safe); handler-supplied audit
  and outbox messages are contract-validated and workflow-pinned.
- Governance drift fixed: build plan → v1.2 / 21 days / `v1.2-demo`;
  traceability message-schema refs → `.v2`.

## 2026-08-22 — Corrections to earlier entries (v1.2 supersessions)

Two statements in the Day-1 entry are superseded and should not be relied on:
- "region `us-central1`" for the model: `gemini-3.5-flash` runs at the `us`
  multi-region per the DAT-1 residency map (`infra/residency.yaml`);
  `us-central1` remains correct for Cloud Run/Firestore/Pub/Sub/Model Armor.
- "the envelope carries the six mandatory ICD-4 fields": the active envelope
  is v2 with EIGHT fields (adds `data_origin`, `trust_state`; `trace_id` is
  the 32-hex OTel mirror).
Also noted for accuracy: emulator-verified (PR #3) is not deployment-verified
— live GCP verification is scheduled Day 6–7 (Lane 2, CI-6).

## 2026-08-23 — Day 3: Orchestrator, first specialists, registry discovery

- **Orchestrator (ORC-1/2).** `services/orchestrator/handlers.py` binds no
  domain tools; its model call yields role OBJECTIVES only, validated
  against an internal schema via `constrained_json` (AGT-7 discipline for
  non-bus output). Bus outputs are work_package_assignment.v2 messages +
  exclusive ownership docs (`workflows/{id}/work_packages/{wp}`, created —
  collision = second assignment refused by the datastore) + the PLANNING
  transition, all in the consumer's committing transaction.
- **Registry discovery (REG-1..3).** `registry.load_registry` (deploy step)
  writes definitions and seeds instances (`agent-<role>-NN` — the contract's
  assigned_agent_id pattern); workforce gets a RESERVE instance for Day 4.
  `discover(capability)` returns only APPROVED definitions and never hands
  out reserves; NoCapableAgent → audited escalation + BLOCKED_AGENT_FAILURE.
- **Specialists (AGT-1/2/7).** `StructuredAgent` wraps the injected model
  callable: parse → envelope → full contract validation; ≤2 retries then a
  contract-valid agent_failure_event.v2. Boundary enforcement is the
  contract itself (additionalProperties: false — a release or substitution
  field fails validation). Specialists never request transitions.
- Orchestrator reasoning failure raises (NACK/redeliver) instead of emitting
  agent_failure_event — that schema's role enum is specialists-only, by
  design (ORC-4 governs specialist failures; the orchestrator's own retry
  path is the transport lease).

## 2026-08-23 — Day 3 completion (entrant HOLD): real ADK + five gap fixes

- **Google ADK is now the execution path (PLT-2).** `agent_base` builds an
  ADK `LlmAgent` driven by a `Runner` with a fresh session per call;
  production passes a Vertex model string, tests pass a `BaseLlm` stub
  (`tests/adk_stub.py`) — identical pipeline either way. Live proof:
  `scripts/smoke_adk_agent.py` (gemini-3.5-flash on Vertex → contract-valid
  sourcing_report.v2), captured in docs/verification.
- **No partial assignments.** The Orchestrator resolves EVERY required
  capability before accumulating any write; any NoCapableAgent → BLOCKED
  with zero assignments/claims.
- **REG-2 transactional.** `check_claim_eligibility` re-reads definition +
  instance INSIDE the committing transaction (reads before writes);
  IneligibleAssignment → audited (ASSIGNMENT_INELIGIBLE) + NACK, so
  redelivery re-discovers fresh state.
- **Specialist runs audited (AUD-1)** atomically with their outbox message:
  decision/DOMAIN_OUTPUT_PRODUCED or failure/SPECIALIST_MALFORMED.
- **Orchestrator exhaustion bounded**: after AGT-7's limit →
  ORCHESTRATOR_REASONING_EXHAUSTED escalation + BLOCKED (recovery via
  BLOCKED → PLANNING), not a NACK loop.

## 2026-08-24 — Day 4: repair loop, monitoring, data-backed domain facts

- **Domain facts come from data, never the model.** `/data/*.yaml`
  (approved parts, qualifications, procedures) + `synthetic_data.py`;
  `StructuredAgent.run(payload_check=...)` refuses outputs contradicting
  the data (retry → agent_failure_event, failure_kind contract_violation).
  Supply approval, Workforce qualifications (no waivers), and Safety
  verdicts are all data-verified; prompts are grounded with registry
  excerpts (supply prompt bumped to v2).
- **Repair loop (ORC-3).** Failure-event handler: workforce → reassignment
  via TxnWrites.reassignments — ownership transfer, instance flips
  (FAILED/ACTIVE), reassignment audit, and the seq-2 assignment commit in
  ONE transaction; the ownership guard makes the transfer exactly-once
  under double-fired failure events. Other roles → BLOCKED + escalation
  (ORC-4 disposition). Reserve located via registry (state RESERVE only).
- **Monitoring (ORC-4).** `monitor.run_monitoring_cycle` (5s demo cycle):
  stale ASSIGNED work packages synthesize deterministic timeout
  agent_failure_events into the outbox (idempotent across cycles);
  malformed/contract failures are emitted at the source by StructuredAgent.
- Riders: REG-2 negative race + full repair loop proven on the REAL
  Firestore emulator; ADK smoke artifact now carries exact captured stdout.

## 2026-08-24 — Day 4 closeout (entrant HOLD): package lifecycle + real flow

- **Package lifecycle (gap 1).** Specialists flip their package
  COMPLETED / FAILED_PENDING_REPAIR atomically with their output
  (TxnWrites.work_package_status_updates; skip-guarded so a stale worker's
  late output cannot flip a reassigned package). The monitor times out only
  ASSIGNED packages — successful agents can never be falsely failed.
- **Real Workforce flow (gap 2).** make_plan_handler consumes the actual
  maintenance plan, extracts task codes, discovers workforce via the
  registry, and claims + assigns — the repair scene is reachable from an
  NMC event end to end.
- **Inputs preserved (gap 3).** Claims store the work order inputs; the
  reserve's seq-2 assignment carries them verbatim.
- **Stale failures are total no-ops (gap 4).** The failure handler consumes
  a stale event with zero writes; the commit-time guard bundles the audit +
  seq-2 assignment INTO the reassignment application, so a plan that turns
  stale commits nothing (no block, no audit, no output).
- **Supply fully data-backed.** Approval is discrepancy-specific
  (is_part_approved_for); shipment_status/eta_days come from the synthetic
  supply-chain facts (HYD-ACT-4402: delayed/21 — the spine's Scene 3 driver);
  unknown parts must report not_ordered/0. Prompt bumped to v3.
- **Safety validates every proposed action (AGT-4):** plans, sourcing
  reports, and rosters, each against its data-backed compliance engine;
  reason codes generalized to ACTION_APPROVED/ACTION_VETOED.

## 2026-08-24 — Day 4 race closeout (entrant HOLD round 2)

- **Owned-effects bundle (blocker 1).** A specialist's domain output, audit,
  and status ride ONE transactional ownership guard
  (TxnWrites.owned_effects): reassigned mid-flight -> nothing commits. The
  outbox can never hold competing rosters; only the current owner completes.
- **Conditional failure disposition (blocker 2).** Non-reserve failures use
  TxnWrites.failure_disposition: the package is transactionally re-read at
  commit — completed/stale -> consume with zero effects; still-assigned ->
  package FAILED + instance FAILED + workflow BLOCKED + escalation audit in
  one transaction. Both interleavings proven on the real emulator with
  thread barriers.
- **Agent output IDs are source-event-scoped**: deterministic per consumed
  message (redelivery idempotent), distinct across different messages about
  the same package (found when two Safety verdicts collided).
- **Safety rider**: engines take the workflow discrepancy (read from the
  work-package record); sourcing_report bumped to v3 under ICD-3 with
  discrepancy-specific part_approved semantics; wrong-discrepancy plan and
  actual roster verdicts tested.
- All bundle-nested audit/outbox messages are contract-validated BEFORE the
  transaction, like every other effect.
