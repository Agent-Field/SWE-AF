#!/usr/bin/env bash
# run.sh — one-command, zero-LLM E2E harness for the SWE-AF Go port.
#
# Runs the ENTIRE swe-planner pipeline (plan -> DAG execute -> verify -> fix
# loop -> finalize -> push -> draft/PR -> CI gate) against a real GitHub repo in
# ~2-5 min with NO LLM calls, by shimming the `claude` CLI with mockclaude.
#
# Usage:  ./run.sh [--keep]
#   --keep   leave BOTH the control plane and the swe-planner node running for
#            UI inspection at http://localhost:18080 (default kills the planner
#            but leaves the control plane up).
set -uo pipefail

# ---------------------------------------------------------------------------
# Paths / config
# ---------------------------------------------------------------------------
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GO_ROOT="$(cd "$HERE/../.." && pwd)"          # .../SWE-AF/go
TS="$(date +%Y%m%d-%H%M%S)"
RUN_DIR="$HERE/runs/$TS"
SHIM="$RUN_DIR/shim"
STATE="$RUN_DIR/state"
CP_PORT=18080
PLANNER_PORT=18003
CP_URL="http://localhost:$CP_PORT"
PLANNER_URL="http://localhost:$PLANNER_PORT"
GH_OWNER="AbirAbbas"
REPO="swe-af-e2e-mock"
REPO_FULL="$GH_OWNER/$REPO"
REPO_URL="https://github.com/$REPO_FULL"
KEEP=0
[[ "${1:-}" == "--keep" ]] && KEEP=1

mkdir -p "$SHIM" "$STATE"
PLANNER_PID=""
CP_STARTED=0

