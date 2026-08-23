"""Firestore document layout and transaction helper (ORC-5, AUD-2, ICD-5).

Layout (all demo-scale collections; reconstruction reads these alone, AUD-2):

- ``system/logical_clock``                     {"logical_time": int}
- ``workflows/{workflow_id}``                  workflow_state.v3 document
- ``workflows/{workflow_id}/audit/{event_id}`` full audit_event.v2 message
- ``workflows/{workflow_id}/outbox/{event_id}`` {"message": ..., "published": bool}
- ``workflows/{workflow_id}/inbox/{idempotency_key}`` processed marker

``run_in_transaction`` works with the real google-cloud-firestore client and
with the buffered test double in ``tests/fake_firestore.py``: the real path
uses the library's transactional retry; the fake path calls the function and
commits its buffered writes only on success, so atomicity-coupling tests
(AUD-2) exercise the same code.
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from google.cloud import firestore
from jsonschema import Draft202012Validator, FormatChecker

_STATE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[2] / "contracts" / "state" / "workflow_state.v3.schema.json"
)

CLOCK_DOC = ("system", "logical_clock")


class StateDocInvalid(Exception):
    """A workflow state document failed schema validation (ORC-5)."""


@functools.lru_cache(maxsize=1)
def _state_validator() -> Draft202012Validator:
    schema = json.loads(_STATE_SCHEMA_PATH.read_text())
    return Draft202012Validator(schema, format_checker=FormatChecker())


def validate_state_doc(doc: dict[str, Any]) -> dict[str, Any]:
    errors = sorted(e.message for e in _state_validator().iter_errors(doc))
    if errors:
        raise StateDocInvalid("; ".join(errors[:5]))
    return doc


def txn_get_dict(txn: Any, ref: Any) -> dict[str, Any] | None:
    """Read one document inside a transaction, normalized to a dict.

    The real google-cloud-firestore ``Transaction.get(ref)`` returns a
    GENERATOR of snapshots, not a snapshot — every transactional read goes
    through this helper so both the real client and the test double resolve
    to ``dict | None``. (Found by adversarial review; the fake's get now
    also yields a generator so tests exercise the real shape.)
    """
    result = txn.get(ref)
    if not hasattr(result, "to_dict"):
        result = next(iter(result), None)
    if result is None:
        return None
    data = result.to_dict()
    return data if data else None


def workflow_ref(db: Any, workflow_id: str) -> Any:
    return db.collection("workflows").document(workflow_id)


def approval_ref(db: Any, workflow_id: str, approval_id: str) -> Any:
    """Authoritative approval_decision record written by the HUM-1 surface."""
    return workflow_ref(db, workflow_id).collection("approvals").document(approval_id)


def consumed_approval_ref(db: Any, workflow_id: str, approval_id: str) -> Any:
    """Marker consumed atomically with a gated transition (HUM-1 replay guard)."""
    return workflow_ref(db, workflow_id).collection("approvals_consumed").document(approval_id)


def audit_ref(db: Any, workflow_id: str, event_id: str) -> Any:
    return workflow_ref(db, workflow_id).collection("audit").document(event_id)


def completion_evidence_ref(db: Any, workflow_id: str) -> Any:
    """Single marker doc holding the LATEST roster (completion) verdict
    consumed pre-resume — deterministic path so the due handler's committing
    transaction can read it without a query (release-gate race closure)."""
    return workflow_ref(db, workflow_id).collection("completion").document("evidence")


def outbox_ref(db: Any, workflow_id: str, event_id: str) -> Any:
    return workflow_ref(db, workflow_id).collection("outbox").document(event_id)


def inbox_ref(db: Any, workflow_id: str, idempotency_key: str) -> Any:
    return workflow_ref(db, workflow_id).collection("inbox").document(idempotency_key)


def work_package_ref(db: Any, workflow_id: str, work_package_id: str) -> Any:
    """Exclusive-ownership record for one work package (ORC-1)."""
    return workflow_ref(db, workflow_id).collection("work_packages").document(work_package_id)


def clock_ref(db: Any) -> Any:
    return db.collection(CLOCK_DOC[0]).document(CLOCK_DOC[1])


def run_in_transaction(db: Any, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
    """Run ``fn(transaction, *args, **kwargs)`` atomically (AUD-2)."""
    txn = db.transaction()
    if isinstance(txn, firestore.Transaction):
        wrapped = firestore.transactional(lambda t: fn(t, *args, **kwargs))
        return wrapped(txn)
    result = fn(txn, *args, **kwargs)
    txn.commit()
    return result
