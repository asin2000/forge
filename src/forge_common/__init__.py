"""Shared FORGE library: contract validation (ICD-1..4)."""

from forge_common.contracts import (
    MESSAGE_TYPES,
    ContractViolation,
    validate_message,
)

__all__ = ["MESSAGE_TYPES", "ContractViolation", "validate_message"]