log()  { printf '\033[1;36m[e2e]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
err()  { printf '\033[1;31m[FAIL]\033[0m %s\n' "$*" >&2; }

ASSERT_FAILS=0
assert() { # assert <desc> <condition-exit-code-in-$?>
  if [[ "$1" -eq 0 ]]; then ok "$2"; else err "$2"; ASSERT_FAILS=$((ASSERT_FAILS+1)); fi
}

cleanup() {
  # Kill the planner (bracketed pattern so we never match this shell).
  [[ -n "$PLANNER_PID" ]] && kill "$PLANNER_PID" 2>/dev/null
  pkill -f "e2e-fast/runs/.*/[s]we-planner" 2>/dev/null
  if [[ "$KEEP" -eq 1 ]]; then
    log "--keep: leaving control plane ($CP_URL) and planner up for inspection"
  else
    log "planner stopped; control plane left running at $CP_URL (Ctrl-C its process to stop)"
  fi
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# 1. Build mockclaude (as `claude` shim) + swe-planner
# ---------------------------------------------------------------------------
log "building mockclaude -> $SHIM/claude"
( cd "$GO_ROOT" && go build -o "$SHIM/claude" ./test/mockclaude/ ) || { err "mockclaude build failed"; exit 1; }
log "building swe-planner"
( cd "$GO_ROOT" && go build -o "$RUN_DIR/swe-planner" ./cmd/swe-planner/ ) || { err "swe-planner build failed"; exit 1; }

# Materialize the scenario JSON in sync with the mock's baked default.
"$SHIM/claude" -dump-scenario > "$RUN_DIR/scenario.json"
log "scenario -> $RUN_DIR/scenario.json"

# ---------------------------------------------------------------------------
# 2. GitHub auth for git push over https
# ---------------------------------------------------------------------------
gh auth setup-git 2>/dev/null || true
GH_TOKEN_VAL="$(gh auth token)"

# ---------------------------------------------------------------------------
# 3. Control plane on :18080 (reuse if already listening)
# ---------------------------------------------------------------------------
if curl -sf --connect-timeout 3 -m 8 "$CP_URL/health" >/dev/null 2>&1; then
  log "control plane already up at $CP_URL — reusing"
else
  log "starting control plane: af server --port $CP_PORT"
  setsid nohup af server --port "$CP_PORT" --open=false > "$RUN_DIR/cp.log" 2>&1 &
  CP_STARTED=1
  for i in $(seq 1 60); do
    curl -sf --connect-timeout 3 -m 8 "$CP_URL/health" >/dev/null 2>&1 && break
    sleep 1
  done
  curl -sf --connect-timeout 3 -m 8 "$CP_URL/health" >/dev/null 2>&1 || { err "control plane did not come up (see $RUN_DIR/cp.log)"; exit 1; }
fi
ok "control plane healthy ($CP_URL)"

# ---------------------------------------------------------------------------
# 4. Ensure a clean private test repo (do NOT touch other e2e repos)
# ---------------------------------------------------------------------------
log "ensuring clean repo $REPO_FULL"
if ! gh repo view "$REPO_FULL" >/dev/null 2>&1; then
  gh repo create "$REPO_FULL" --private --add-readme >/dev/null || { err "cannot create $REPO_FULL"; exit 1; }
  sleep 2
fi
# Reset to a single clean seed commit on the default branch and drop stale
# feature/issue branches + open PRs from prior runs.
RESET_DIR="$RUN_DIR/reset-repo"
git clone --quiet "https://x-access-token:${GH_TOKEN_VAL}@github.com/${REPO_FULL}.git" "$RESET_DIR" 2>/dev/null || {
  err "clone for reset failed"; exit 1; }
(
  cd "$RESET_DIR"
  DEF_BRANCH="$(git symbolic-ref --quiet --short HEAD 2>/dev/null || echo main)"
  git checkout --quiet --orphan clean-slate
  git rm -rqf . 2>/dev/null || true
  printf '# %s\n\nDeterministic E2E fixture repo for the SWE-AF Go port harness.\n' "$REPO" > README.md
  git add README.md
  git -c user.email=swe-af-mock@example.com -c user.name="SWE-AF Mock" commit --quiet -m "chore: reset e2e fixture"
  git branch -M "$DEF_BRANCH"
  git push --quiet --force origin "$DEF_BRANCH"
  # Delete every other remote branch (closes any stale PRs).
  for b in $(git ls-remote --heads origin | awk '{print $2}' | sed 's#refs/heads/##'); do
    [[ "$b" != "$DEF_BRANCH" ]] && git push --quiet origin --delete "$b" 2>/dev/null || true
  done
)
ok "repo reset to clean seed commit"

# ---------------------------------------------------------------------------
# 5. Start swe-planner (:18003) with the claude shim on PATH
# ---------------------------------------------------------------------------
log "starting swe-planner on :$PLANNER_PORT (shim=$SHIM)"
PATH="$SHIM:$PATH" \
  AGENTFIELD_SERVER="$CP_URL" \
  AGENT_CALLBACK_URL="$PLANNER_URL" \
  NODE_ID="swe-planner" \
  PORT="$PLANNER_PORT" \
  GH_TOKEN="$GH_TOKEN_VAL" \
  SWE_MOCK_SCENARIO="$RUN_DIR/scenario.json" \
  SWE_MOCK_STATE_DIR="$STATE" \
  setsid nohup "$RUN_DIR/swe-planner" > "$RUN_DIR/planner.log" 2>&1 &
PLANNER_PID=$!

for i in $(seq 1 60); do
  curl -sf --connect-timeout 3 -m 8 "$PLANNER_URL/health" >/dev/null 2>&1 && break
  sleep 1
done
curl -sf --connect-timeout 3 -m 8 "$PLANNER_URL/health" >/dev/null 2>&1 || { err "planner did not come up (see $RUN_DIR/planner.log)"; exit 1; }
# Wait until the CP knows the planner's reasoners.
for i in $(seq 1 30); do
  RC="$(curl -s --connect-timeout 3 -m 30 "$CP_URL/api/v1/nodes/swe-planner" | grep -c run_coder || true)"
  [[ "$RC" -ge 1 ]] && break
  sleep 1
done
ok "swe-planner registered (pid $PLANNER_PID)"

# ---------------------------------------------------------------------------
# 6. Kick off the async build and poll to terminal
# ---------------------------------------------------------------------------
GOAL="$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1]))["goal"])' "$RUN_DIR/scenario.json" 2>/dev/null)"
[[ -z "$GOAL" ]] && GOAL="Create a small Python utility package with four independent helper modules and unit tests."

read -r -d '' BODY <<JSON
{"input":{"goal":$(python3 -c 'import json,sys;print(json.dumps(sys.argv[1]))' "$GOAL"),
"repo_url":"$REPO_URL",
"config":{"runtime":"claude_code","check_ci":true,"ci_wait_seconds":25,"ci_poll_seconds":5,"ci_startup_grace_seconds":5,"max_verify_fix_cycles":1}}}
JSON

log "POST /api/v1/execute/async/swe-planner.build"
RESP="$(curl -s --connect-timeout 3 -m 30 -X POST "$CP_URL/api/v1/execute/async/swe-planner.build" \
  -H 'Content-Type: application/json' -d "$BODY")"
EXEC_ID="$(printf '%s' "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("execution_id",""))' 2>/dev/null)"
WF_ID="$(printf '%s' "$RESP" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("workflow_id",""))' 2>/dev/null)"
if [[ -z "$EXEC_ID" ]]; then err "no execution_id (resp: $RESP)"; exit 1; fi
log "execution_id=$EXEC_ID workflow_id=$WF_ID"

STATUS=""; RESULT_JSON=""
START=$(date +%s)
for i in $(seq 1 200); do   # 200 * 3s = 10 min cap
  REC="$(curl -s --connect-timeout 3 -m 30 "$CP_URL/api/v1/executions/$EXEC_ID")"
  STATUS="$(printf '%s' "$REC" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status",""))' 2>/dev/null)"
  case "$STATUS" in
    succeeded|failed|cancelled|timeout|error)
      RESULT_JSON="$REC"; break;;
  esac
  sleep 3
