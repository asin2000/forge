# Contracts (ICD-1..4)

Versioned JSON Schemas (Draft 2020-12) governing every inter-agent message.
Schemas are written **before** either endpoint is implemented (ICD-1) and are
**immutable once merged**: changing a message shape means adding
`<type>.v<N+1>.schema.json`, never editing in place (ICD-3 — enforced by
`scripts/check_schema_versions.py`).

Every bus message is `{"envelope": ..., "payload": ...}`. The envelope
(`envelope.v1.schema.json`) carries the six mandatory fields of ICD-4:
`workflow_id`, `work_package_id` (`wp-none` when workflow-scoped),
`event_id`, `trace_id`, `idempotency_key`, `schema_version`. Each message
schema pins `schema_version` to its own type, so a message cannot validate
against the wrong contract.

Validation runs at publish **and** subscribe via
`forge_common.validate_message` (ICD-2). Nonconforming messages are rejected
and audited, never coerced.

| Schema | Producer → Consumer | Req |
|---|---|---|
| nmc_event.v1 | intake → Orchestrator | ORC-1 |
| work_package_assignment.v1 | Orchestrator → specialist | ORC-1, ORC-3 |
| maintenance_action_plan.v1 | Maintenance → Orchestrator | AGT-1 |
| sourcing_report.v1 | Supply → Orchestrator | AGT-2 |
| roster_assignment.v1 | Workforce → Orchestrator | AGT-3 |
| validation_verdict.v1 | Safety → Orchestrator | AGT-4 |
| quarantine_verdict.v1 | Cyber Trust → bus | SEC-1..4, AGT-5 |
| approval_request.v1 | Orchestrator → human gate | HUM-2 |
| approval_decision.v1 | human gate → Orchestrator | HUM-1 |
| agent_failure_event.v1 | platform/agents → Orchestrator monitor | ORC-4, AGT-7 |
| due_event.v1 | Logical Clock → Orchestrator | ORC-5 |
| audit_event.v1 | all → audit log | AUD-1, AUD-3 |

`state/workflow_state.v1.schema.json` is the **internal** Firestore workflow
document (ORC-5) — deliberately not registered as a bus message type.

`examples/` holds at least one valid instance per schema; CI-2 validates all
of them on every PR (`scripts/validate_contracts.py`).
