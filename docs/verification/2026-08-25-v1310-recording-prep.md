# v1.3-demo.10 — pre-recording defect fix and recording prep — 2026-08-25

While preparing to record against `.9`, the entrant reported three
console observations. Two were behavior-as-designed; one was a real
presentation defect, fixed and redeployed as `v1.3-demo.10`.

## The defect: veto card outlived the recovery

The SAFETY VETO callout (his own review item — "surface veto reason")
rendered for the workflow's whole life, so a RELEASED recovery whose
story included a hostile-bulletin beat still carried the red veto card
under its "MISSION CAPABLE — RELEASED" headline — muddying the readiness
payoff frame the recording builds toward. Fix (one guard in
`vetoCallout`): the callout renders only while the recovery is live; the
audit-trail table below remains the veto's permanent record.

- PR #43 → `main` = **`d15d80e`**; all eight PR gates green including
  the real-emulator integration gate (5m17s). Pins bumped to `.10` in
  the same commit (pins precede the tag).
- Tag **`v1.3-demo.10`** = `d15d80e78ac805892a6f553a4b5779a2a11ce78f`.
- CI-6 WIF deploy:
  <https://github.com/asin2000/forge/actions/runs/32880336776> —
  **success** (fourth consecutive green CI-6 run). Build digest
  `sha256:bb5b0ab5b7b0155f4626aef4138d9d5390a19bed733f31164a830ed7751dbc37`;
  all seven services confirmed on new revisions serving exactly that
  digest at 100% traffic (orchestrator 00025-frp, maintenance 00024-sj8,
  supply 00024-bb7, workforce 00024-gcf, safety 00024-gjb, cyber-trust
  00017-phq, dashboard 00028-hh8).
- Fix verified live in a browser against the exact workflow from the
  defect report (`wf-op-cc2c9ea50e`, GX12-07, RELEASED with a veto in
  its trail): headline and story render with **no** veto card; the veto
  events remain in the audit-trail table.

## Acceptance + rehearsal on the deployed `.10`

```
ACCEPTANCE: PASS 10/10 consecutive on d15d80e78ac805892a6f553a4b5779a2a11ce78f
(runs 61/66/54/57/64/63/58/53/104/73s)
DRESS REHEARSAL: PASS (82s <= 240s)
```

The **ninth** consecutive first-attempt 10/10 of the v1.3 series; the
third on a CI-6-deployed fleet.

## Behavior-as-designed (no change)

- **Fleet tiles are click-to-open-recovery**: a tile carries a click
  only while its vehicle has an active recovery; with the fleet fully
  idle nothing is clickable, and the tile becomes clickable the moment
  a recovery starts.
- **The "stuck" veto message** was the detail pane auto-selecting the
  most recent workflow, whose story legitimately included the veto —
  the defect above made it persist past release; the auto-select itself
  is intended.

## Logical Clock re-epoch (recording exception, audited)

The entrant required the demo to open at Day 1. The never-reset rule
exists because a reset once stranded a live workflow 357 days from its
part; both re-epochs today were performed only after a transactional
guard proved **zero non-terminal workflows** exist, and each was
recorded in the same transaction as a contract-conforming
`CLOCK_REEPOCHED` audit under `wf-system-clock` (operator identity,
from/to values) — reconstructable from Firestore alone, like every
other clock movement:

- 1071 → 1 (pre-`.10`, after the entrant's practice takes), and
- 232 → 1 (after the `.10` validation spines advanced the fresh clock).

Historical audit day-stamps on old terminal workflows are unchanged and
remain internally consistent per workflow.

## Final recording state

Fleet 12/12 MISSION_CAPABLE, no live workflows, all agent instances
IDLE/RESERVE and HEALTHY, Logical Clock at **Day 1**, dashboard serving
`.10`. The video, the deployed runtime, the final tag, and the
submission repository all correspond to `d15d80e`.
