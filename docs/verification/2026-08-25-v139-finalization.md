# v1.3-demo.9 finalization — 2026-08-25

The entrant's QA PASS on PR #41 left one temporary condition: `main`
described `v1.3-demo.9` as frozen and accepted before the tag, the
deployment, and the acceptance evidence existed. This record closes that
condition. Sequence executed on the entrant's instruction (relayed with
Codex's checklist), in order, with no runtime changes beyond the tag-
triggered deployment itself.

## 1. Tag

`v1.3-demo.9` = **`b4e607036adddd47d7557a600d4d1a490ed84458`** (annotated,
on `main`, the PR #41 merge). The README/SUBMISSION pins in that commit
already name `.9` — the tagged tree self-describes (pins-precede-tag).

## 2. CI-6 deployment (Lane 2, keyless)

Tag push triggered the WIF deploy:
<https://github.com/asin2000/forge/actions/runs/32863939575> —
**success**, headSha `b4e6070…8458`. The run checked out the tag
(`HEAD is now at b4e6070`), ran `infra/deploy.sh` end-to-end via
GitHub OIDC → `github-pool` → `forge-ci-deployer@` (zero SA keys), then
`scripts/ci6_smoke.py` (live Gemini classifier round-trip + Pub/Sub
publish/consume, asserted from Firestore) — the third consecutive green
CI-6 run (32855508933 dispatch, 32857591642 tag `.8`, 32863939575 tag `.9`).

## 3. Deployed-revision confirmation

Cloud Build in that run pushed image digest
`sha256:e26406eda1bede24f559889f5060b6e681ed43d84d7e2ad9e863d5dc65680f42`
(from the run log). All seven services flipped to new revisions inside
the run's window (15:13–15:16 UTC) serving **exactly that digest**, 100%
traffic:

| service | revision | created (UTC) |
|---|---|---|
| forge-orchestrator | 00024-khs | 15:13:23 |
| forge-maintenance | 00023-wfd | 15:13:58 |
| forge-supply | 00023-fc9 | 15:14:15 |
| forge-workforce | 00023-2pf | 15:14:34 |
| forge-safety | 00023-nvb | 15:14:56 |
| forge-cyber-trust | 00016-tc9 | 15:15:41 |
| forge-dashboard | 00027-mn6 | 15:15:57 |

Chain: tag `b4e6070` → CI checkout `b4e6070` → build digest `e26406ed…` →
all seven revisions at `e26406ed…`. The fleet runs the `.9` candidate.

## 4. Live duplicate-start rejection (cleanup item 3, on the deployed runtime)

Against the deployed dashboard through the authenticated forwarder:

- Start on idle `GX12-11` → `200`, `wf-op-7d0b345f7c`, INTAKE.
- Second start on `GX12-11` → **`409`**: "GX12-11 already has an active
  recovery (wf-op-7d0b345f7c, INTAKE)".
- `wf-op-7d0b345f7c`'s audit trail then held **exactly one**
  `OPERATOR_START_REJECTED` event: `event_kind=blocked_action`,
  `agent_identity=forge-approval-surface`,
  `detail={"operator": "armond.sinclair@gmail.com"}`, `effective_at=777`
  (the real Logical Clock day), `observed_at=2026-08-25T15:19:17Z`.
- Cleanup by the audited terminal path: cancel → `CANCELLED`; fleet
  returned to 12/12 MISSION_CAPABLE. `GX12-10`'s `RELEASED` outcome
  (the entrant's own run) was untouched; `GX12-11` truthfully records
  `last_outcome=CANCELLED` from this verification.

The HUM-3/AUD-1 claim — refused duplicate starts are audited on the
active recovery's trail — now holds **on the deployed runtime**, not
only in tests.

## 5. Acceptance + rehearsal on the deployed `.9`

`PROJECT_ID=… RUNS=10 bash scripts/acceptance_run.sh` on candidate
`b4e6070…` (clean tree):

```
== FORGE acceptance: 10 consecutive live spines
   project=forge-agentic-0821-6418
   candidate=b4e607036adddd47d7557a600d4d1a490ed84458
   [PASS] run 1/10  (61s)
   [PASS] run 2/10  (55s)
   [PASS] run 3/10  (53s)
   [PASS] run 4/10  (54s)
   [PASS] run 5/10  (59s)
   [PASS] run 6/10  (53s)
   [PASS] run 7/10  (53s)
   [PASS] run 8/10  (56s)
   [PASS] run 9/10  (55s)
   [PASS] run 10/10  (55s)
ACCEPTANCE: PASS 10/10 consecutive on b4e607036adddd47d7557a600d4d1a490ed84458
```

**PASS 10/10, first attempt** — the **eighth** consecutive first-attempt
10/10 across the v1.3 series, and the second on a CI-6-deployed fleet.

`bash scripts/dress_rehearsal.sh`: **PASS, 73s wall-clock (budget 240s) — Scene 1 bulletin veto (rules SP-SEC-004, SP-PART-001) and the full NMC→RELEASED spine with both human approvals**.

## Closure

With this record committed (documentation-only; deliberately no `.10` —
post-acceptance evidence does not change the candidate), every claim in
the `.9` pins is backed by dated evidence: the tag exists at the pinned
SHA, CI-6 deployed it keylessly and green, all seven services serve its
image, the audited duplicate-start rejection is verified live, and the
fresh 10/10 + rehearsal passed on the deployed runtime. Recording may
proceed against the exact final runtime, pending the entrant's (Codex's)
independent audit of this state.
