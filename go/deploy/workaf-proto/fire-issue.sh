#!/bin/zsh
# Dispatch one GitHub issue to swe-planner.build_issue on the prototype control
# plane, exactly as WorkAF's agentfield step would. Prints the execution id.
# Usage: fire-issue.sh <owner/repo> <issue-number> [base-branch]
set -euo pipefail
: ${KIT_DIR:=$HOME/af-proto-link/deploy/workaf-proto}
HERE=$KIT_DIR; source $KIT_DIR/env.sh
repo=${1:?owner/repo}; num=${2:?issue number}; base=${3:-}
issue=$(gh issue view $num -R $repo --json title,body,url)
input=$(jq -n --argjson i "$issue" --arg repo "https://github.com/$repo" --arg base "$base" --argjson n $num \
  '{input: ({title: $i.title, body: $i.body, repo_url: $repo, issue_number: $n, issue_url: $i.url}
            + (if $base == "" then {} else {base_branch: $base} end))}')
print -r -- "$input" | jq -c .input >&2
curl -fsS -H "X-API-Key: $(<$LOCAL/cp/secrets/cpkey)" -H 'Content-Type: application/json' \
  -d "$input" http://127.0.0.1:$CP_PORT/api/v1/execute/async/swe-planner.build_issue | jq -r '.execution_id'
