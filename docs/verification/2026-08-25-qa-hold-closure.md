# Entrant QA-HOLD closure — 2026-08-25

The entrant's independent read-only audit of public commit `829e510`
returned **QA HOLD**: five P1 and three P2 findings. All eight are closed
(PRs #37–#39; final candidate **`v1.3-demo.8` = `eb9b333`**), each with the
fix at the source and, where the entrant required it, regression tests.

## P1 — failure-injection race (recording-gate item 1)

*Reproduced by the entrant: `owner_state=FAILED`, package result still
`COMPLETED`.* The transactional ownership guard (`check_wp_status_update`)
now refuses results from a FAILED owner: the bundle never applies, the
refusal is audited (`RESULT_REFUSED_OWNER_FAILED`, blocked_action, the
workflow trace, the real logical day), and the package stays ASSIGNED for
the monitor's normal failure disposition (reserve deployment or BLOCKED).
Mid-flight failure injection is now *meaningful* in the demo rather than a
race. Regressions: `test_failed_owner_cannot_commit_a_success`,
`test_refused_package_is_timed_out_by_the_monitor`, plus the healthy-path
control.

## P1 — duplicate recoveries (recording-gate item 2)

*Reproduced by the entrant: two live workflows for `GX12-03`.* The console
now enforces ONE active recovery per vehicle: a per-equipment marker is
read-then-claimed inside the same Firestore transaction that creates the
workflow (`create_workflow(..., exclusive=True)`), so simultaneous starts
cannot both commit; the console returns 409 naming the existing recovery.
Markers pointing at terminal or deleted workflows are stale and pass.
Harness/driver creations remain non-exclusive by design (synthetic
fixtures legitimately revisit a vehicle). Regressions: duplicate-409,
cancel-frees, stale-marker, and direct-create tests.

## P1 — CI-6 missing

Implemented and **proven live**: `.github/workflows/deploy.yml` deploys
the entire PLT-6 `infra/deploy.sh` via **Workload Identity Federation**
(GitHub OIDC → `github-pool` → `forge-ci-deployer@`; zero service-account
keys in the repo or its secrets), then runs `scripts/ci6_smoke.py` — one
live-Gemini agent round-trip (the tool-less classifier on a benign
bulletin) and one Pub/Sub publish/consume, both asserted from Firestore.

- Green run: Actions **32855508933** (workflow_dispatch, 7m19s) —
  `ingest: 200 verdict_published=True`,
  `publish/consume proven: verdict 8799603a… -> safety audit`,
  `CI-6 SMOKE: PASS`.
- The path was earned honestly: five failing runs surfaced real
  fresh-runner gaps (Cloud Resource Manager + Cloud Billing APIs never
  enabled; missing datastore/pubsub-IAM/logging/build roles; interactive
  gcloud prompts; external-account credentials cannot mint OIDC tokens
  client-side — the auth action mints the smoke's ID token). Each gap is
  now fixed in project config or the workflow itself.
- The `v1.3-demo.8` tag push triggers a further run on the exact final
  candidate (`on: push: tags: v*`).

## P1 — misleading traceability

`pending_allowed` entries are now **`{id, until, reason}`** and the gate
FAILS on expiry — "strict PASS" can no longer coexist with open-ended
exemptions. CI-6, DFT-4, SUB-2, and SUB-8 are closed with real references;
only SUB-1/SUB-3/SUB-7 remain, dated 2026-08-31 with reasons (entrant
submission-portal actions).

## P1 — SUB-2 architecture gap

`docs/forge-architecture.svg` + `docs/forge-architecture.png` are now in
the repo (the entrant's storyline poster, consistent with
`architecture/manifest.yaml`). The SUBMISSION Mermaid is rerouted so every
agent edge transits the Pub/Sub node, and the caption now names the seven
actual Cloud Run services instead of claiming every box is one.

## P2 — governance drift / test isolation / public repo

`CLAUDE.md` rewritten to the v1.3 baseline with the standing invariants
(monotonic clock, terminal-state discipline, registry truth, console
honesty) and a deliberately drift-proof status section; DFT-4 reworded to
the tag series. The orchestrator `/tick` health probe is injectable
(`create_worker(health_prober=…)`) and the tick unit test stubs it — no
unit test can reach the network through registry endpoints. SUBMISSION.md
reflects the **public** repository.

## Re-acceptance on the final candidate

Suite **322 tests (301 unit + 21 real-client emulator)**; all 8 PR gates
plus the emulator suite run locally.

```
   project=forge-agentic-0821-6418
   candidate=eb9b3333508adf73945b9cd68883a5b6cb533d79
   [PASS] run 1/10  (54s)   [PASS] run 6/10  (52s)
   [PASS] run 2/10  (52s)   [PASS] run 7/10  (59s)
   [PASS] run 3/10  (51s)   [PASS] run 8/10  (56s)
   [PASS] run 4/10  (55s)   [PASS] run 9/10  (62s)
   [PASS] run 5/10  (53s)   [PASS] run 10/10 (76s)
ACCEPTANCE: PASS 10/10 consecutive on eb9b3333508adf73945b9cd68883a5b6cb533d79
```

First attempt, zero resets — the seventh consecutive first-attempt 10/10
in the v1.3 series, and the first run **on a fleet deployed by CI-6
itself**. Dress rehearsal: **PASS, 71 s** (budget 240 s).
