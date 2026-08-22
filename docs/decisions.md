# Design decisions

> Provenance note: headers carry the MERGE date plus the plan's
> build-day label — the build ran ahead of the calendar schedule.

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

## 2026-08-21 (build-day 2) — Day 2 core (branch `day2-state-and-messaging`)

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

## 2026-08-21 (build-day 2) — Day 2 adversarial review fixes (same branch)

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

## 2026-08-21 (build-day 2) — Day 2 verification gate (branch `day2-verification-gate`)

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

## 2026-08-21 (build-day 2) — Corrections to earlier entries (v1.2 supersessions)

Two statements in the Day-1 entry are superseded and should not be relied on:
- "region `us-central1`" for the model: `gemini-3.5-flash` runs at the `us`
  multi-region per the DAT-1 residency map (`infra/residency.yaml`);
  `us-central1` remains correct for Cloud Run/Firestore/Pub/Sub/Model Armor.
- "the envelope carries the six mandatory ICD-4 fields": the active envelope
  is v2 with EIGHT fields (adds `data_origin`, `trust_state`; `trace_id` is
  the 32-hex OTel mirror).
Also noted for accuracy: emulator-verified (PR #3) is not deployment-verified
— live GCP verification is scheduled Day 6–7 (Lane 2, CI-6).

## 2026-08-21 (build-day 3) — Day 3: Orchestrator, first specialists, registry discovery

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

## 2026-08-21 (build-day 3) — Day 3 completion (entrant HOLD): real ADK + five gap fixes

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

## 2026-08-21 (build-day 4) — Day 4: repair loop, monitoring, data-backed domain facts

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

## 2026-08-21 (build-day 4) — Day 4 closeout (entrant HOLD): package lifecycle + real flow

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

## 2026-08-21 (build-day 4) — Day 4 race closeout (entrant HOLD round 2)

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

## 2026-08-21 (build-day 5) — Day 5: quarantine-first screening pipeline (SEC-1..4)

- **Quarantine store**: canonical gs:// URIs per the verdict contract;
  demo/emulator bytes live in the Firestore `quarantine` collection under
  the same IDs; Day 6 deploy binds the actual GCS bucket with bucket-level
  IAM (Cyber Trust SA read-only) — the AGT-5 access negative test is a
  Lane 2 live-IAM check, since collection-level isolation is not
  server-SDK-enforceable inside one Firestore database.
- **`released` semantics** (frozen contract, documented interpretation):
  True = the pipeline COMPLETED without error (SEC-2); flagged/malicious
  results still publish (released:true) so Safety can reject the
  identifier — the spine's Scene 1 requires exactly this. Errors →
  released:false, and the schema itself forbids safe_metadata then.
- **Classifier constraints (SEC-3/SEC-4)**: tool-less ADK agent
  (structural test asserts zero tools); candidate_part_identifier is
  schema-constrained to the identifiers the bounded parser actually
  extracted — the model cannot invent one (invention exhausts retries and
  fails closed).
- **Safety consumes verdicts**: VALIDATORS gains quarantine_verdict.v2 —
  SP-SEC-004 (flagged/unreleased content cannot drive actions) + SP-PART-001
  (candidate evaluated against the trusted registry, never the document).
- Model Armor adapter mirrors the proven smoke REST call; live exercised by
  the smoke + Lane 2 (CI-6); unit/emulator stub it.

## 2026-08-21 (build-day 5) — Day 5 corrective (entrant HOLD)

- **Model Armor fail-closed for real**: invocationResult PARTIAL/FAILURE,
  missing fields, malformed JSON, transport/timeout/auth failures all
  RAISE; ADC replaces the gcloud shell-out (Cloud Run service identity);
  injectable transport/token for the 8-shape test matrix. Live bug found
  during the fix: category matching used a substring test that also matched
  NO_MATCH_FOUND — now an exact-value walk; verdict flags on aggregate OR
  any per-filter match.
- **Live defense-in-depth finding** (captured in verification): the
  injection diluted in benign vendor prose evades the armor filter even at
  LOW_AND_ABOVE, while the pure probe flags — and the LIVE gemini
  classifier catches the dilution (malicious, 1.0, candidate
  VND-ACT-9901). This is the Scene-1 narrative, proven live.
- **quarantine_verdict.v3**: separates screening_complete /
  raw_disposition(quarantined) / metadata_release(released|withheld) —
  no more released:true beside malicious; schema conditionals enforce
  withheld-forbids-metadata and incomplete-forces-withheld.
- **Trusted discrepancy context**: Safety resolves the workflow discrepancy
  from orchestrator-written work-package records ONLY (wp-none falls back
  to scanning the workflow's packages); part checks are STRICT — no
  discrepancy context is itself an SP-PART-001 violation, never a global
  fallback (the wrong-discrepancy hole is closed on the quarantine path).
- **GCS quarantine store is real**: raw bytes live in the bucket
  (gs://forge-quarantine-<project>, created; live put/read round-trip
  captured); Firestore metadata NEVER carries raw_text;
  FirestoreQuarantineStore is emulator-only. Per-SA IAM negative test lands
  with the deployed identities (Day 6/7, Lane 2). New dependency (DFT-3):
  google-cloud-storage — SEC-1 quarantine object storage.
- **Re-ingest integrity**: identical re-ingest idempotent (returns the
  STORED record); hash/workflow/source mismatch -> QuarantineConflict +
  REINGEST_CONFLICT audit.
- Riders: clear exception boundary (AgentOutputMalformed / ScreeningError /
  unexpected — all fail closed); prompt BEGIN/END markers neutralized in
  document text (escape test); count corrected per entrant: 135 was right.

## 2026-08-21 (build-day 5) — Final corrective: two fail-closed gaps

- **invocationResult must be EXPLICIT SUCCESS**: the previous
  `.get("invocationResult", "SUCCESS")` default silently treated a response
  missing the field as fully screened — a fail-open. Missing/UNSPECIFIED
  now raise like PARTIAL/FAILURE (regressions added).
- **GCS write order inverted to object-first**: upload with
  `if_generation_match=0` (single winner under concurrency), hash-verify an
  existing object before adopting it, THEN create/validate Firestore
  metadata persisting the object generation; reads verify downloaded bytes
  against the recorded SHA (tamper -> screening fails closed, audited). An
  orphaned object after a Firestore failure is safe and self-repairing on
  retry; stranded metadata can no longer exist. Five-scenario test battery
  (upload failure, retry, orphan repair, different-content race, tamper).
- Riders: SEC-2/SEC-4 traceability now cite quarantine_verdict.v3; the live
  smoke is labeled a COMPONENT smoke (end-to-end = Lane 2); decision-log
  headers carry merge date + build-day label (everything so far merged
  2026-08-21).

## 2026-08-21 (build-day 5) — Atomic quarantine ingest (final blocker)

- **Metadata + DOCUMENT_QUARANTINED audit commit in ONE transaction.**
  Stores now only ESTABLISH the raw object (GCS: generation-zero
  precondition; emulator store: embeds bytes in the record) and commit
  nothing; ingest_document owns the single transaction that
  creates-or-validates metadata AND creates the audit. The audit's event ID
  is DETERMINISTIC in (workflow, doc, sha), so an identical retry repairs a
  missing audit (orphan object, legacy metadata-without-audit, crash
  debris) without ever duplicating one — no metadata-only window can exist
  (AUD-2 holds at the quarantine boundary). Fault-injection, orphan-repair,
  legacy-repair, and no-duplicate regressions on the fake; the
  repair/recovery matrix also runs on the REAL Firestore emulator.
- Riders: the sequential race test is renamed precondition-loser (true
  concurrency lives in the emulator barrier tests); the live component
  smoke uses a FRESH doc id and asserts the captured GCS generation with a
  generation-pinned read (re-captured).

## 2026-08-21 (build-day 5) — Conflict-audit boundary (PR #15 regression)

- Moving establish_object ahead of the try/except in the atomic-ingest
  rework silently de-audited the GCS object-byte-mismatch conflict. Both
  conflict locations (object bytes at establish; workflow/source in the
  transaction) now route through one _audit_reingest_conflict helper;
  the precondition-loser test asserts exactly one REINGEST_CONFLICT audit.
- Smoke hygiene: the live component smoke ingests via the PRODUCTION
  atomic path (no direct metadata writes — the smoke must not create the
  metadata-without-audit state the fix prohibits), asserts the
  DOCUMENT_QUARANTINED audit, and deletes its own records; earlier smoke
  debris (3 unaudited records + objects) purged from the live project.
- Honest-scope fix: removed the claim that emulator barrier tests cover
  GCS ingestion concurrency — the GCS conflict test is correctly
  sequential (precondition loser).

## 2026-08-21 (build-day 6) — Human gate, dashboard, spine gate handlers, Gemma no-go

- HUM-1 approval surface: FastAPI service (services/dashboard) behind Cloud
  Run IAM/IAP. Approver identity derives ONLY from the authenticated
  principal (IAP header, else Google-verified ID token); client-supplied
  approver_identity is rejected with 400. record_approval_decision is the
  single writer and now (enqueue_outbox=True) creates the bus copy of the
  decision in the SAME transaction as the authoritative record and its
  APPROVAL_RECORDED audit.
- HUM-2 producer: approval_request.v2 was a contract with no producer. The
  orchestrator verdict handler now composes the full decision record
  (source refs, extracted facts, cited SP rules, constraints, confidence,
  alternatives, versions) IN THE SAME COMMIT as the transition into the
  awaiting state — a workflow can never sit at a gate without its evidence.
  Composition is a deterministic function of the trusted Safety verdict
  (versions.model_id: deterministic-policy); the model-produced evidence
  keeps its own provenance via source_refs into the audit trail.
- Spine gate handlers: validation_verdict.v2 routes VALIDATING->
  AWAITING_SCHEDULE_APPROVAL (schedule_override request; resume day =
  clock + sourcing ETA; missing sourcing evidence escalates instead of
  fabricating an ETA) and ASSEMBLY_RESUMED->AWAITING_RELEASE_APPROVAL
  (equipment_release). approval_decision.v2 applies the human decision:
  approved release consumes the recorded approval once (RELEASED); a
  rejected release returns to rework; stale/replayed decisions are audited
  no-ops, never NACK loops.
- REG-4 catalog: dashboard renders definitions + instances with health
  DERIVED from heartbeat staleness at read time (never stored).
- CI-9 config gate: scripts/check_config.py cross-checks registry.yaml
  (role coverage, ACTIVE contracts, per-role identity pattern) and every
  region literal in infra scripts against the DAT-1 location map. Seventh
  CI job.
- PLT-5 decision gate (today): NO-GO on Gemma. Live probe from the project
  hit 404 on every serverless candidate (gemma-3-27b-it / -12b-it /
  google/gemma-3-27b-it at us, us-central1, global) — Gemma on Vertex
  requires a dedicated Model Garden GPU endpoint (quota, cost, new DAT-1
  entry). Per PLT-5, the tool-less gemini-3.5-flash classifier satisfies
  SEC-3 and the Gemma model-integration bonus route is forfeited.
- Smoke-cleanup rider (PR #16 GO): smoke_quarantine_live.py now uses a
  UNIQUE workflow id per run and registers cleanup via atexit (try/finally
  equivalent), deleting only the current run's records even when an
  assertion fails mid-smoke.
- Live production finding (Cloud Run revision 00001): Cloud Run IAM
  verifies the caller ID token and forwards it with the SIGNATURE STRIPPED
  — in-container re-verification is structurally impossible
  (MalformedError), and the unhandled failure was a 500. Fixed: verifier
  failures map to 401 (never 500), and behind Cloud Run IAM the deploy
  sets TRUST_PLATFORM_AUTH=1 so identity is read from the
  platform-validated token claims (issuer/verified-email/expiry still
  enforced) — safe only under --no-allow-unauthenticated. Three unit
  regressions including the flag-off path.
- deploy.sh clean-project findings: SA creation quota is 5/minute (429) —
  the script now backs off and retries; impersonation-based IAM testing
  needs iamcredentials.googleapis.com (harness prerequisite only).

## 2026-08-21 (build-day 6, PR #17 HOLD fixes) — Auth modes, trace continuity, CI-9 vs reality

- HOLD blocker 1 (HUM-1 spoofable): the plain x-goog-authenticated-user-email
  header was accepted as identity and checked BEFORE the bearer token —
  Google's IAP guidance says that header must never be relied upon (it is
  client-forgeable; only the signed x-goog-iap-jwt-assertion proves
  identity). auth.py now runs in ONE explicit deploy-set mode
  (FORGE_AUTH_MODE): cloudrun-iam (bearer only, platform-validated claims;
  plain header ignored), iap (ONLY the signed assertion, verified against
  IAP public keys with configured IAP_AUDIENCE + IAP issuer, fail-closed
  without an audience), verify (full Google verification; local/dev).
  Unknown mode refuses everything. Forged-header negatives at the auth
  layer, the app layer (401 + nothing recorded), on the emulator loop
  (forged header rides EVERY call; approver comes from the credential), and
  live against Cloud Run. New read-only /api/whoami lets ops confirm the
  derived principal.
- HOLD blocker 2 (trace continuity): the decide endpoint minted a fresh
  uuid5 trace id, a second application trace identifier (OBS-1/ICD-4
  violation). The decision envelope now carries the approval_request's
  trace_id verbatim — which is the workflow trace the verdict rode in on.
  Exact-equality tests: request == decision == authoritative record ==
  every audit event, on the fake and on the real client.
- HOLD blocker 3 (CI-9 passed an incomplete check): the gate now validates
  architecture/manifest.yaml against reality — code on disk may not claim
  "planned"; service accounts must be the resolved per-role ids (stale -sa
  names fail); "deployed" requires a real Cloud Run endpoint in the DAT-1
  region and non-deployed services may carry none; every services/ dir
  needs an entry; Cloud Logging location must match the DAT-1 map; registry
  endpoints must be null (not deployed) or a real URL — the "deploy-time"
  placeholder fails — and must agree with the manifest's status. Run
  against the stale state it produced 20 failures (captured in the PR);
  manifest and registry were then corrected to the true state (six agents
  implemented/not-deployed, dashboard deployed with its real endpoint,
  observability pinned). Endpoint NETWORK reachability is deliberately out
  of CI scope (hermetic); it lives in the verification docs.

### Recorded Day-7 corrections (from the same review)

- PLT-6 incomplete: deploy.sh requires infra/setup-gcp.sh first and deploys
  only the dashboard service. Day 7 must fold the setup steps in (or chain
  them) and deploy the agent workers, then run the clean-project
  deployment test PLT-6 verification requires.
- AGT-6 not yet scoped: every service account currently receives
  project-wide roles/pubsub.editor and roles/datastore.user. Day 7 must
  replace these with per-topic/per-subscription Pub/Sub bindings and
  document the Firestore scoping limitation honestly (datastore.user is
  project-level; per-collection isolation is not an IAM primitive — state
  the compensating controls). The bucket test proves the Cyber Trust
  storage boundary ONLY; it is not evidence of scoped IAM elsewhere.
- Adversarial verification pass (4 independent reviewers) before push: all
  three closures confirmed; three of its findings fixed same-day —
  (a) cloudrun-iam claim validation now maps ANY failure to 401 (non-dict
  payloads / non-numeric exp previously escaped as 500; unreachable in
  production since the platform pre-verifies, but it contradicted the
  never-500 invariant; regression tests added); (b) the trace-equality test
  now re-asserts a single trace AFTER the decision handler consumes the
  gate, and the emulator loop asserts it after RELEASED — a handler minting
  a second trace id can no longer pass the suite; (c) the CI-9
  planned-with-code guard keys off the services/ directory itself, so an
  emptied source list cannot smuggle a planned status past the gate.
  Additional Day-7 note from the same pass: when production entrypoints
  land for clock.emit_due_events and the monitor cycle, their trace ids
  must derive from the workflow root trace (today only tests call them,
  correctly) — same second-trace-identifier class as blocker 2.

## 2026-08-21 (build-day 7) — Full spine, OBS, fleet deployment, Lane 2

- Spine completion: PLANNING->VALIDATING on plan arrival (replans
  REVALIDATE without re-claiming the standing staffing package — the
  veto->rework loop was a claim-collision DLQ dead end before);
  due_event.v2 handler (SUSPENDED->ASSEMBLY_RESUMED, stale-safe).
- Root-trace derivation (Day-6 rider): workflow_state.v2 stores the
  workflow's root trace_id; the Logical Clock's due events and the ORC-4
  monitor's timeout events derive their trace from the WORKFLOW doc, never
  a caller. emit_due_events(db) lost its trace parameter entirely.
- ORDER-INDEPENDENT gate routing (live push delivery makes arrival order
  arbitrary — three real races the ordered tests had masked):
  (1) verdict-before-sourcing-report used to ESCALATE+BLOCK on missing
  evidence; now the verdict is HELD and whichever of verdict/report lands
  LAST opens the schedule gate (orchestrator consumes sourcing_report.v3).
  (2) verdicts route BY SUBJECT (plan/sourcing -> schedule gate; roster ->
  release gate) — a late plan verdict can never open the release gate.
  (3) completion evidence arriving pre-resume (roster verdict while
  suspended) is recorded and RE-KEYED by the due handler at resume in the
  same commit as the transition. The worker + emulator spine tests now run
  with IMMEDIATE delivery (live shape) — no scene-timing deferral.
- Live-run finding (run 1 vs run 2): a deterministic data-engine VETO of
  the delayed sourcing report blocked run 1 purely because it arrived
  post-resume (run 2: same veto, earlier arrival, benign stale no-op).
  Post-resume vetoes now block ONLY on completion (roster) evidence.
- OBS-1/OBS-2: forge_common/otel.py — W3C traceparent/tracestate in
  Pub/Sub attributes (authoritative), envelope trace_id a read-only
  mirror seeding the synthetic parent when no W3C context arrives, ONE
  metadata-only consume span per delivery (ADK spans nest under it),
  Cloud Trace exporter gated on FORGE_TRACE_EXPORT=cloud with 100%
  sampling. CI-3's inject/extract test + OBS-2 attribute-allowlist tests.
- Worker runtime (services/worker.py): one Cloud Run service per role,
  filtered ORDERED push subscriptions (OIDC = role SA), delivery
  semantics 204-ack / 429-claim-retry / 200-terminal-reject-audited /
  500-DLQ. The full spine runs through the worker push surface in unit
  AND emulator tests, and LIVE (docs/verification/2026-08-21-day7-live.md).
- PLT-6 complete: deploy.sh chains setup-gcp.sh and stands up EVERYTHING
  (SAs, scoped IAM, bus+DLQ, regional log bucket, registry, one image,
  5 workers + dashboard, push subscriptions). Live production findings
  encoded: lockfile must carry the trace exporter; Pub/Sub FILTERS CAP AT
  256 CHARS (routing now stamps to_<role> attributes from the canonical
  forge_common.pubsub.ROUTING map, CI-9 cross-checks it against registry
  consumes); dead-lettering needs BOTH service-agent grants (with only
  DLQ-publisher, exhausted messages redeliver forever); span export needs
  roles/cloudtrace.agent.
- AGT-6 narrowed (Day-6 rider): per-topic publisher bindings replace
  project-wide pubsub.editor (removed live); Firestore's per-collection
  limitation documented in deploy.sh + README compensating controls.
- Cloud Logging regionalized (Day-6 rider): forge-logs bucket in
  us-central1, _Default sink redirected; _Required exception disclosed in
  the README (DAT-3 section added).
- DAT-2 closure: data_origin/trust_state now rendered in the dashboard
  audit trail (they were displayed nowhere — requirement text says SHALL).
- Traceability flipped to STRICT with a dated pending_allowed list
  (CI-6 WIF half Day 8, CI-8 Day 9, DFT-4 Day 8, SUB-* Days 9-10);
  CI-5 Vulture gate added as the EIGHTH CI job (clean).
- Demo hygiene lessons: failure evidence must outlive cleanup (the live
  driver dumps the full trail before deleting anything); live pollers use
  reached-or-passed state semantics (equality deadlocks on a missed
  intermediate); live cleanup re-seeds agent instance states.
- Live-run root cause (runs 1 & 3 vs run 2): UNGROUNDED MAINTENANCE PLANS.
  The live planner invented task codes (e.g. outside TC-101/102/201); no
  technician holds a qualification for an invented code, so every roster
  failed its data check, the primary AND reserve exhausted (AGT-7), and
  ORC-3 blocked with RESERVE_UNAVAILABLE — guardrails all behaved exactly
  as specified; the defect was the planner's freedom to schedule
  unexecutable work. Fix in the same data-backed pattern as Supply and
  Workforce: prompts/maintenance_action_plan.v2.md carries the STAFFABLE
  task catalog (derived from qualifications.yaml), the maintenance
  payload_check rejects off-catalog codes at the source (AGT-7 retry sees
  the catalog), and safety's plan engine independently vetoes unstaffable
  plans (SP-QUAL-001) — belt and suspenders, both regression-tested.
  **Pattern (now three times proven): every fact a model asserts must have
  a registry it is checked against — parts, qualifications, and now the
  work breakdown itself.**

- Pre-push adversarial verification (5 reviewers over Day 7) — all real
  findings fixed same-day: (1) OBS-2/DAT-3: google-adk captures FULL
  prompts+responses into span attributes BY DEFAULT — forced off at otel
  import and in every service env, leak-tested with sentinels; (2) the
  SDK's auto status wrote str(exc) — which can embed payload text — into
  exported status.description: statuses now carry the exception TYPE only;
  (3) a present-but-garbage traceparent orphaned the hop silently: mirror
  fallback + explicit flag; (4) a vetoed ROSTER verdict pre-resume
  misrouted as a plan veto (or vanished): completion evidence (approved OR
  vetoed) now records via a transactional marker and re-keys at resume,
  blocking there if vetoed; (5) lost-wakeup race between the roster
  verdict and the resume: BOTH sides now act in their committing
  transactions (the verdict commit re-reads the workflow doc; the due
  commit reads the marker) — whichever commits second sees the other;
  (6) the re-key id now folds the resume epoch (second suspensions no
  longer AlreadyExists-crash-loop); (7) input-boundary contract rejections
  (audited) ack, but OUTPUT-side violations now 500 to the DLQ — never
  silent loss; (8) push subscriptions carry a retry policy (60s-600s) so
  redelivery credit outlives the 120s claim lease; (9) ordered-publish
  failures resume the paused ordering key; (10) /tick heartbeat (Cloud
  Scheduler, every minute) runs the ORC-4 monitor, ORC-5 due emission, and
  an outbox sweep — the monitor and clock now RUN as deployed, and
  dashboard decisions always drain; (11) deploy.sh grants the dead-letter
  subscriber half AFTER the subscriptions exist (clean-project ordering),
  setup provisions the Pub/Sub service agent + compute/scheduler APIs;
  (12) REG-5's missing lifecycle audits implemented (definition-change
  audits on load, IDLE->ACTIVE activation audited in the claim txn);
  (13) README/verification-doc honesty sweep (us multi-region for Gemini,
  real deploy command + prerequisites, Firestore compensating controls,
  PLT-6 clean-project claim softened and kept pending until the Day-8
  fresh-project run). Silent-swallow of a replan racing its veto is now
  AUDITED (PLAN_REVISION_HELD); automating replans requires an in-band
  re-trigger first.

## 2026-08-22 (freeze acceptance) — 10x run caught an ungrounded-parts stall

The first 10-consecutive live acceptance run failed at run 9 (8/10 had
passed): the live gemini planner produced part numbers not approved for
DSC-0042 (ACTUATOR-HYD-GX12, HYD-FLUID-H15); Safety CORRECTLY vetoed
(SP-PART-001), the workflow returned to PLANNING, and — with no automated
replan producer — the spine stalled. Root cause: Maintenance was grounded
in the staffable-task catalog (Day-7 fix) but NOT in the approved-parts
registry, unlike Supply. Fix (same pattern, now complete): the maintenance
prompt (v3) carries the approved parts for THIS discrepancy, and the
payload_check runs plan_violations at source so a straying model retries
(AGT-7) against the approved list instead of shipping a plan doomed to a
downstream veto. The veto path itself remains correct and tested — this
just stops the happy path from depending on the model guessing an approved
part. **Third instance of the invariant: every fact a model asserts —
parts, qualifications, task codes — must be checked against a registry, and
the registry belongs IN THE PROMPT, not just in the downstream validator.**
The acceptance run did exactly its job: 8 lucky runs hid a reliability gap
that the 9th exposed. Candidate advances; the 10x restarts from zero.
