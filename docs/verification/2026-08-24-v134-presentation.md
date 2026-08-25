# v1.3-demo.4 presentation-layer acceptance — 2026-08-24 (evening)

Scope: the entrant's product-presentation review implemented in full (PR
#31, merged `6eddd34`, 8/8 gates) — mission-story panel, plain-English
agent lanes, five hero-moment banners, readiness payoff — plus **20
confirmed findings** from a 27-agent pre-merge adversarial review, all
closed. The two P0-honesty catches: the SAFETY VETO callout rendered empty
on the flagship beat (the veto audit never carried its reasons — fixed at
the source in `services/safety/handlers.py`, the veto's WHY now commits
verbatim in the audit detail, regression-locked); and cancelling a
recovery fired the readiness celebration (`/api/fleet` now reports each MC
vehicle's `last_outcome`; the payoff requires RELEASED).

Tag **`v1.3-demo.4` = `6eddd34`**. Suite at candidate: **312 tests
(291 unit + 21 real-client emulator)**, all local gates green.

## CI-8 — 10/10 consecutive live spines (verbatim)

```
== FORGE acceptance: 10 consecutive live spines
   project=forge-agentic-0821-6418
   candidate=6eddd34e4f0ece89731e35f5a9ff022a0d64dfcd
   [PASS] run 1/10  (62s)
   [PASS] run 2/10  (57s)
   [PASS] run 3/10  (55s)
   [PASS] run 4/10  (50s)
   [PASS] run 5/10  (50s)
   [PASS] run 6/10  (51s)
   [PASS] run 7/10  (57s)
   [PASS] run 8/10  (58s)
   [PASS] run 9/10  (49s)
   [PASS] run 10/10  (52s)
ACCEPTANCE: PASS 10/10 consecutive on 6eddd34e4f0ece89731e35f5a9ff022a0d64dfcd
```

First attempt, zero resets — the fourth consecutive first-attempt 10/10
across the v1.3 tag series. Dress rehearsal same evening: **PASS, 72 s**
(budget 240 s).

## Live presentation verification (deployed fleet)

A console-started recovery (`GX12-09`) with the hostile bulletin injected,
captured at the schedule gate through the operator's authenticated route:
the mission-story panel reads "GX12-09 · NON-MISSION-CAPABLE", the stage
rail pulses on "Schedule approval", CURRENT ACTION "Awaiting your decision
— schedule_override" OWNED BY "YOU (human gate)", the View-distributed-
trace link resolves, the approval card renders the full HUM-2 record with
enlarged Approve/Reject, and the lanes read as human activity ("Screening
verdict published", "Delivered its work product", safety pulsing
"Validating against safety policies" with "VETOED an unauthorized action"
in its ticker). The header shows "✓ operator authenticated" — no personal
email anywhere in the DOM. Rehearsal workflow cleaned; logical clock reset
to Day 0; the entrant's own live workflows left untouched.
