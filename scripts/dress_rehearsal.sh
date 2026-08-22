#!/usr/bin/env bash
# FORGE ≤4-minute dress rehearsal (Day-8, DFT-4). The demo story end to end
# against the deployed fleet, timed as ONE take:
#   Scene 1 — a poisoned vendor bulletin caught (quarantine -> Model Armor ->
#             classifier -> Safety veto), raw text never escaping;
#   Scene 2 — the full recovery spine (NMC -> 21-day suspension -> RELEASED)
#             with both human approvals through the deployed dashboard.
# Asserts the total wall-clock is at or under 4:00 (240s).
#
# Usage: PROJECT_ID=<id> bash scripts/dress_rehearsal.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
PYTHON="${PYTHON:-./.venv/bin/python}"
BUDGET=240

echo "== FORGE dress rehearsal (target <= ${BUDGET}s) project=${PROJECT_ID}"
start=$(date +%s)

echo "-- Scene 1: bulletin screening + Safety veto"
PROJECT_ID="$PROJECT_ID" "$PYTHON" -u scripts/demo_scene1_live.py 2>&1 | grep -E "LIVE SCENE 1|Safety veto|model_armor=" || {
  echo "DRESS REHEARSAL: FAIL (Scene 1)"; exit 1; }

echo "-- Scene 2: NMC -> RELEASED with two human approvals"
PROJECT_ID="$PROJECT_ID" "$PYTHON" -u scripts/demo_spine_live.py 2>&1 | grep -E "LIVE SPINE: PASS|status=(RELEASED|SUSPENDED|AWAITING)|decision recorded" || {
  echo "DRESS REHEARSAL: FAIL (Scene 2)"; exit 1; }

elapsed=$(( $(date +%s) - start ))
echo "== dress rehearsal wall-clock: ${elapsed}s (budget ${BUDGET}s)"
if [ "$elapsed" -le "$BUDGET" ]; then
  echo "DRESS REHEARSAL: PASS (${elapsed}s <= ${BUDGET}s)"
else
  echo "DRESS REHEARSAL: OVER BUDGET (${elapsed}s > ${BUDGET}s)"
  exit 1
fi
