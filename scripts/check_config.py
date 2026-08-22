#!/usr/bin/env python3
"""CI-9 config gate: registry + manifest + residency must describe REALITY.

Checks (all hermetic — network reachability of live endpoints is proven in
the verification docs, not in CI):

registry (REG-5 CI half):
- every service role maps to exactly one registry definition and vice versa;
- declared contracts are ACTIVE MESSAGE_TYPES;
- service identities follow the per-role pattern (AGT-6);
- endpoint is either null (not yet deployed) or a real Cloud Run URL in the
  DAT-1 region — placeholder strings like "deploy-time" FAIL;
- registry endpoint state agrees with the manifest's deployment status.

architecture manifest (DFT-5 half):
- every directory under services/ has a manifest entry and every manifest
  source path exists;
- status is one of planned/implemented/deployed, and a service whose source
  code exists may not claim "planned";
- service_account is the resolved per-role account id (forge-<role>, no
  legacy -sa suffix) and agrees with the registry service_identity;
- deployed services carry a real endpoint URL in the DAT-1 Cloud Run
  region; non-deployed services carry none;
- the observability section pins the Cloud Logging location to the DAT-1
  map.

residency (DAT-1):
- every region literal in the infra scripts appears in the approved
  location map, and the Pub/Sub adapter's regional endpoint matches.
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
MANIFEST_STATUSES = {"planned", "implemented", "deployed"}


def check_registry(registry: dict, manifest: dict, run_region: str) -> list[str]:
    failures: list[str] = []
    ids = [a["agent_id"] for a in registry["agents"]]
    if len(ids) != len(set(ids)):
        failures.append("duplicate agent_id in registry.yaml")
    by_role = {a["agent_id"].removeprefix("forge-").replace("-", "_") for a in registry["agents"]}
    for role in SERVICE_ROLES - by_role:
        failures.append(f"service role {role} has no registry definition")
    for role in by_role - SERVICE_ROLES:
        failures.append(f"registry definition for unknown service role {role}")
    endpoint_re = re.compile(rf"^https://[a-z0-9-]+\.{re.escape(run_region)}\.run\.app$")
    manifest_services = manifest.get("services", {})
    for agent in registry["agents"]:
        agent_id = agent["agent_id"]
        for contract in agent["contracts"].get("consumes", []) + agent["contracts"].get(
            "produces", []
        ):
            if contract not in MESSAGE_TYPES:
                failures.append(f"{agent_id} declares inactive contract {contract}")
        if not re.fullmatch(
            r"forge-[a-z-]+@PROJECT_ID\.iam\.gserviceaccount\.com", agent["service_identity"]
        ):
            failures.append(f"{agent_id} service_identity not per-role pattern (AGT-6)")
        role = agent_id.removeprefix("forge-").replace("-", "_")
        endpoint = agent.get("endpoint")
        deployed = manifest_services.get(role, {}).get("status") == "deployed"
        if endpoint is None:
            if deployed:
                failures.append(f"{agent_id}: manifest says deployed but registry endpoint is null")
        elif not endpoint_re.match(str(endpoint)):
            failures.append(
                f"{agent_id}: endpoint {endpoint!r} is not a real Cloud Run URL in the "
                f"DAT-1 region (placeholders like 'deploy-time' are not deployment records)"
            )
        elif not deployed:
            failures.append(
                f"{agent_id}: registry carries endpoint {endpoint!r} but the manifest "
                f"does not mark {role} deployed"
            )
    return failures


def check_manifest(manifest: dict, registry: dict, residency: dict) -> list[str]:
    failures: list[str] = []
    run_region = residency["locations"]["cloud_run"]
    endpoint_re = re.compile(rf"^https://[a-z0-9-]+\.{re.escape(run_region)}\.run\.app$")
    services = manifest.get("services", {})

    on_disk = {
        d.name for d in (ROOT / "services").iterdir() if d.is_dir() and not d.name.startswith("__")
    }
    for missing in sorted(on_disk - set(services)):
        failures.append(f"manifest: services/{missing}/ exists but has no manifest entry")

    identities = {a["agent_id"]: a["service_identity"] for a in registry["agents"]}
    for key, svc in services.items():
        status = svc.get("status")
        if status not in MANIFEST_STATUSES:
            failures.append(f"manifest {key}: status {status!r} not in {sorted(MANIFEST_STATUSES)}")
        sources = svc.get("source", [])
        existing = [s for s in sources if (ROOT / s).exists()]
        for s in set(sources) - set(existing):
            failures.append(f"manifest {key}: source path {s} does not exist")
        if status == "planned" and (existing or (ROOT / "services" / key).is_dir()):
            # keyed off the DIRECTORY too, so emptying the source list cannot
            # smuggle a planned status past the gate
            failures.append(
                f"manifest {key}: status 'planned' but source code exists — "
                f"use implemented or deployed"
            )
        expected_sa = f"forge-{key.replace('_', '-')}"
        sa = svc.get("service_account")
        if sa != expected_sa:
            failures.append(
                f"manifest {key}: service_account {sa!r} is not the resolved per-role "
                f"account id {expected_sa!r} (AGT-6; legacy -sa names are stale)"
            )
        agent_id = f"forge-{key.replace('_', '-')}"
        if agent_id in identities and not identities[agent_id].startswith(f"{expected_sa}@"):
            failures.append(
                f"manifest {key}: service_account disagrees with registry "
                f"service_identity {identities[agent_id]}"
            )
        endpoint = svc.get("endpoint")
        if status == "deployed":
            if not endpoint or not endpoint_re.match(str(endpoint)):
                failures.append(
                    f"manifest {key}: deployed without a real Cloud Run endpoint in "
                    f"{run_region} (got {endpoint!r})"
                )
        elif endpoint:
            failures.append(f"manifest {key}: endpoint {endpoint!r} on a non-deployed service")

    observability = manifest.get("observability")
    if not observability:
        failures.append("manifest: no observability section (Cloud Logging location unpinned)")
    else:
        logging_location = observability.get("cloud_logging_location")
        if logging_location != residency["locations"].get("cloud_logging"):
            failures.append(
                f"manifest: cloud_logging_location {logging_location!r} != DAT-1 map "
                f"{residency['locations'].get('cloud_logging')!r}"
            )
    return failures


def check_residency(residency: dict) -> list[str]:
    failures: list[str] = []
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
    return failures


def main() -> int:
    registry = yaml.safe_load((ROOT / "agents" / "registry.yaml").read_text())
    residency = yaml.safe_load((ROOT / "infra" / "residency.yaml").read_text())
    manifest = yaml.safe_load((ROOT / "architecture" / "manifest.yaml").read_text())

    failures = (
        check_registry(registry, manifest, residency["locations"]["cloud_run"])
        + check_manifest(manifest, registry, residency)
        + check_residency(residency)
    )
    if failures:
        print(f"CONFIG GATE (CI-9): FAIL ({len(failures)})")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("CONFIG GATE (CI-9): PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
