#!/bin/zsh
# Read or answer a paused SWE-AF execution through the control plane's own
# routes — the same ones WorkAF's Needs you calls.
# Usage: answer.sh <exec> status | approve | changes "<words>" | reject ["<words>"]
set -euo pipefail
: ${KIT_DIR:=$HOME/af-proto-link/deploy/workaf-proto}
HERE=$KIT_DIR; source $KIT_DIR/env.sh
ex=${1:?execution id}; act=${2:-status}; words=${3:-}
key=$(<$LOCAL/cp/secrets/cpkey); api=http://127.0.0.1:$CP_PORT/api/v1/executions/$ex
case $act in
  status) curl -fsS -H "X-API-Key: $key" $api/approval-status; exit;;
  approve) d=approved;; changes) d=request_changes;; reject) d=rejected;;
  *) die "usage: answer.sh <exec> status|approve|changes <words>|reject [words]";;
esac
jq -n --arg d $d --arg r "$words" '{decision: $d, reason: $r}' |
  curl -fsS -H "X-API-Key: $key" -H 'Content-Type: application/json' -d @- $api/approval-response
