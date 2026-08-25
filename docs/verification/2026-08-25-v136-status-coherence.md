# v1.3-demo.6 acceptance — status coherence (2026-08-25, evening)

Scope: three entrant-reported defects (PR #35, merged `c15af7b`, 8/8
gates), fixed in the runtime rather than painted over:

1. **Agents showed ACTIVE forever** — `claim_work_package` set instances
   ACTIVE with no production path back. A COMPLETED work package now
   returns its ACTIVE owner to IDLE in the same transaction via the
   sanctioned writer (`apply_wp_status_update`; owner + clock read in the
   check phase), audited `AGENT_DEACTIVATED` on the workflow trace at the
   real logical day. The console's status chips and live lane view can no
   longer conflict.
2. **`AGENT_ACTIVATED` audits were stamped day 0** — mis-sorting the
   trail (AUD-3) and breaking per-recovery day math ("Recovery day 315"
   on a 21-day recovery). Claim callers now pass the Logical Clock day
   read before the transaction; the console anchors recovery-day on
   `WORKFLOW_CREATED`.
3. **The detail pane rendered only on click** and went stale against the
   polled list (missing approval cards, wrong stage). It now re-renders
   whenever its payload actually changes.

The header Day chip (absolute sim day) is restored per the entrant;
relative per-recovery days remain everywhere else.

Tag **`v1.3-demo.6` = `c15af7b`**; suite 315 (294 unit + 21 emulator).

## CI-8 — 10/10 consecutive live spines (verbatim)

```
   project=forge-agentic-0821-6418
   candidate=c15af7b8bb93ebc818c06b32b1a48b900403056a
   [PASS] run 1/10  (55s)
   [PASS] run 2/10  (49s)
   [PASS] run 3/10  (53s)
   [PASS] run 4/10  (48s)
   [PASS] run 5/10  (52s)
   [PASS] run 6/10  (53s)
   [PASS] run 7/10  (48s)
   [PASS] run 8/10  (50s)
   [PASS] run 9/10  (48s)
   [PASS] run 10/10  (55s)
ACCEPTANCE: PASS 10/10 consecutive on c15af7b8bb93ebc818c06b32b1a48b900403056a
```

First attempt, zero resets — the sixth consecutive first-attempt 10/10 in
the v1.3 series. Dress rehearsal: **PASS, 78 s** (budget 240 s). These
runs exercise the new completion→IDLE path on every spine (three
specialist packages each), on the live fleet.
