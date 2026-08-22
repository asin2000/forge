"""Orchestrator monitoring loop (ORC-4): detect specialist failures within
one monitoring cycle.

The cycle is 5 seconds in the demo environment (spec §2). Each cycle scans
ASSIGNED work packages; one whose assignment is older than
``timeout_seconds`` yields a synthesized ``agent_failure_event.v2``
(failure_kind ``timeout``) enqueued idempotently to the workflow outbox —
its document ID is deterministic in (work package, owner, seq), so repeated
cycles never duplicate the event. Consumption by
``make_failure_handler`` performs the ORC-3/ORC-4 disposition.
(Malformed-after-retries and contract violations are detected at the source
by StructuredAgent, which emits the failure event directly.)
"""

from __future__ import annotations

import datetime
from typing import Any

from forge_common import layout
from forge_common.audit import now_iso
from forge_common.contracts import validate_message
from forge_common.messages import (
    build_envelope,
    deterministic_event_id,
    deterministic_trace_id,
)

MONITORING_CYCLE_SECONDS = 5  # demo environment (spec §2)
DEFAULT_TIMEOUT_SECONDS = 30


def build_timeout_event(
    *,
    workflow_id: str,
    trace_id: str,
    work_package_id: str,
    role: str,
    agent_id: str,
    assignment_seq: int,
) -> dict[str, Any]:
    event_id = deterministic_event_id(
        "timeout", workflow_id, work_package_id, agent_id, str(assignment_seq)
    )
    message = {
        "envelope": build_envelope(
            workflow_id=workflow_id,
            work_package_id=work_package_id,
            schema_version="agent_failure_event.v2",
            event_id=event_id,
            trace_id=trace_id,
            idempotency_key=f"idem-timeout-{event_id[:18]}",
        ),
        "payload": {
            "role": role,
            "agent_id": agent_id,
            "failure_kind": "timeout",
            "attempts": 1,
            "detail": f"no domain output within {DEFAULT_TIMEOUT_SECONDS}s of assignment",
            "detected_at": now_iso(),
        },
    }
    validate_message(message)
    return message


def run_monitoring_cycle(
    db: Any,
    *,
    trace_id_for: Any = None,
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    now: str | None = None,
) -> list[str]:
    """One ORC-4 cycle. Returns work_package_ids newly flagged as timed out.

    Timeout events ride the WORKFLOW's root trace (state doc trace_id,
    OBS-1) by default — this unattended producer never mints its own.
    ``trace_id_for(workflow_id)`` remains injectable for tests; ``now`` is
    injectable for tests (ISO-8601, as ``audit.now_iso``).
    """
    current = now or now_iso()
    cutoff = (
        (
            datetime.datetime.fromisoformat(current.replace("Z", "+00:00"))
            - datetime.timedelta(seconds=timeout_seconds)
        )
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    flagged: list[str] = []
    for wf_snapshot in db.collection("workflows").stream():
        workflow = wf_snapshot.to_dict() if hasattr(wf_snapshot, "to_dict") else wf_snapshot
        workflow_id = workflow["workflow_id"]
        packages = layout.workflow_ref(db, workflow_id).collection("work_packages")
        for snapshot in packages.stream():
            wp = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
            if wp.get("status") != "ASSIGNED":
                continue
            reference = wp.get("reassigned_observed_at") or wp.get("assigned_observed_at", "")
            if reference > cutoff:
                continue
            trace = (
                trace_id_for(workflow_id)
                if trace_id_for is not None
                else workflow.get("trace_id") or deterministic_trace_id(workflow_id)
            )
            message = build_timeout_event(
                workflow_id=workflow_id,
                trace_id=trace,
                work_package_id=wp["work_package_id"],
                role=wp["role"],
                agent_id=wp["owner_instance_id"],
                assignment_seq=int(wp.get("assignment_seq", 1)),
            )
            eid = message["envelope"]["event_id"]

            def _enqueue(txn: Any, wid: str = workflow_id, e: str = eid, m: dict = message) -> bool:
                if layout.txn_get_dict(txn, layout.outbox_ref(db, wid, e)):
                    return False
                txn.create(
                    layout.outbox_ref(db, wid, e),
                    {"message": m, "published": False, "enqueued_at": now_iso()},
                )
                return True

            if layout.run_in_transaction(db, _enqueue):
                flagged.append(wp["work_package_id"])
    return flagged
