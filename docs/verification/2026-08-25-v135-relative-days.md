# v1.3-demo.5 acceptance — relative days + status-first lanes (2026-08-25)

Scope: two entrant corrections (PR #33, merged `371afc8`, 8/8 gates).

**Root cause acknowledged:** the global logical clock had been treated as
resettable. Acceptance runs advanced it to Day 567; a "recording
readiness" reset pulled it back to Day 0 while a live operator workflow
(approved resume-day 357 baked into its plan) was suspended — stranding it
357 sim-days from its part, which the absolute-day UI then displayed
verbatim ("waiting 300+ days"). Fixes: the clock is **never reset**
(runbook precondition replaced with stale-workflow cleanup); every
viewer-facing day count is now **relative to its recovery** ("Recovery day
N · part due in M days"); the absolute sim day renders only as a small
label beside the Advance control. The stranded workflow was cancelled
(audited, operator principal) and demo residue cleared.

**Lanes corrected per the entrant:** current status leads (state + health
chips in the lane header; "Idle — ready" / "Standing by (reserve)" /
"FAILED — restore required"), with the last outcome as a secondary
"last: …" line — outcomes augment status, never replace it.

Tag **`v1.3-demo.5` = `371afc8`**; suite 312 (291 unit + 21 emulator).

## CI-8 — 10/10 consecutive live spines (verbatim)

```
   project=forge-agentic-0821-6418
   candidate=371afc82f9e94bcb594a86447e63a33a8a887165
   [PASS] run 1/10  (87s)
   [PASS] run 2/10  (58s)
   [PASS] run 3/10  (56s)
   [PASS] run 4/10  (49s)
   [PASS] run 5/10  (45s)
   [PASS] run 6/10  (52s)
   [PASS] run 7/10  (47s)
   [PASS] run 8/10  (48s)
   [PASS] run 9/10  (47s)
   [PASS] run 10/10  (48s)
ACCEPTANCE: PASS 10/10 consecutive on 371afc82f9e94bcb594a86447e63a33a8a887165
```

First attempt, zero resets — the fifth consecutive first-attempt 10/10 in
the v1.3 series. Dress rehearsal: **PASS, 78 s** (budget 240 s). Note the
acceptance itself now runs against a monotonic clock (it advanced during
the run and was NOT reset afterward) — the relative-day display is what
makes that coherent, which is the point of the change.
