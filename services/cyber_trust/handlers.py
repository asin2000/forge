"""Cyber Trust Agent (AGT-5): the quarantine-first screening pipeline
(SEC-1..SEC-4, PLT-4).

Pipeline per document: bounded parser → explicit Model Armor screening call
→ tool-less structured classifier (SEC-3; ``gemini-3.5-flash`` per PLT-5's
baseline, Gemma attempted in parallel for the bonus at the Day-6 gate). ALL
THREE must complete before anything derived from the document reaches the
bus (SEC-1); ANY error leaves the document quarantined with an audited
failure and publishes nothing (SEC-2, fail closed).

Publication is a ``quarantine_verdict`` message only: verdict + typed safe
metadata (the candidate part identifier, constrained to identifiers the
bounded parser actually extracted). The raw document never leaves the
quarantine store (SEC-4); downstream agents evaluate the identifier against
the trusted approved-parts registry, never the source text.

``released`` semantics (documented interpretation of the frozen contract):
True when the screening PIPELINE completed without error (SEC-2) — a
flagged/malicious result still publishes with ``released: true`` so Safety
can reject the identifier (spine Scene 1); the flag rides in ``screening``.
``released: false`` (errors) forbids safe_metadata by contract.
"""

from __future__ import annotations

import re
from typing import Any

from jsonschema import Draft202012Validator

from forge_common import layout
from forge_common.agent_base import (
    AdkTextRunner,
    AgentOutputMalformed,
    constrained_json,
    load_prompt,
)
from forge_common.audit import build_audit_event, now_iso
from forge_common.clock import read_clock
from forge_common.contracts import validate_message
from forge_common.messages import build_envelope, deterministic_event_id
from forge_common.quarantine import (
    STATUS_QUARANTINED,
    STATUS_SCREENED,
    FirestoreQuarantineStore,
    quarantine_ref,
)

CYBER_TRUST_AGENT = "agent-cyber_trust-01"
CYBER_TRUST_IDENTITY = "forge-cyber-trust"

#: Bounded parser limits (SEC-1: bounded, deterministic, no model).
MAX_DOCUMENT_CHARS = 20_000
_IDENTIFIER_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,7}(?:-[A-Z0-9]{2,8}){1,3}\b")


class ScreeningError(Exception):
    """Any parser/screening/classifier failure — the document stays
    quarantined (SEC-2, fail closed)."""


def bounded_parse(raw_text: str) -> dict[str, Any]:
    """Deterministic, bounded extraction (SEC-1). Raises on bound breach."""
    if len(raw_text) > MAX_DOCUMENT_CHARS:
        raise ScreeningError(f"document exceeds the {MAX_DOCUMENT_CHARS}-char parser bound")
    text = "".join(ch for ch in raw_text if ch == "\n" or ch == "\t" or ord(ch) >= 32)
    identifiers = sorted(
        {
            m.group(0)
            for m in _IDENTIFIER_RE.finditer(text)
            if any(c.isdigit() for c in m.group(0)) and 4 <= len(m.group(0)) <= 32
        }
    )
    return {"text": text, "identifiers": identifiers}


def _classifier_validator(identifiers: list[str]) -> Draft202012Validator:
    return Draft202012Validator(
        {
            "type": "object",
            "additionalProperties": False,
            "required": ["label", "confidence", "candidate_part_identifier", "rationale"],
            "properties": {
                "label": {"enum": ["benign", "suspicious", "malicious"]},
                "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                # Constrained to identifiers the bounded parser actually
                # extracted: the model cannot invent an identifier (SEC-4).
                "candidate_part_identifier": {"enum": [*identifiers, None]},
                "rationale": {"type": "string", "minLength": 5, "maxLength": 500},
            },
        }
    )


