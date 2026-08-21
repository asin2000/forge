"""Quarantine storage (SEC-1, SEC-2, SEC-4, AGT-5).

External documents enter quarantine FIRST and raw content never leaves it:
only Cyber Trust reads this store, and nothing here is ever placed on the
agent bus — publication is verdict-only plus typed untrusted metadata
(SEC-4).

PRODUCTION: :class:`GcsQuarantineStore` — the raw bytes live as a GCS
object at the canonical ``gs://`` URI the verdict contract references; the
Firestore metadata document carries hash/status/workflow but NEVER the raw
text. Bucket-level IAM grants object read to the Cyber Trust service
account only (AGT-6 platform enforcement; live positive/negative IAM tests
run against the deployed per-role identities, Lane 2).

EMULATOR-ONLY: :class:`FirestoreQuarantineStore` keeps the bytes in the
Firestore ``quarantine`` collection so Lane 1 and the emulator gate can run
without GCS. It must not be deployed.

RE-INGEST INTEGRITY: reusing a ``doc_id`` with identical bytes, workflow,
and source is idempotent and returns the STORED record; any mismatch raises
:class:`QuarantineConflict` — one document ID can never audit one document
while screening another.
"""

from __future__ import annotations

from typing import Any

from forge_common import layout
from forge_common.audit import now_iso
from forge_common.messages import sha256_hex

STATUS_QUARANTINED = "QUARANTINED"
STATUS_SCREENED = "SCREENED"


class QuarantineConflict(Exception):
    """A doc_id was re-used with different bytes, workflow, or source."""


def quarantine_ref(db: Any, doc_id: str) -> Any:
    return db.collection("quarantine").document(doc_id)


class _QuarantineStoreBase:
    """Shared metadata handling; subclasses own the raw bytes."""

    def __init__(self, db: Any, *, bucket: str):
        self._db = db
        self.bucket = bucket

    def uri(self, doc_id: str) -> str:
        return f"gs://{self.bucket}/quarantine/{doc_id}"

    def _metadata(
        self, *, doc_id: str, workflow_id: str, raw_text: str, source: str
    ) -> dict[str, Any]:
        return {
            "doc_id": doc_id,
            "workflow_id": workflow_id,
            "quarantine_uri": self.uri(doc_id),
            "sha256": sha256_hex(raw_text),
            "source": source,
            "status": STATUS_QUARANTINED,
            "ingested_observed_at": now_iso(),
        }

    def _existing_or_conflict(
        self, record: dict[str, Any], existing: dict[str, Any] | None
    ) -> dict[str, Any] | None:
        """None -> proceed with a fresh put; record -> idempotent re-ingest."""
        if existing is None:
            return None
        for field in ("sha256", "workflow_id", "source"):
            if existing.get(field) != record[field]:
                raise QuarantineConflict(
                    f"doc_id {record['doc_id']} re-ingested with different "
                    f"{field} (stored {existing.get(field)!r} != attempted "
                    f"{record[field]!r})"
                )
        return existing

    def get(self, doc_id: str) -> dict[str, Any] | None:
        """Metadata record (no raw text in the GCS-backed store)."""
        snapshot = quarantine_ref(self._db, doc_id).get()
        return snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot


class FirestoreQuarantineStore(_QuarantineStoreBase):
    """EMULATOR-ONLY store: raw bytes inside the Firestore record. Never
    deploy this — production is :class:`GcsQuarantineStore`."""

    def put(
        self, *, doc_id: str, workflow_id: str, raw_text: str, source: str
    ) -> tuple[dict[str, Any], bool]:
        record = {
            **self._metadata(
                doc_id=doc_id, workflow_id=workflow_id, raw_text=raw_text, source=source
            ),
            "raw_text": raw_text,
        }

        def _put(txn: Any) -> tuple[dict[str, Any], bool]:
            existing = layout.txn_get_dict(txn, quarantine_ref(self._db, doc_id))
            kept = self._existing_or_conflict(record, existing)
            if kept is not None:
                return kept, False
            txn.create(quarantine_ref(self._db, doc_id), record)
            return record, True

        return layout.run_in_transaction(self._db, _put)

    def read_raw(self, doc_id: str) -> str:
        record = self.get(doc_id)
        if record is None:
            raise KeyError(doc_id)
        return record["raw_text"]


class GcsQuarantineStore(_QuarantineStoreBase):
    """Production store: raw bytes in GCS under the canonical URI; Firestore
    metadata NEVER contains raw_text (SEC-4)."""

    def __init__(self, db: Any, *, bucket: str, client: Any = None):
        super().__init__(db, bucket=bucket)
        if client is None:  # pragma: no cover - exercised by the live smoke
            from google.cloud import storage

            client = storage.Client()
        self._bucket = client.bucket(bucket)

    def put(
        self, *, doc_id: str, workflow_id: str, raw_text: str, source: str
    ) -> tuple[dict[str, Any], bool]:
        record = self._metadata(
            doc_id=doc_id, workflow_id=workflow_id, raw_text=raw_text, source=source
        )

        def _put(txn: Any) -> dict[str, Any] | None:
            existing = layout.txn_get_dict(txn, quarantine_ref(self._db, doc_id))
            kept = self._existing_or_conflict(record, existing)
            if kept is not None:
                return kept
            txn.create(quarantine_ref(self._db, doc_id), record)
            return None

        kept = layout.run_in_transaction(self._db, _put)
        if kept is not None:
            return kept, False  # idempotent re-ingest: object already uploaded
        self._bucket.blob(f"quarantine/{doc_id}").upload_from_string(
            raw_text, content_type="text/plain"
        )
        return record, True

    def read_raw(self, doc_id: str) -> str:
        """Reads the GCS object — succeeds only for identities holding
        bucket read (the Cyber Trust service account in production)."""
        return self._bucket.blob(f"quarantine/{doc_id}").download_as_text()
