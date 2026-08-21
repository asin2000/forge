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
