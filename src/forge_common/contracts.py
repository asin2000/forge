"""Contract validation for FORGE inter-agent messages.

Implements ICD-1/ICD-2/ICD-4: every message is validated against its versioned
JSON schema, selected by ``envelope.schema_version``, at BOTH publish and
subscribe. Nonconforming messages raise :class:`ContractViolation` — they are
rejected and audited by the caller, never silently coerced.
"""

from __future__ import annotations

import functools
import json
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker
from referencing import Registry, Resource

CONTRACTS_DIR = Path(__file__).resolve().parents[2] / "contracts"

#: Message types allowed on the agent bus. The workflow_state schema is
#: internal Firestore state and deliberately NOT registered here.
MESSAGE_TYPES = (
    "nmc_event.v2",
    "work_package_assignment.v2",
    "maintenance_action_plan.v2",
    "sourcing_report.v3",
    "roster_assignment.v2",
    "validation_verdict.v2",
    "quarantine_verdict.v3",
    "approval_request.v2",
    "approval_decision.v2",
    "agent_failure_event.v2",
    "due_event.v2",
    "audit_event.v2",
)


class ContractViolation(Exception):
    """A message failed contract validation (ICD-2)."""

    def __init__(self, schema_version: str, errors: list[str]):
        self.schema_version = schema_version
        self.errors = errors
        super().__init__(f"{schema_version}: {'; '.join(errors[:5])}")


def _load_schema_file(name: str) -> dict[str, Any]:
    path = CONTRACTS_DIR / f"{name}.schema.json"
    if not path.is_file():
        raise ContractViolation(name, [f"unknown schema '{name}'"])
    return json.loads(path.read_text())


@functools.lru_cache(maxsize=1)
def _registry() -> Registry:
    """Registry resolving relative $refs (e.g. envelope) by filename."""
    resources = []
    for path in CONTRACTS_DIR.glob("*.schema.json"):
        schema = json.loads(path.read_text())
        resources.append((path.name, Resource.from_contents(schema)))
        resources.append((schema["$id"], Resource.from_contents(schema)))
    return Registry().with_resources(resources)


@functools.lru_cache(maxsize=64)
def validator_for(schema_version: str) -> Draft202012Validator:
    schema = _load_schema_file(schema_version)
    return Draft202012Validator(schema, registry=_registry(), format_checker=FormatChecker())


def validate_message(message: dict[str, Any]) -> str:
    """Validate a bus message; return its schema_version.

    Called at publish and at subscribe (ICD-2). Raises ContractViolation on
    any nonconformance, including an unknown or non-bus schema_version.
    """
    envelope = message.get("envelope")
    if not isinstance(envelope, dict):
        raise ContractViolation("<missing>", ["message has no envelope object"])
    schema_version = envelope.get("schema_version")
    if schema_version not in MESSAGE_TYPES:
        raise ContractViolation(
            str(schema_version), ["schema_version is not a registered bus message type"]
        )
    validator = validator_for(schema_version)
    errors = [
        f"{'/'.join(str(p) for p in e.absolute_path) or '<root>'}: {e.message}"
        for e in validator.iter_errors(message)
    ]
    if errors:
        raise ContractViolation(schema_version, sorted(errors))
    return schema_version
