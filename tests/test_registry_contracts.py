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


def test_reg5_definition_loads_and_instance_activations_are_audited():
    """REG-5 (verify-pass finding): definition lifecycle changes and
    instance state transitions emit AUD-1 events. A first load audits every
    definition; an idempotent reload audits NOTHING; a claim audits the
    IDLE->ACTIVE activation in the claim's own transaction."""
    from tests.fake_firestore import FakeFirestore

    from forge_common import layout, registry, state
    from forge_common.bus import TxnWrites, process_message
    from forge_common.messages import build_envelope, deterministic_event_id
    from forge_common.registry import REGISTRY_AUDIT_WORKFLOW

    db = FakeFirestore()
    registry.load_registry(db)
    audits = lambda: [  # noqa: E731
        s.to_dict()["payload"]["reason_code"]
        for s in layout.workflow_ref(db, REGISTRY_AUDIT_WORKFLOW).collection("audit").stream()
    ]
    first = audits()
    assert first.count("REGISTRY_DEFINITION_LOADED") == 6
    registry.load_registry(db)
    assert audits() == first, "idempotent reload must not re-audit"

    wf = "wf-reg5-0001"
    trace = "ab" * 16
    layout.clock_ref(db).set({"logical_time": 0})
    state.create_workflow(
        db, workflow_id=wf, equipment_id="GX12-07", trace_id=trace, logical_time=0
    )

    def claiming_handler(message, writes: TxnWrites):
        writes.work_package_claims.append(
            {
                "work_package_id": f"wp-supply-{wf.removeprefix('wf-')}",
                "instance_id": "agent-supply-01",
                "role": "supply",
                "objective": "Source the part.",
            }
        )

    message = {
        "envelope": build_envelope(
            workflow_id=wf,
            schema_version="nmc_event.v2",
            event_id=deterministic_event_id("reg5-nmc", wf),
            trace_id=trace,
            idempotency_key="idem-reg5-nmc",
        ),
        "payload": {
            "equipment_id": "GX12-07",
            "discrepancy_code": "DSC-0042",
            "description": "reg5 activation audit probe",
            "reported_at": "2026-08-21T09:00:00Z",
        },
    }
    process_message(db, message, claiming_handler, consumer_identity="forge-orchestrator")
    workflow_audits = [
        s.to_dict() for s in layout.workflow_ref(db, wf).collection("audit").stream()
    ]
    activation = [a for a in workflow_audits if a["payload"]["reason_code"] == "AGENT_ACTIVATED"]
    assert len(activation) == 1
    assert activation[0]["envelope"]["trace_id"] == trace
