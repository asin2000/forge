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
