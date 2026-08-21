#!/usr/bin/env python3
"""CI-9 config gate: registry validity + DAT-1 residency map compliance.

- every service directory maps to exactly one registry definition and vice
  versa; declared contracts are ACTIVE MESSAGE_TYPES; service identities
  follow the per-role pattern (AGT-6);
- every region/location literal in the deploy scripts appears in the DAT-1
  approved location map, and the Pub/Sub adapter's regional endpoint
  matches the map (DAT-1).
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from forge_common.contracts import MESSAGE_TYPES  # noqa: E402

SERVICE_ROLES = {"orchestrator", "maintenance", "supply", "workforce", "safety", "cyber_trust"}


def main() -> int:
    failures: list[str] = []
    registry = yaml.safe_load((ROOT / "agents" / "registry.yaml").read_text())
    residency = yaml.safe_load((ROOT / "infra" / "residency.yaml").read_text())

    # --- registry validity (REG-5 CI half) ---
    ids = [a["agent_id"] for a in registry["agents"]]
    if len(ids) != len(set(ids)):
        failures.append("duplicate agent_id in registry.yaml")
    by_role = {a["agent_id"].removeprefix("forge-").replace("-", "_") for a in registry["agents"]}
    for role in SERVICE_ROLES - by_role:
        failures.append(f"service role {role} has no registry definition")
    for role in by_role - SERVICE_ROLES:
        failures.append(f"registry definition for unknown service role {role}")
    for agent in registry["agents"]:
        for contract in agent["contracts"].get("consumes", []) + agent["contracts"].get(
            "produces", []
        ):
            if contract not in MESSAGE_TYPES and contract != "audit_event.v2":
                failures.append(f"{agent['agent_id']} declares inactive contract {contract}")
        if not re.fullmatch(
            r"forge-[a-z-]+@PROJECT_ID\.iam\.gserviceaccount\.com", agent["service_identity"]
        ):
            failures.append(f"{agent['agent_id']} service_identity not per-role pattern (AGT-6)")

    # --- residency map compliance (DAT-1) ---
    allowed = set(residency["locations"].values()) | {residency["residency_jurisdiction"].lower()}
    region_re = re.compile(r"\b(us-[a-z]+\d|europe-[a-z]+\d|asia-[a-z]+\d)\b")
    for script in ["infra/setup-gcp.sh", "infra/deploy.sh"]:
        path = ROOT / script
        if not path.exists():
            continue
        for literal in set(region_re.findall(path.read_text())):
            if literal not in allowed:
                failures.append(f"{script}: region literal {literal} not in the DAT-1 map")
    pubsub_src = (ROOT / "src/forge_common/pubsub.py").read_text()
    expected = residency["pubsub"]["endpoint"]
    if 'DEFAULT_REGION = "us-central1"' not in pubsub_src or "us-central1" not in expected:
        failures.append("pubsub adapter default region does not match the DAT-1 map endpoint")

    if failures:
        print(f"CONFIG GATE (CI-9): FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("CONFIG GATE (CI-9): PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
