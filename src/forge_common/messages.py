"""Envelope construction and deterministic identifiers (ICD-4, OBS-1, ICD-5).

The envelope's ``trace_id`` is a read-only mirror of the 32-hex OpenTelemetry
trace ID (OBS-1); until OTel init lands, callers pass the workflow's root
trace ID through. Deterministic event IDs make repeated emission of the same
logical event collide on the same document ID, which is what makes due-event
double-fire idempotent (ORC-5).
"""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

_EVENT_NS = uuid.UUID("6ba7b810-9dad-11d1-80b4-00c04fd430c8")  # uuid.NAMESPACE_DNS


def deterministic_event_id(*parts: str) -> str:
    """Stable UUID for a logical event; same parts -> same ID."""
    return str(uuid.uuid5(_EVENT_NS, "forge:" + ":".join(parts)))


def deterministic_trace_id(*parts: str) -> str:
    """Stable 32-hex trace ID for workflow-originated traces (OBS-1 shape)."""
    return uuid.uuid5(_EVENT_NS, "forge-trace:" + ":".join(parts)).hex


def sha256_hex(obj: Any) -> str:
    """Canonical SHA-256 of a JSON-serializable object (AUD-1 hashes)."""
    canonical = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_envelope(
    *,
    workflow_id: str,
    schema_version: str,
    event_id: str,
    trace_id: str,
    idempotency_key: str,
    work_package_id: str = "wp-none",
    trust_state: str = "TRUSTED",
) -> dict[str, Any]:
    """ICD-4 envelope: eight mandatory fields, DAT-2 labels included."""
    return {
        "workflow_id": workflow_id,
        "work_package_id": work_package_id,
        "event_id": event_id,
        "trace_id": trace_id,
        "idempotency_key": idempotency_key,
        "schema_version": schema_version,
        "data_origin": "SYNTHETIC",
        "trust_state": trust_state,
    }
