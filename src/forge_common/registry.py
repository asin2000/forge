"""Agent Registry: declarative definitions, runtime instances, capability
discovery (REG-1, REG-2, REG-3).

Definitions come from the version-controlled ``agents/registry.yaml`` and are
loaded into the Firestore ``agent_registry`` collection by the PLT-6 deploy
step (:func:`load_registry`). Runtime instances live in ``agent_instances``
with states ``IDLE | RESERVE | ACTIVE | FAILED``; derived health comes from
heartbeat staleness, never a stored flag (REG-1).

Discovery (:func:`discover`) resolves a required capability to an APPROVED
definition and a usable instance — never a hard-coded service address
(REG-3). Discovery yields identity and endpoint only; authorization still
fails at IAM (AGT-6). Absence of any capable APPROVED definition raises
:class:`NoCapableAgent`, which callers convert into the audited
``BLOCKED_AGENT_FAILURE`` escalation (ORC-4 disposition).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from forge_common import layout
from forge_common.audit import now_iso

REGISTRY_YAML = Path(__file__).resolve().parents[2] / "agents" / "registry.yaml"

DEFINITION_STATES = ("DRAFT", "APPROVED", "DEPRECATED", "RETIRED")
INSTANCE_STATES = ("IDLE", "RESERVE", "ACTIVE", "FAILED")


class NoCapableAgent(Exception):
    """No APPROVED definition offers the required capability (REG-3)."""


class IneligibleAssignment(Exception):
    """REG-2: assignment refused at claim time — the definition is no longer
    APPROVED or the instance is not assignable. Rejected and audited."""


def definition_ref(db: Any, agent_id: str) -> Any:
    return db.collection("agent_registry").document(agent_id)


def instance_ref(db: Any, instance_id: str) -> Any:
    return db.collection("agent_instances").document(instance_id)


def _role_of(definition_id: str) -> str:
    return definition_id.removeprefix("forge-").replace("-", "_")


def instance_id_for(definition_id: str, seq: int) -> str:
    """Instance IDs follow the contract pattern ``agent-<role>-NN``."""
    return f"agent-{_role_of(definition_id)}-{seq:02d}"


def load_registry(db: Any, yaml_path: Path = REGISTRY_YAML) -> list[str]:
    """PLT-6 deploy step: load definitions and seed runtime instances.

    Idempotent: definitions are overwritten from the declarative source of
    truth; instance documents are created only if absent so runtime state
    (ACTIVE/FAILED, heartbeats) survives redeploys. Each definition gets one
    primary IDLE instance; the Workforce role also gets a RESERVE instance
    (ORC-3's held reserve, deployed in the Day 4 repair loop).
    """
    doc = yaml.safe_load(yaml_path.read_text())
    loaded: list[str] = []
    for definition in doc["agents"]:
        agent_id = definition["agent_id"]
        definition_ref(db, agent_id).set({**definition, "loaded_observed_at": now_iso()})
        loaded.append(agent_id)
        seeds = [(1, "IDLE")]
        if agent_id == "forge-workforce":
            seeds.append((2, "RESERVE"))
        for seq, instance_state in seeds:
            iid = instance_id_for(agent_id, seq)
            ref = instance_ref(db, iid)
            snapshot = ref.get()
            if not (snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot):
                ref.set(
                    {
                        "instance_id": iid,
                        "definition_id": agent_id,
                        "role": _role_of(agent_id),
                        "state": instance_state,
                        "last_heartbeat_at": None,
                    }
                )
    return loaded


def discover(db: Any, capability: str) -> dict[str, Any]:
    """Resolve a capability to {definition, instance} (REG-2, REG-3).

    Only APPROVED definitions are eligible; work goes to a non-FAILED,
    non-RESERVE instance of the definition (reserves deploy only on failure,
    ORC-3). Raises NoCapableAgent when nothing qualifies.
    """
    definition = None
    for snapshot in db.collection("agent_registry").stream():
        candidate = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if candidate.get("lifecycle_status") == "APPROVED" and capability in candidate.get(
            "capabilities", []
        ):
            definition = candidate
            break
    if definition is None:
        raise NoCapableAgent(f"no APPROVED definition offers capability {capability!r}")
    instances = []
    for snapshot in db.collection("agent_instances").stream():
        inst = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if inst.get("definition_id") == definition["agent_id"] and inst.get("state") in (
            "IDLE",
            "ACTIVE",
        ):
            instances.append(inst)
    if not instances:
        raise NoCapableAgent(f"definition {definition['agent_id']} has no assignable instance")
    instances.sort(key=lambda i: i["instance_id"])
    return {"definition": definition, "instance": instances[0]}


def check_claim_eligibility(
    txn: Any, db: Any, *, instance_id: str, role: str, **_ignored: Any
) -> None:
    """REG-2 enforced AT ASSIGNMENT TIME, transactionally (reads only).

    Discovery's answer can go stale between the handler and the commit: the
    definition and instance are re-read INSIDE the committing transaction and
    the claim is refused — IneligibleAssignment — if the definition is not
    APPROVED or the instance is not IDLE/ACTIVE for the right role. Callers
    run all eligibility reads before any transaction write (real Firestore
    requires reads first).
    """
    inst = layout.txn_get_dict(txn, instance_ref(db, instance_id))
    if not inst or inst.get("state") not in ("IDLE", "ACTIVE") or inst.get("role") != role:
        raise IneligibleAssignment(
            f"instance {instance_id} is not assignable for role {role} "
            f"(state={inst.get('state') if inst else 'missing'}) (REG-2)"
        )
    definition = layout.txn_get_dict(txn, definition_ref(db, inst["definition_id"]))
    if not definition or definition.get("lifecycle_status") != "APPROVED":
        raise IneligibleAssignment(
            f"definition {inst['definition_id']} is not APPROVED "
            f"({definition.get('lifecycle_status') if definition else 'missing'}) (REG-2)"
        )


def claim_work_package(
    txn: Any,
    db: Any,
    *,
    workflow_id: str,
    work_package_id: str,
    instance_id: str,
    role: str,
    objective: str = "",
    inputs: dict[str, Any] | None = None,
) -> None:
    """Create the exclusive-ownership record for a work package (ORC-1).

    WRITES ONLY — callers must have run check_claim_eligibility earlier in
    the same transaction (REG-2). ``create`` collides if the work package
    already has an owner — exclusive ownership is enforced by the datastore,
    not by convention. Marks the owning instance ACTIVE (REG-2 state).
    """
    txn.create(
        layout.work_package_ref(db, workflow_id, work_package_id),
        {
            "work_package_id": work_package_id,
            "owner_instance_id": instance_id,
            "role": role,
            "objective": objective,
            "inputs": inputs or {},
            "status": "ASSIGNED",
            "assignment_seq": 1,
            "assigned_observed_at": now_iso(),
        },
    )
    txn.set(
        instance_ref(db, instance_id),
        {"state": "ACTIVE", "state_changed_observed_at": now_iso()},
        merge=True,
    )


def find_reserve_instance(db: Any, role: str) -> dict[str, Any] | None:
    """The held RESERVE instance for a role, if any (ORC-3)."""
    for snapshot in db.collection("agent_instances").stream():
        inst = snapshot.to_dict() if hasattr(snapshot, "to_dict") else snapshot
        if inst.get("role") == role and inst.get("state") == "RESERVE":
            return inst
    return None


def check_reassignment(
    txn: Any,
    db: Any,
    *,
    workflow_id: str,
    work_package_id: str,
    failed_instance_id: str,
    reserve_instance_id: str,
    **_ignored: Any,
) -> dict[str, Any]:
    """READS ONLY: decide whether a reassignment may apply (ORC-3, exactly
    once). Returns a plan; ``skip=True`` when ownership already transferred
    (a double-fired failure event applies nothing). Raises
    IneligibleAssignment when the reserve is not actually in RESERVE."""
    wp = layout.txn_get_dict(txn, layout.work_package_ref(db, workflow_id, work_package_id))
    if not wp:
        raise IneligibleAssignment(f"work package {work_package_id} does not exist")
    if wp.get("owner_instance_id") != failed_instance_id or wp.get("status") == "COMPLETED":
        # Stale failure event (ownership already transferred, or the package
        # completed): the ENTIRE reassignment — transfer, audit, seq-2
        # assignment — is a transactional no-op.
        return {"skip": True, "work_package": wp}
    reserve = layout.txn_get_dict(txn, instance_ref(db, reserve_instance_id))
    if not reserve or reserve.get("state") != "RESERVE":
        raise IneligibleAssignment(
            f"instance {reserve_instance_id} is not a held reserve "
            f"(state={reserve.get('state') if reserve else 'missing'}) (ORC-3)"
        )
    return {"skip": False, "work_package": wp}


def apply_reassignment(
    txn: Any,
    db: Any,
    *,
    workflow_id: str,
    work_package_id: str,
    failed_instance_id: str,
    reserve_instance_id: str,
    plan: dict[str, Any],
    audit_event: dict[str, Any] | None = None,
    assignment_message: dict[str, Any] | None = None,
) -> bool:
    """WRITES ONLY (after :func:`check_reassignment` in the same txn):
    atomically transfer ownership to the reserve, mark the failed instance
    FAILED and the reserve ACTIVE, and write the bundled reassignment audit
    event and seq-2 assignment (ORC-3). Returns False on skip — a skipped
    plan writes NOTHING, so a stale failure leaves no audit, no outbox
    message, and no state change."""
    if plan.get("skip"):
        return False
    wp = plan["work_package"]
    txn.set(
        layout.work_package_ref(db, workflow_id, work_package_id),
        {
            **wp,
            "owner_instance_id": reserve_instance_id,
            "reassigned_from": failed_instance_id,
            "status": "ASSIGNED",
            "assignment_seq": int(wp.get("assignment_seq", 1)) + 1,
            "reassigned_observed_at": now_iso(),
        },
    )
    if audit_event is not None:
        txn.create(
            layout.audit_ref(db, workflow_id, audit_event["envelope"]["event_id"]),
            audit_event,
        )
    if assignment_message is not None:
        txn.create(
            layout.outbox_ref(db, workflow_id, assignment_message["envelope"]["event_id"]),
            {"message": assignment_message, "published": False, "enqueued_at": now_iso()},
        )
    txn.set(
        instance_ref(db, failed_instance_id),
        {"state": "FAILED", "state_changed_observed_at": now_iso()},
        merge=True,
    )
    txn.set(
        instance_ref(db, reserve_instance_id),
        {"state": "ACTIVE", "state_changed_observed_at": now_iso()},
        merge=True,
    )
    return True


def check_wp_status_update(
    txn: Any,
    db: Any,
    *,
    workflow_id: str,
    work_package_id: str,
    expected_owner: str,
    **_ignored: Any,
) -> dict[str, Any]:
    """READS ONLY: decide whether a status update may apply.

    Skips (writes nothing) when the package is missing, no longer ASSIGNED,
    or owned by someone else — a stale worker's late output must not flip a
    reassigned package's status.
    """
    wp = layout.txn_get_dict(txn, layout.work_package_ref(db, workflow_id, work_package_id))
    if not wp or wp.get("status") != "ASSIGNED" or wp.get("owner_instance_id") != expected_owner:
        return {"skip": True}
    return {"skip": False, "work_package": wp}


def apply_wp_status_update(
    txn: Any,
    db: Any,
    *,
    workflow_id: str,
    work_package_id: str,
    status: str,
    plan: dict[str, Any],
    **_ignored: Any,
) -> bool:
    """WRITES ONLY: mark the package COMPLETED or FAILED_PENDING_REPAIR —
    atomically with the specialist's outbox message, so a successful agent
    can never be falsely timed out afterwards (ORC-4)."""
    if plan.get("skip"):
        return False
    txn.set(
        layout.work_package_ref(db, workflow_id, work_package_id),
        {**plan["work_package"], "status": status, "status_observed_at": now_iso()},
    )
    return True


def check_failure_disposition(
    txn: Any,
    db: Any,
    *,
    workflow_id: str,
    work_package_id: str,
    failed_instance_id: str,
    **_ignored: Any,
) -> dict[str, Any]:
    """READS ONLY: decide whether a non-reserve failure disposition applies.

    Skips — consuming the event with ZERO effects — when the package
    completed or ownership moved on between the handler's pre-read and this
    commit (the race entrant review flagged: a completed Maintenance/Supply
    package must never produce a blocked workflow)."""
    wp = layout.txn_get_dict(txn, layout.work_package_ref(db, workflow_id, work_package_id))
    if wp and (
        wp.get("status") == "COMPLETED" or wp.get("owner_instance_id") != failed_instance_id
    ):
        return {"skip": True}
    return {"skip": False, "work_package": wp}


def apply_failure_disposition(
    txn: Any,
    db: Any,
    *,
    workflow_id: str,
    work_package_id: str,
    failed_instance_id: str,
    audit_event: dict[str, Any],
    plan: dict[str, Any],
    **_ignored: Any,
) -> bool:
    """WRITES ONLY (transition applied separately by the bus, before other
    writes): mark the package and instance FAILED and write the escalation
    audit — atomically with the workflow block."""
    if plan.get("skip"):
        return False
    wp = plan.get("work_package")
    if wp:
        txn.set(
            layout.work_package_ref(db, workflow_id, work_package_id),
            {**wp, "status": "FAILED", "status_observed_at": now_iso()},
        )
    txn.set(
        instance_ref(db, failed_instance_id),
        {"state": "FAILED", "state_changed_observed_at": now_iso()},
        merge=True,
    )
    txn.create(
        layout.audit_ref(db, workflow_id, audit_event["envelope"]["event_id"]),
        audit_event,
    )
    return True
