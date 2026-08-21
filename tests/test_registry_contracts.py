"""CI-3 gate: every contract the registry declares is an ACTIVE bus type.

Prevents partial version cutovers (entrant review: registry.yaml still
declared sourcing_report.v2 after the code moved to v3)."""

import yaml

from forge_common.contracts import MESSAGE_TYPES
from forge_common.registry import REGISTRY_YAML


def test_registry_contracts_are_active_message_types():
    doc = yaml.safe_load(REGISTRY_YAML.read_text())
    for agent in doc["agents"]:
        declared = agent["contracts"].get("consumes", []) + agent["contracts"].get("produces", [])
        for contract in declared:
            assert contract in MESSAGE_TYPES, (
                f"{agent['agent_id']} declares {contract}, which is not in the "
                f"active MESSAGE_TYPES set"
            )
