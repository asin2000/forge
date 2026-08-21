"""In-memory Firestore test double for Lane 1 tests (CI-3).

Implements exactly the surface forge_common uses: document refs, snapshots,
collection ``stream``, and buffered transactions whose writes commit only on
success — so the AUD-2 atomic-coupling and ICD-5 idempotency tests exercise
the production code paths. The CI-4 spine gate runs the same code against the
real Firestore emulator in CI.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from typing import Any


class FakeSnapshot:
    def __init__(self, data: dict[str, Any] | None):
        self._data = copy.deepcopy(data)
        self.exists = data is not None

    def to_dict(self) -> dict[str, Any] | None:
        return copy.deepcopy(self._data)


class FakeDocumentRef:
    def __init__(self, store: dict[tuple[str, ...], dict[str, Any]], path: tuple[str, ...]):
        self._store = store
        self.path = path

    def collection(self, name: str) -> FakeCollectionRef:
        return FakeCollectionRef(self._store, self.path + (name,))

    def get(self) -> FakeSnapshot:
        return FakeSnapshot(self._store.get(self.path))

    def set(self, data: dict[str, Any], merge: bool = False) -> None:
        if merge and self.path in self._store:
            self._store[self.path] = {**self._store[self.path], **copy.deepcopy(data)}
        else:
            self._store[self.path] = copy.deepcopy(data)

    def create(self, data: dict[str, Any]) -> None:
        if self.path in self._store:
            raise AlreadyExists(f"document exists: {'/'.join(self.path)}")
        self._store[self.path] = copy.deepcopy(data)


class FakeCollectionRef:
    def __init__(self, store: dict[tuple[str, ...], dict[str, Any]], path: tuple[str, ...]):
        self._store = store
        self.path = path

    def document(self, doc_id: str) -> FakeDocumentRef:
        return FakeDocumentRef(self._store, self.path + (doc_id,))

    def stream(self) -> Iterator[FakeSnapshot]:
        depth = len(self.path) + 1
        for path in sorted(self._store):
            if len(path) == depth and path[: len(self.path)] == self.path:
                yield FakeSnapshot(self._store[path])


class AlreadyExists(Exception):
    pass


class FakeTransaction:
    """Buffers writes; ``commit`` applies them or nothing (AUD-2 semantics)."""

    def __init__(self, store: dict[tuple[str, ...], dict[str, Any]]):
        self._store = store
        self._writes: list[tuple[str, tuple[str, ...], dict[str, Any]]] = []

    def get(self, ref: FakeDocumentRef) -> Iterator[FakeSnapshot]:
        """Yield a generator, matching the REAL client's Transaction.get —
        production code must normalize via layout.txn_get_dict."""

        def _one() -> Iterator[FakeSnapshot]:
            for _op, path, data in reversed(self._writes):
                if path == ref.path:
                    yield FakeSnapshot(data)
                    return
            yield ref.get()

        return _one()

    def set(self, ref: FakeDocumentRef, data: dict[str, Any], merge: bool = False) -> None:
        if merge:
            snapshot = next(iter(self.get(ref)), None)
            current = snapshot.to_dict() if snapshot is not None else None
            data = {**(current or {}), **data}
        self._writes.append(("set", ref.path, copy.deepcopy(data)))

    def create(self, ref: FakeDocumentRef, data: dict[str, Any]) -> None:
        pending = any(path == ref.path for _, path, _ in self._writes)
        if pending or ref.path in self._store:
            raise AlreadyExists(f"document exists: {'/'.join(ref.path)}")
        self._writes.append(("create", ref.path, copy.deepcopy(data)))

    def commit(self) -> None:
        for _, path, data in self._writes:
            self._store[path] = data
        self._writes = []


class FakeFirestore:
    def __init__(self) -> None:
        self.store: dict[tuple[str, ...], dict[str, Any]] = {}

    def collection(self, name: str) -> FakeCollectionRef:
        return FakeCollectionRef(self.store, (name,))

    def transaction(self) -> FakeTransaction:
        return FakeTransaction(self.store)