done
WALL=$(( $(date +%s) - START ))
log "terminal status=$STATUS after ${WALL}s"

# Persist the terminal record + mock logs for inspection.
printf '%s' "$RESULT_JSON" > "$RUN_DIR/execution.json"

# ---------------------------------------------------------------------------
# 7. Assertions
# ---------------------------------------------------------------------------
echo; log "==================== ASSERTIONS ===================="

# (a) build succeeded
[[ "$STATUS" == "succeeded" ]]; assert $? "execution terminal status = succeeded (got: $STATUS)"
SUCCESS="$(printf '%s' "$RESULT_JSON" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("result",{}).get("success",""))' 2>/dev/null)"
[[ "$SUCCESS" == "True" ]]; assert $? "BuildResult.success = true (got: $SUCCESS)"

# (b) draft/PR created — capture URL
PR_URL="$(printf '%s' "$RESULT_JSON" | python3 -c 'import json,sys;r=json.load(sys.stdin).get("result",{});print(r.get("pr_url") or "")' 2>/dev/null)"
[[ -n "$PR_URL" ]]; assert $? "PR created: ${PR_URL:-<none>}"
if [[ -n "$PR_URL" ]]; then
  gh pr view "$PR_URL" --json state,isDraft,headRefName >/dev/null 2>&1; assert $? "PR is viewable via gh"
  HEAD_BRANCH="$(gh pr view "$PR_URL" --json headRefName -q .headRefName 2>/dev/null)"
fi

# (c) pushed branch has the expected files
if [[ -n "${HEAD_BRANCH:-}" ]]; then
  FOUND=0
  for f in mockpkg/alpha.py mockpkg/beta.py mockpkg/gamma.py mockpkg/delta.py; do
    gh api "repos/$REPO_FULL/contents/$f?ref=$HEAD_BRANCH" >/dev/null 2>&1 && FOUND=$((FOUND+1))
  done
  [[ "$FOUND" -eq 4 ]]; assert $? "pushed branch $HEAD_BRANCH has all 4 helper modules (found $FOUND/4)"
fi

