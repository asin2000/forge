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
