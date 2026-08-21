# FORGE — Fleet Operational Readiness & Governed Execution

Multi-agent system coordinating recovery of non-mission-capable (NMC) support
equipment for a **fictional** installation operating twelve GX-12 Ground
Support Vehicles. Entry for the All Things Agentic Hackathon, **Fortified
Enterprise Fleet** track, by Anansi Labs.

**Governing baseline:** `FORGE-REQUIREMENTS.md` v1.1.2 (see project docs). If
code and that document disagree, the document wins. Schedule:
`FORGE-BUILD-PLAN.md`. Every module traces to a requirement ID via
`requirements/traceability.yaml` (DFT-1).

## Clean statement (SUB-5)

This repository contains **no** CALM-CFR+ solver code, **no** controlled
technical information, **no** real platform data, **no** real technical
orders, and **no** actual military identifiers. All equipment, parts,
procedures, personnel, and vendor data are synthetic and fictional.

## Architecture (summary)

A Readiness Orchestrator (manager agent — performs no domain work) decomposes
NMC events into exclusively-owned work packages for five specialists:
Maintenance, Supply, Workforce, Safety & Policy, and Cyber Trust. Messages are
versioned JSON contracts (`/contracts`) validated at publish and subscribe.
State, transactional inbox/outbox, audit trail, and the Logical Clock live in
Firestore; the bus is Pub/Sub with per-workflow ordering keys. Each role
deploys as its own Cloud Run service with its own service account; boundaries
are enforced at IAM. External documents pass a quarantine-first pipeline
(bounded parser → Model Armor → tool-less classifier) and only a structured
verdict plus tightly-typed safe metadata ever reaches the bus. Reasoning:
`gemini-3.5-flash` on Vertex AI, single pinned region (`us-central1`).

## Repository layout

    contracts/        versioned message schemas + examples (ICD-1..4)
    src/forge_common/ shared contract validation (ICD-2)
    services/         one directory per agent role (Cloud Run services)
    prompts/          versioned prompt files (PLT-1)
    requirements/     traceability.yaml (DFT-1)
    architecture/     manifest.yaml — service → module → diagram node (DFT-5)
    scripts/          CI gates and smoke tests
    infra/            GCP bootstrap (setup-gcp.sh)
    tests/            unit + contract tests (CI-3 seed)

## Spin-up

Local (no cloud needed for Lane-1 gates):

    python3 -m venv .venv && source .venv/bin/activate
    pip install -r requirements.lock && pip install -e . --no-deps
    python scripts/validate_contracts.py
    python scripts/check_traceability.py
    ruff check . && pytest

GCP bootstrap (prerequisites: authenticated principal, project ID, billing
enabled): see `infra/setup-gcp.sh`. Full one-command environment stand-up
(`deploy.sh`, PLT-6) lands Day 6–7 of the build plan.

## Process rules

PRs only — no direct commits to `main` (branch protection). New dependencies
need a one-line justification in the PR description (DFT-3). Work not
traceable to a requirement ID is rejected in review (DFT-2). After the Day-8
freeze (`v1.1-demo` tag), only demo-spine defects merge (DFT-4).