# (d) DAG child executions include the expected reasoner set
DAG="$(curl -s --connect-timeout 3 -m 30 "$CP_URL/api/ui/v1/workflows/$WF_ID/dag" 2>/dev/null)"
printf '%s' "$DAG" > "$RUN_DIR/workflow_dag.json"
REASONER_SET="$(python3 - "$RUN_DIR/workflow_dag.json" <<'PY' 2>/dev/null
import json,sys
d=json.load(open(sys.argv[1]))
seen=set()
def walk(n):
    if not isinstance(n,dict): return
    r=n.get("reasoner_id")
    if r: seen.add(r)
    for c in n.get("children",[]) or []: walk(c)
walk(d.get("dag",{}))
for t in d.get("timeline",[]) or []: walk(t)
print(",".join(sorted(seen)))
PY
)"
for want in run_product_manager run_architect run_tech_lead run_sprint_planner \
            run_git_init run_workspace_setup run_coder run_code_reviewer run_qa \
            run_qa_synthesizer run_issue_advisor run_merger run_verifier \
            generate_fix_issues run_github_pr run_ci_watcher; do
  printf '%s' "$REASONER_SET" | grep -qw "$want"; assert $? "DAG has child execution: $want"
done

# (e) scenario control-loop paths fired (from the mock invocation log)
LOG="$STATE/invocations.jsonl"
scenario_check() { # <desc> <python-expr-over-lines>
  local desc="$1"; shift
  python3 - "$LOG" "$@" >/dev/null 2>&1; assert $? "$desc"
}
python3 - "$LOG" <<'PY' >/dev/null 2>&1
import json,sys
lines=[json.loads(l) for l in open(sys.argv[1]) if l.strip()]
def dec(role,issue): return [x["decision"] for x in lines if x["role"]==role and x.get("issue")==issue]
assert dec("code_reviewer","alpha")[:2]==["fix","approve"], ("alpha", dec("code_reviewer","alpha"))
assert dec("code_reviewer","gamma")[:2]==["block","approve"], ("gamma", dec("code_reviewer","gamma"))
assert any(x["role"]=="issue_advisor" and x["decision"]=="retry_modified" for x in lines), "advisor"
vd=[x["decision"] for x in lines if x["role"]=="verifier"]
assert vd[:2]==["failed","passed"], ("verifier", vd)
assert any(x["role"]=="github_pr" and x["decision"]=="created" for x in lines), "github_pr"
PY
assert $? "scenario paths fired: reviewer fix→approve (alpha), block→approve (gamma), advisor retry_modified, verifier failed→passed, github_pr created"

# CI no_checks path (repo has no workflows) via the real ci watcher
CI_STATUS="$(printf '%s' "$RESULT_JSON" | python3 -c 'import json,sys
r=json.load(sys.stdin).get("result",{})
g=r.get("ci_gate_results") or []
print((g[0].get("final_status") or g[0].get("watch",{}).get("status")) if g else "")' 2>/dev/null)"
[[ "$CI_STATUS" == *no_checks* ]]; assert $? "CI gate reached no_checks path (got: ${CI_STATUS:-<none>})"

# (f) notes were recorded for the build execution
NOTES_N="$(curl -s --connect-timeout 3 -m 30 "$CP_URL/api/v1/executions/$EXEC_ID/notes" | python3 -c 'import json,sys
try:
  d=json.load(sys.stdin)
  n=d if isinstance(d,list) else d.get("notes",d.get("data",[]))
  print(len(n))
except Exception: print(0)' 2>/dev/null)"
[[ "${NOTES_N:-0}" -ge 1 ]]; assert $? "build execution has notes recorded (count: ${NOTES_N:-0})"

# ---------------------------------------------------------------------------
# 8. Summary
# ---------------------------------------------------------------------------
echo; log "==================== SUMMARY ===================="
log "wall clock:      ${WALL}s"
log "status:          $STATUS   success=$SUCCESS"
log "PR:              ${PR_URL:-<none>}"
log "CI gate:         ${CI_STATUS:-<none>}"
log "run dir:         $RUN_DIR  (planner.log, cp.log, execution.json, invocations.jsonl)"
log "invocation log:  $LOG"
if [[ "$ASSERT_FAILS" -eq 0 ]]; then
  ok  "ALL ASSERTIONS PASSED"
  exit 0
else
  err "$ASSERT_FAILS assertion(s) FAILED"
  exit 1
fi
