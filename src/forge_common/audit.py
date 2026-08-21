"""Append-only audit event construction (AUD-1, AUD-3).

Every audit record is a full, contract-valid ``audit_event.v2`` bus message
stored under ``workflows/{id}/audit/{event_id}``. It carries ``observed_at``
(real system time) and ``effective_at`` (Logical Clock day) so reconstruction
across simulated time jumps is unambiguous (AUD-3). Writes happen only inside
the same transaction as the state change they describe (AUD-2) — see
``state.transition_workflow`` and ``bus.process_message``.
"""

from __future__ import annotations

import datetime
import json
from typing import Any

from forge_common.contracts import validate_message
from forge_common.messages import build_envelope, deterministic_event_id, sha256_hex


def now_iso() -> str:
    return (
        datetime.datetime.now(datetime.UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def build_audit_event(
    *,
    workflow_id: str,
    trace_id: str,
    agent_identity: str,
    event_kind: str,
    reason_code: str,
    input_obj: Any,
    output_obj: Any,
    effective_at: int,
    observed_at: str | None = None,
    state_after: str | None = None,
    detail: str | None = None,
    work_package_id: str = "wp-none",
) -> dict[str, Any]:
    """Build a contract-valid audit_event.v2 message (AUD-1)."""
    observed = observed_at or now_iso()
    payload: dict[str, Any] = {
        "event_kind": event_kind,
        "agent_identity": agent_identity,
        "input_sha256": sha256_hex(input_obj),
        "output_sha256": sha256_hex(output_obj),
        "reason_code": reason_code,
        "observed_at": observed,
        "effective_at": effective_at,
    }
    if state_after is not None:
        payload["state_after"] = state_after
    if detail is not None:
        payload["detail"] = detail
    event_id = deterministic_event_id(
        "audit", workflow_id, reason_code, payload["input_sha256"], observed
    )
    message = {
        "envelope": build_envelope(
            workflow_id=workflow_id,
            work_package_id=work_package_id,
            schema_version="audit_event.v2",
            event_id=event_id,
            trace_id=trace_id,
            idempotency_key=f"idem-audit-{event_id}",
        ),
        "payload": payload,
    }
    validate_message(message)
    return message


AUDIT_DETAIL_MAX = 1000  # audit_event.v2 payload.detail maxLength


def bounded_json_detail(obj: dict[str, Any], limit: int = AUDIT_DETAIL_MAX) -> str:
    """JSON detail string guaranteed to fit the audit_event.v2 cap.

    A maximum-length approval ``comment`` (1000 chars) would overflow the
    1000-char ``detail`` once JSON-wrapped: oversize comments are dropped
    first (flagged ``comment_omitted``), and the last resort is a minimal
    marker — the detail stays parseable JSON in every case.
    """
    text = json.dumps(obj, sort_keys=True)
    if len(text) <= limit:
        return text
    trimmed = dict(obj)
    approval = trimmed.get("approval")
    if isinstance(approval, dict) and "comment" in approval:
        approval = {k: v for k, v in approval.items() if k != "comment"}
        approval["comment_omitted"] = True
        trimmed["approval"] = approval
        text = json.dumps(trimmed, sort_keys=True)
        if len(text) <= limit:
            return text
    return json.dumps({"detail_omitted": True, "reason": "exceeds audit detail limit"})
