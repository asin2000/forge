#!/usr/bin/env bash
# FORGE acceptance harness (Day-8 freeze gate). Runs the live spine N times
# CONSECUTIVELY against the deployed fleet; every run must exit 0. The spines
# share the global Logical Clock, so they run strictly sequentially — never
# in parallel. Prints per-run pass/fail and wall-clock, and the candidate
# commit each run executed on (freeze requires a clean tree — asserted).
#
# Usage: PROJECT_ID=<id> [RUNS=10] bash scripts/acceptance_run.sh
set -euo pipefail

PROJECT_ID="${PROJECT_ID:?set PROJECT_ID}"
RUNS="${RUNS:-10}"
PYTHON="${PYTHON:-./.venv/bin/python}"

if [ -n "$(git status --porcelain)" ]; then
  echo "FATAL: working tree is dirty — freeze acceptance requires a clean candidate" >&2
  git status --short >&2
  exit 1
fi
CANDIDATE="$(git rev-parse HEAD)"
echo "== FORGE acceptance: ${RUNS} consecutive live spines"
echo "   project=${PROJECT_ID}"
echo "   candidate=${CANDIDATE}"

passed=0
for i in $(seq 1 "$RUNS"); do
  start=$(date +%s)
  if PROJECT_ID="$PROJECT_ID" "$PYTHON" -u scripts/demo_spine_live.py >"/tmp/forge-accept-${i}.log" 2>&1; then
    elapsed=$(( $(date +%s) - start ))
    passed=$((passed + 1))
    echo "   [PASS] run ${i}/${RUNS}  (${elapsed}s)"
  else
    elapsed=$(( $(date +%s) - start ))
    echo "   [FAIL] run ${i}/${RUNS}  (${elapsed}s) — see /tmp/forge-accept-${i}.log"
    grep -vE '^I[0-9]' "/tmp/forge-accept-${i}.log" | tail -20 >&2
    echo "ACCEPTANCE: FAIL at run ${i} (${passed}/${RUNS} passed) on ${CANDIDATE}"
    exit 1
  fi
done
echo "ACCEPTANCE: PASS ${passed}/${RUNS} consecutive on ${CANDIDATE}"
