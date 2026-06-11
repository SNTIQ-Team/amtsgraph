#!/usr/bin/env bash
# Full-Germany harvest orchestrator. Every phase is resumable — re-running
# this script continues where it stopped.
set -uo pipefail
cd "$(dirname "$0")"

echo "=== [1/4] OpenPLZ (geo spine, all states) ==="
python3 fetch_openplz.py || exit 1

echo "=== [2/4] Justiz places (court register, ~8200 PLZ) ==="
python3 fetch_justiz.py --phase places --workers 6 --delay 0.25 || exit 1

echo "=== [3a/4] Justiz chains (background) + [3b/4] PVOG (parallel) ==="
python3 fetch_justiz.py --phase chains --workers 6 --delay 0.25 --sample 200 \
    > justiz_chains.log 2>&1 &
JUSTIZ_PID=$!
python3 fetch_pvog.py --workers 8 --delay 0.25 > pvog.log 2>&1 &
PVOG_PID=$!
wait $JUSTIZ_PID; JUSTIZ_RC=$?
wait $PVOG_PID;  PVOG_RC=$?
echo "justiz chains rc=$JUSTIZ_RC, pvog rc=$PVOG_RC"
[ $JUSTIZ_RC -ne 0 ] && echo "WARNING: justiz chains failed (see justiz_chains.log)"
[ $PVOG_RC -ne 0 ] && echo "WARNING: pvog failed (see pvog.log)"

echo "=== [3c/4] BA Jobcenter register (gE/zkT) ==="
python3 fetch_ba.py || exit 1

echo "=== [4/4] Build + validate ==="
python3 build_db.py || exit 1
echo "=== DONE ==="
