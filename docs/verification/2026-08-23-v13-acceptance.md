# v1.3 operator-console acceptance — 2026-08-23

Scope: the HUM-3 amendment (PR #24, merged `7e19d47`, 8/8 CI gates) per the
entrant's reset rule — a fresh CI-8 acceptance on the exact tagged commit
before anything is recorded. Tag **`v1.3-demo` = `7e19d47`**; the fleet was
redeployed from it first (`infra/deploy.sh` full rerun; dashboard revision
`forge-dashboard-00017-2wv`; IAM delta applied: dashboard SA `run.invoker`
on `forge-cyber-trust`; `FORGE_CYBER_TRUST_URL` set).

Local gate state at candidate: 287 tests (266 unit + 21 real-client
emulator), ruff/format clean, vulture clean, contracts 31 schemas /
29 examples PASS, schema-versions PASS, traceability strict PASS (66
requirements), CI-9 config PASS.

## CI-8 — 10/10 consecutive live spines (verbatim)

```
== FORGE acceptance: 10 consecutive live spines
   project=forge-agentic-0821-6418
   candidate=7e19d47ceb3ad617b1343fa7bc076e6e3aebfea5
   [PASS] run 1/10  (47s)
   [PASS] run 2/10  (46s)
   [PASS] run 3/10  (46s)
   [PASS] run 4/10  (46s)
   [PASS] run 5/10  (45s)
   [PASS] run 6/10  (46s)
   [PASS] run 7/10  (54s)
   [PASS] run 8/10  (47s)
   [PASS] run 9/10  (46s)
   [PASS] run 10/10  (51s)
ACCEPTANCE: PASS 10/10 consecutive on 7e19d47ceb3ad617b1343fa7bc076e6e3aebfea5
```

First attempt, zero resets.

## Dress rehearsal (verbatim tail)

```
   [22:48:36Z] status=SUSPENDED_AWAITING_PART
   [22:48:44Z] status=AWAITING_RELEASE_APPROVAL
   decision recorded: {'approval_id': 'apr-6eae3b67-e492-5a2a-b68e-a47ce4c9fcbf', 'decision': 'approved', 'approver': 'armond.sinclair@gmail.com'}
   [22:48:45Z] status=AWAITING_RELEASE_APPROVAL
   [22:48:51Z] status=RELEASED
LIVE SPINE: PASS
== dress rehearsal wall-clock: 69s (budget 240s)
DRESS REHEARSAL: PASS (69s <= 240s)
```

## HUM-3 console rehearsal — every operator control, deployed (verbatim)

Driven through the same authenticated HTTP calls the dashboard buttons
make (identity token bearer = the forwarder path the recording uses):

```
== HUM-3 console rehearsal against https://forge-dashboard-3vnecimj5a-uc.a.run.app
1. started wf-op-385298fe5e (operator armond.sinclair@gmail.com)
2. bulletin doc-bulletin-73983ca7 verdict_published=True
   reached AWAITING_SCHEDULE_APPROVAL
   approved schedule_override as armond.sinclair@gmail.com
   reached SUSPENDED_AWAITING_PART
3. clock advanced 21 days -> Day 252 (due_events=1)
   reached AWAITING_RELEASE_APPROVAL
   approved equipment_release as armond.sinclair@gmail.com
   reached RELEASED
4. RELEASED; trail has 25 events; 6 carry the operator identity verbatim
5. wf-op-06ca8b66ed started then CANCELLED; second cancel correctly 409
6. after a live tick the cancelled workflow is inert: status CANCELLED, trail=['WORKFLOW_CREATED', 'WORKFLOW_CANCELLED']
7. agent-workforce-02 FAILED then restored to RESERVE (ORC-3 topology intact)
8. cleaned rehearsal workflows; logical clock reset to Day 0
CONSOLE REHEARSAL: PASS
```

Notes, stated precisely: the console spine included the poisoned-bulletin
injection into the SAME workflow — the trail carries WORKFLOW_CREATED,
OPERATOR_BULLETIN_INJECTED, DOCUMENT_QUARANTINED, ACTION_VETOED,
SCHEDULE_OVERRIDE_APPROVED, PART_ETA_REACHED, RELEASE_APPROVED (asserted by
the rehearsal script before printing PASS). "Day 252" reflects the shared
live clock after ten acceptance spines; it was reset to Day 0 at cleanup,
which is the recording precondition. The cancelled workflow's trail shows
the cancel landed before its NMC event was consumed; the late-trigger
audited no-op (NMC_EVENT_STALE) is proven at unit and emulator level
(`tests/test_day9_operator_controls.py`,
`test_day9_operator_console_start_cancel_real_client`).

State after evidence capture: rehearsal workflows deleted, logical clock at
Day 0, all instances at seed states (workforce-02 RESERVE), `forge-tick`
running, fleet = `v1.3-demo` runtime. Ready to record.