def screen_document(
    db: Any,
    store: FirestoreQuarantineStore,
    doc_id: str,
    *,
    armor: Any,
    classifier_model: Any,
    classifier_model_id: str,
    trace_id: str,
) -> dict[str, Any] | None:
    """Run the SEC-1 pipeline for one quarantined document and publish the
    verdict transactionally (outbox + audit + status in one transaction).

    Returns the published verdict message, or None when fail-closed (the
    document stays QUARANTINED with an audited screening failure) or when
    the document was already screened (idempotent re-run).
    """
    record = store.get(doc_id)
    if record is None:
        raise ScreeningError(f"no quarantined document {doc_id}")
    if record["status"] == STATUS_SCREENED:
        return None  # already screened; verdict already published
    workflow_id = record["workflow_id"]

    def _fail_closed(reason: str) -> None:
        audit = build_audit_event(
            workflow_id=workflow_id,
            trace_id=trace_id,
            agent_identity=CYBER_TRUST_AGENT,
            event_kind="blocked_action",
            reason_code="SCREENING_FAILED",
            input_obj={"doc_id": doc_id, "sha256": record["sha256"]},
            output_obj={"error": reason[:500]},
            effective_at=read_clock(db),
        )

        def _write(txn: Any) -> None:
            current = layout.txn_get_dict(txn, quarantine_ref(db, doc_id))
            if not current or current["status"] == STATUS_SCREENED:
                return
            txn.set(
                quarantine_ref(db, doc_id),
                {
                    **current,
                    "status": STATUS_QUARANTINED,
                    "last_error": reason[:500],
                    "screening_failed_observed_at": now_iso(),
                },
            )
            audit_ref = layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"])
            if not layout.txn_get_dict(txn, audit_ref):
                txn.create(audit_ref, audit)

        layout.run_in_transaction(db, _write)

    # SEC-1: parser, Model Armor, classifier — all three, in order.
    try:
        parsed = bounded_parse(record["raw_text"])
        armor_result = armor(parsed["text"])
        if armor_result.get("verdict") not in ("clean", "flagged"):
            raise ScreeningError(f"unusable Model Armor result: {armor_result!r}")
        classification = constrained_json(
            AdkTextRunner(name="forge_cyber_trust_classifier", model=classifier_model),
            load_prompt(
                "cyber_trust_classifier.v1.md",
                {
                    "document_text": parsed["text"],
                    "extracted_identifiers": ", ".join(parsed["identifiers"]) or "(none)",
                },
            ),
            _classifier_validator(parsed["identifiers"]),
        )
    except (ScreeningError, AgentOutputMalformed, Exception) as exc:  # noqa: BLE001
        _fail_closed(f"{type(exc).__name__}: {exc}")
        return None

    payload: dict[str, Any] = {
        "document_ref": {
            "quarantine_uri": record["quarantine_uri"],
            "sha256": record["sha256"],
        },
        "screening": {
            "parser_ok": True,
            "model_armor": {
                "verdict": armor_result["verdict"],
                "categories": armor_result.get("categories", []),
            },
            "classifier": {
                "label": classification["label"],
                "model_id": classifier_model_id,
                "confidence": classification["confidence"],
            },
        },
        "released": True,
    }
    candidate = classification.get("candidate_part_identifier")
    if candidate:
        payload["safe_metadata"] = {"candidate_part_identifier": candidate}
    event_id = deterministic_event_id("verdict", workflow_id, doc_id)
    verdict = {
        "envelope": build_envelope(
            workflow_id=workflow_id,
            schema_version="quarantine_verdict.v2",
            event_id=event_id,
            trace_id=trace_id,
            idempotency_key=f"idem-verdict-{doc_id}"[:128],
        ),
        "payload": payload,
    }
    validate_message(verdict)
    audit = build_audit_event(
        workflow_id=workflow_id,
        trace_id=trace_id,
        agent_identity=CYBER_TRUST_AGENT,
        event_kind="quarantine",
        reason_code="DOCUMENT_SCREENED",
        input_obj={"doc_id": doc_id, "sha256": record["sha256"]},
        output_obj=payload,
        effective_at=read_clock(db),
    )

    def _publish(txn: Any) -> bool:
        current = layout.txn_get_dict(txn, quarantine_ref(db, doc_id))
        if not current or current["status"] == STATUS_SCREENED:
            return False
        txn.set(
            quarantine_ref(db, doc_id),
            {**current, "status": STATUS_SCREENED, "screened_observed_at": now_iso()},
        )
        txn.create(
            layout.outbox_ref(db, workflow_id, event_id),
            {"message": verdict, "published": False, "enqueued_at": now_iso()},
        )
        txn.create(layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"]), audit)
        return True

    return verdict if layout.run_in_transaction(db, _publish) else None


def ingest_document(
    db: Any,
    store: FirestoreQuarantineStore,
    *,
    workflow_id: str,
    doc_id: str,
    raw_text: str,
    source: str,
    trace_id: str,
) -> dict[str, Any]:
    """SEC-1: the external document enters quarantine FIRST, audited. The
    raw text goes nowhere else."""
    record = store.put(doc_id=doc_id, workflow_id=workflow_id, raw_text=raw_text, source=source)
    audit = build_audit_event(
        workflow_id=workflow_id,
        trace_id=trace_id,
        agent_identity=CYBER_TRUST_AGENT,
        event_kind="quarantine",
        reason_code="DOCUMENT_QUARANTINED",
        input_obj={"doc_id": doc_id, "source": source, "sha256": record["sha256"]},
        output_obj={"quarantine_uri": record["quarantine_uri"]},
        effective_at=read_clock(db),
    )

    def _audit(txn: Any) -> None:
        ref = layout.audit_ref(db, workflow_id, audit["envelope"]["event_id"])
        if not layout.txn_get_dict(txn, ref):
            txn.create(ref, audit)

    layout.run_in_transaction(db, _audit)
    return record
