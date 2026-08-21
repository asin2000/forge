"""Safety & Policy Agent (AGT-4): validates EVERY proposed action against
the synthetic procedures library — maintenance plans, sourcing reports, and
roster assignments. A veto is not human-overridable.

The verdict is DATA-BACKED: the compliance engine (plan_violations /
sourcing_violations / roster_violations) computes violations from the
procedures library, approved-parts registry, and qualification records; the
model drafts the verdict message, and the payload_check refuses any verdict
that contradicts the computed violations.
"""

from __future__ import annotations

import json
from typing import Any

from forge_common import layout, synthetic_data
from forge_common.agent_base import StructuredAgent
from forge_common.audit import build_audit_event
from forge_common.bus import TxnWrites
from forge_common.clock import read_clock

ROLE = "safety"
AGENT_ID = "agent-safety-01"
PROMPT_FILE = "safety_validation.v1.md"
OUTPUT_SCHEMA = "validation_verdict.v2"


#: schema_version -> compliance engine (AGT-4: every proposed action).
VALIDATORS = {
    "maintenance_action_plan.v2": synthetic_data.plan_violations,
    "sourcing_report.v3": synthetic_data.sourcing_violations,
    "roster_assignment.v2": synthetic_data.roster_violations,
    "quarantine_verdict.v2": synthetic_data.verdict_violations,
}


def make_validation_handler(db: Any, model: Any):
    """Bus handler validating every proposed action (fan-out subscriber)."""

    def handle(message: dict[str, Any], writes: TxnWrites) -> None:
        envelope = message["envelope"]
        engine = VALIDATORS.get(envelope["schema_version"])
        if engine is None:
            return  # not a proposed action; consumption recorded, no verdict
        plan = message["payload"]
        wp_snapshot = layout.work_package_ref(
            db, envelope["workflow_id"], envelope["work_package_id"]
        ).get()
        wp = wp_snapshot.to_dict() if hasattr(wp_snapshot, "to_dict") else wp_snapshot
        discrepancy = (wp or {}).get("inputs", {}).get("discrepancy_code")
        violations = engine(plan, discrepancy_code=discrepancy)

        def check(payload: dict[str, Any]) -> list[str]:
            expected = "vetoed" if violations else "approved"
            errors = []
            if payload["verdict"] != expected:
                errors.append(
                    f"verdict {payload['verdict']} contradicts the compliance "
                    f"engine ({expected}; {len(violations)} violation(s)) (AGT-4)"
                )
            missing = {rule for rule, _ in violations} - set(payload.get("rule_refs", []))
            if missing:
                errors.append(f"rule_refs missing violated rules: {sorted(missing)}")
            if payload["subject_event_id"] != envelope["event_id"]:
                errors.append("subject_event_id does not reference the reviewed plan")
            return errors

        agent = StructuredAgent(agent_id=AGENT_ID, role=ROLE, model=model)
        result = agent.run(
            prompt_file=PROMPT_FILE,
            variables={
                "subject_event_id": envelope["event_id"],
                "plan_json": json.dumps(plan, indent=1)[:4000],
                "rules_excerpt": json.dumps(list(synthetic_data.procedures().values())),
                "violations_json": json.dumps(
                    [{"rule_ref": r, "reason": why} for r, why in violations]
                ),
            },
            schema_version=OUTPUT_SCHEMA,
            workflow_id=envelope["workflow_id"],
            work_package_id=envelope["work_package_id"],
            trace_id=envelope["trace_id"],
            source_event_id=envelope["event_id"],
            payload_check=check,
        )
        writes.outbox_messages.append(result)
        failed = result["envelope"]["schema_version"] == "agent_failure_event.v2"
        vetoed = not failed and result["payload"]["verdict"] == "vetoed"
        writes.audit_events.append(
            build_audit_event(
                workflow_id=envelope["workflow_id"],
                trace_id=envelope["trace_id"],
                agent_identity=AGENT_ID,
                event_kind="failure" if failed else ("veto" if vetoed else "decision"),
                reason_code=(
                    "SPECIALIST_MALFORMED"
                    if failed
                    else ("ACTION_VETOED" if vetoed else "ACTION_APPROVED")
                ),
                input_obj=plan,
                output_obj=result["payload"],
                effective_at=read_clock(db),
                work_package_id=envelope["work_package_id"],
            )
        )

    return handle


# Backward-compatible alias (Day 4 tests): plans are one of the validated kinds.
make_plan_validation_handler = make_validation_handler
