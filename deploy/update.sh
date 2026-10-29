#!/usr/bin/env bash
# Auto-deploy, run by shasta-world-update.timer as the shasta user: if main has moved, test the new
# commit in a scratch copy and switch to it only if every test passes. Nobody needs to SSH in to ship.
set -euo pipefail

APP=${APP:-/opt/shasta-world}
STATE=${STATE:-/var/lib/shasta-world/last-failed-commit}
cd "$APP"

git fetch -q origin main
NEW=$(git rev-parse origin/main)
OLD=$(git rev-parse HEAD)
[ "$NEW" = "$OLD" ] && exit 0
if [ -f "$STATE" ] && [ "$(cat "$STATE")" = "$NEW" ]; then
  exit 0      # already failed its tests; wait for the next commit
fi

echo "main moved: ${OLD:0:7} -> ${NEW:0:7}. Testing it."
TMP=$(mktemp -d)
trap 'rm -rf "$TMP"' EXIT
git archive "$NEW" | tar -x -C "$TMP"

if (cd "$TMP" && python3 tests/contract_test.py && python3 tests/client_test.py && python3 tests/map_test.py \
      && python3 tests/chaos_sim.py 1000 1); then
  git -c advice.detachedHead=false checkout -q --detach "$NEW"
  rm -f "$STATE"
  sudo /usr/bin/systemctl restart shasta-world
  echo "deployed ${NEW:0:7}"
else
  echo "$NEW" > "$STATE"
  echo "tests failed on ${NEW:0:7}; staying on ${OLD:0:7}"
  exit 1
fi
