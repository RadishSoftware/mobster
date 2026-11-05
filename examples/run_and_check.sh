#!/usr/bin/env bash
# Run one task from a script, keep its events, and act on how it ended.
#
#   examples/run_and_check.sh "Turn on Dark Mode"
#
# Needs jq, a phone at $MOBSTER_WDA_URL (default 127.0.0.1:8100) and $MOBSTER_ENV_FILE with your Claude or
# OpenAI key. With --json nobody can answer an approval, so a send, buy, post or delete is declined.
set -uo pipefail

task=${1:?usage: run_and_check.sh "TASK"}
events=$(mktemp -t mobster-run)

# Act. Exit code 3 means no phone answered.
mobster run "$task" --execute --json > "$events"
code=$?
if [ "$code" -eq 3 ]; then
  echo "No phone at ${MOBSTER_WDA_URL:-http://127.0.0.1:8100}. Run: mobster doctor" >&2
  exit 3
fi
jq -c 'select(.event == "result") | {status, reason, data}' "$events"

if [ "$code" -eq 0 ]; then
  echo "Done. Events are in $events"
else
  echo "The task did not finish (exit $code). Events are in $events" >&2
fi
exit "$code"
