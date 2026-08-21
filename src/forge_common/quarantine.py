"""Quarantine storage (SEC-1, SEC-2, SEC-4, AGT-5).

External documents enter quarantine FIRST and raw content never leaves it:
only Cyber Trust reads this store, and nothing here is ever placed on the
agent bus — publication is verdict-only plus typed safe metadata (SEC-4).

The canonical address is the ``gs://`` URI mandated by the
quarantine_verdict contract; the demo/emulator store keeps the bytes in a
Firestore ``quarantine`` collection under that URI's document ID, while the
production deploy (Day 6) binds the actual GCS bucket with bucket-level IAM
granting read to the Cyber Trust service account only (AGT-6 platform
enforcement — verified by the Lane 2 negative IAM test, not by convention).
"""

from __future__ import annotations

from typing import Any

from forge_common import layout
from forge_common.audit import now_iso
from forge_common.messages import sha256_hex

STATUS_QUARANTINED = "QUARANTINED"
STATUS_SCREENED = "SCREENED"


def quarantine_ref(db: Any, doc_id: str) -> Any:
    return db.collection("quarantine").document(doc_id)


class FirestoreQuarantineStore:
    """Demo/emulator quarantine store; production is the GCS bucket."""

    def __init__(self, db: Any, *, bucket: str):
        self._db = db
        self.bucket = bucket

    def uri(self, doc_id: str) -> str:
        return f"gs://{self.bucket}/quarantine/{doc_id}"

    def put(self, *, doc_id: str, workflow_id: str, raw_text: str, source: str) -> dict[str, Any]:
        """Quarantine a raw document (SEC-1: quarantine FIRST). Returns the
        document record (raw content included — for Cyber Trust only)."""
        record = {
            "doc_id": doc_id,
            "workflow_id": workflow_id,
            "quarantine_uri": self.uri(doc_id),
            "sha256": sha256_hex(raw_text),
            "raw_text": raw_text,
            "source": source,
            "status": STATUS_QUARANTINED,
            "ingested_observed_at": now_iso(),
        }

        def _put(txn: Any) -> None:
            if layout.txn_get_dict(txn, quarantine_ref(self._db, doc_id)):
                return  # idempotent re-ingest
            txn.create(quarantine_ref(self._db, doc_id), record)

        layout.run_in_transaction(self._db, _put)
        return record

    def get(self, doc_id: str) -> dict[str, Any] | None:
        snapshot = quarantine_ref(self._db, doc_id).get()
        return snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
