#!/usr/bin/env bash
# evaluate.sh — Automated comparison of agent-built todo apps
# Runs against each subdirectory and produces RESULTS.md
#
# Usage: bash evaluate.sh [base_dir]
#   base_dir defaults to the directory containing this script

set -eo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BASE_DIR="${1:-$SCRIPT_DIR}"
RESULTS_FILE="$BASE_DIR/RESULTS.md"

AGENTS=("swe-af" "claude-code-haiku" "claude-code-sonnet" "codex")
LABELS=("SWE-AF (haiku)" "Claude Code (haiku)" "Claude Code (sonnet)" "Codex (o3)")

# ─── Per-agent evaluation ──────────────────────────────────────────
# Writes results to individual temp files to avoid stdout pollution
evaluate() {
  local dir="$1"
  local outfile="$2"

  if [ ! -d "$dir" ] || [ -z "$(ls -A "$dir" 2>/dev/null)" ]; then
    echo "EMPTY|EMPTY|0|0|flat|FAIL|PASS|N/A|0|none|FAIL|FAIL|FAIL" > "$outfile"
    return
  fi

  # --- Find the CLI entry point ---
  local cli_file=""
  for candidate in cli.js index.js todo.js app.js main.js src/cli.js src/index.js bin/todo.js; do
    if [ -f "$dir/$candidate" ]; then
      cli_file="$candidate"
      break
    fi
  done

  # If no standard name found, check package.json bin/main field
  if [ -z "$cli_file" ] && [ -f "$dir/package.json" ]; then
    local pkg_main
    pkg_main=$(node -e "
      const p = require('$dir/package.json');
      const bin = p.bin;
      if (typeof bin === 'string') { console.log(bin); }
      else if (typeof bin === 'object') { console.log(Object.values(bin)[0] || ''); }
      else { console.log(p.main || ''); }
    " 2>/dev/null || echo "")
    if [ -n "$pkg_main" ] && [ -f "$dir/$pkg_main" ]; then
      cli_file="$pkg_main"
    fi
  fi

  # --- 1. Functional: CLI works ---
  local cli_works="FAIL"
  if [ -n "$cli_file" ]; then
    local add_ok=0 list_ok=0 complete_ok=0 delete_ok=0

    # Run CLI from within project directory (some apps use CWD for file paths)
    # Try add (suppress all output)
    if (builtin cd "$dir" && node "$cli_file" add "eval test task" >/dev/null 2>&1); then
      add_ok=1
    fi

    # Try list
    if (builtin cd "$dir" && node "$cli_file" list >/dev/null 2>&1); then
      list_ok=1
    fi

    # Try complete (id 1)
    if (builtin cd "$dir" && node "$cli_file" complete 1 >/dev/null 2>&1); then
      complete_ok=1
    fi

    # Try delete (id 1)
    if (builtin cd "$dir" && node "$cli_file" delete 1 >/dev/null 2>&1); then
      delete_ok=1
    fi

    if [ $add_ok -eq 1 ] && [ $list_ok -eq 1 ] && [ $complete_ok -eq 1 ] && [ $delete_ok -eq 1 ]; then
      cli_works="PASS"
    elif [ $add_ok -eq 1 ] || [ $list_ok -eq 1 ]; then
      cli_works="PARTIAL"
    fi

    # Clean up any test data created
    rm -f "$dir/todos.json" "$dir/data.json" "$dir/todo.json" "$dir/tasks.json" "$dir/.todos.json"
  fi

  # --- 2. Functional: Tests pass ---
  local tests_pass="FAIL"
  local test_file_count
  # Use find with proper grouping to exclude node_modules and .git
  test_file_count=$(find "$dir" \( -path '*/node_modules' -o -path '*/.git' \) -prune -o \( -name '*.test.js' -o -name '*.spec.js' \) -print 2>/dev/null | wc -l | tr -d ' ')

  if [ "$test_file_count" -gt 0 ]; then
    # Install deps if package.json exists and node_modules doesn't
    if [ -f "$dir/package.json" ] && [ ! -d "$dir/node_modules" ]; then
      (builtin cd "$dir" && npm install --silent 2>/dev/null) || true
    fi

    # Prefer npm test if package.json has a test script, else use node --test
    local test_ran=0
    if [ -f "$dir/package.json" ]; then
      local test_script
      test_script=$(node -e "const p=require('$dir/package.json'); console.log(p.scripts && p.scripts.test ? p.scripts.test : '')" 2>/dev/null || echo "")
      if [ -n "$test_script" ] && [ "$test_script" != "echo \"Error: no test specified\" && exit 1" ]; then
        if (builtin cd "$dir" && npm test >/dev/null 2>&1); then
          tests_pass="PASS"
        else
          tests_pass="FAIL"
        fi
        test_ran=1
      fi
    fi

    if [ "$test_ran" -eq 0 ]; then
      # Run from within the project directory so relative paths in tests work
      local test_files_rel
      test_files_rel=$(find "$dir" \( -path '*/node_modules' -o -path '*/.git' \) -prune -o \( -name '*.test.js' -o -name '*.spec.js' \) -print 2>/dev/null | while read -r f; do echo "${f#$dir/}"; done)

      if (builtin cd "$dir" && node --test $test_files_rel >/dev/null 2>&1); then
        tests_pass="PASS"
      else
        tests_pass="FAIL"
      fi
    fi

    # Clean up any test data
    rm -f "$dir/todos.json" "$dir/data.json" "$dir/todo.json" "$dir/tasks.json" "$dir/.todos.json"
  else
    tests_pass="NO_TESTS"
  fi

  # --- 3. Structure: Source files ---
  local src_files
  src_files=$(find "$dir" \( -path '*/node_modules' -o -path '*/.git' -o -path '*/test*' \) -prune -o -name '*.js' -not -name '*.test.js' -not -name '*.spec.js' -print 2>/dev/null | wc -l | tr -d ' ')

  # --- 5. Structure: Test organization ---
  local test_org="flat"
  local has_unit=0 has_integration=0 has_acceptance=0 has_smoke=0
  { [ -d "$dir/tests/unit" ] || [ -d "$dir/test/unit" ]; } && has_unit=1 || true
  { [ -d "$dir/tests/integration" ] || [ -d "$dir/test/integration" ]; } && has_integration=1 || true
  { [ -d "$dir/tests/acceptance" ] || [ -d "$dir/test/acceptance" ]; } && has_acceptance=1 || true
  { [ -d "$dir/tests/smoke" ] || [ -d "$dir/test/smoke" ]; } && has_smoke=1 || true

  local org_count=$((has_unit + has_integration + has_acceptance + has_smoke))
  if [ $org_count -ge 3 ]; then
    test_org="layered (${org_count} tiers)"
  elif [ $org_count -ge 1 ]; then
    test_org="partial (${org_count} tiers)"
  fi

  # --- 6. Hygiene: .gitignore ---
  local has_gitignore="FAIL"
  if [ -f "$dir/.gitignore" ]; then
    local covers_nm=0 covers_env=0
    grep -q 'node_modules' "$dir/.gitignore" 2>/dev/null && covers_nm=1 || true
    grep -q '\.env' "$dir/.gitignore" 2>/dev/null && covers_env=1 || true
    if [ $covers_nm -eq 1 ] && [ $covers_env -eq 1 ]; then
      has_gitignore="PASS"
    elif [ $covers_nm -eq 1 ]; then
      has_gitignore="PARTIAL"
    fi
  fi

  # --- 7. Hygiene: node_modules committed ---
  local nm_clean="PASS"
  if git -C "$dir" ls-files --cached node_modules 2>/dev/null | grep -q . 2>/dev/null; then
    nm_clean="FAIL"
  fi

  # --- 8. Hygiene: Git status clean ---
  local git_clean="N/A"
  if [ -d "$dir/.git" ]; then
    local porcelain
    porcelain=$(git -C "$dir" status --porcelain 2>/dev/null || echo "error")
    if [ -z "$porcelain" ]; then
      git_clean="PASS"
    else
      git_clean="FAIL"
    fi
  fi

  # --- 9. Git: Commit count ---
  local commit_count="0"
  if [ -d "$dir/.git" ]; then
    commit_count=$(git -C "$dir" log --oneline 2>/dev/null | wc -l | tr -d ' ')
  fi

  # --- 10. Git: Commit quality ---
  local commit_quality="none"
  if [ -d "$dir/.git" ] && [ "$commit_count" -gt 0 ]; then
    if [ "$commit_count" -eq 1 ]; then
      commit_quality="monolithic"
    elif [ "$commit_count" -le 3 ]; then
      commit_quality="few commits"
    else
      local descriptive
      descriptive=$(git -C "$dir" log --oneline 2>/dev/null | grep -cE '(feat|fix|test|chore|refactor|add|implement|create|update):?\s' || echo 0)
      if [ "$descriptive" -ge 3 ]; then
        commit_quality="descriptive (${commit_count} commits)"
      else
        commit_quality="basic (${commit_count} commits)"
      fi
    fi
  fi

  # --- 11. Quality: Error handling ---
  local error_handling="FAIL"
  local try_catch_count=0 error_msg_count=0
  try_catch_count=$(grep -rl 'try' "$dir" --include='*.js' 2>/dev/null | grep -v node_modules | grep -v '.git' | wc -l | tr -d ' ') || true
  error_msg_count=$(grep -rl 'console\.error\|process\.exit\|throw new Error' "$dir" --include='*.js' 2>/dev/null | grep -v node_modules | grep -v '.git' | wc -l | tr -d ' ') || true

  if [ "$try_catch_count" -gt 0 ] && [ "$error_msg_count" -gt 0 ]; then
    error_handling="PASS"
  elif [ "$try_catch_count" -gt 0 ] || [ "$error_msg_count" -gt 0 ]; then
    error_handling="PARTIAL"
  fi

  # --- 12. Quality: package.json ---
  local has_package="FAIL"
  if [ -f "$dir/package.json" ]; then
    local has_name has_desc
    has_name=$(node -e "const p=require('$dir/package.json'); console.log(p.name ? 1 : 0)" 2>/dev/null || echo 0)
    has_desc=$(node -e "const p=require('$dir/package.json'); console.log(p.description ? 1 : 0)" 2>/dev/null || echo 0)
    if [ "$has_name" = "1" ] && [ "$has_desc" = "1" ]; then
      has_package="PASS"
    elif [ "$has_name" = "1" ]; then
      has_package="PARTIAL"
    fi
  fi

  # --- 13. Quality: README ---
  local has_readme="FAIL"
  { [ -f "$dir/README.md" ] || [ -f "$dir/readme.md" ]; } && has_readme="PASS" || true

  # --- Output all metrics to file ---
  echo "${cli_works}|${tests_pass}|${src_files}|${test_file_count}|${test_org}|${has_gitignore}|${nm_clean}|${git_clean}|${commit_count}|${commit_quality}|${error_handling}|${has_package}|${has_readme}" > "$outfile"
}

# ─── Main ───────────────────────────────────────────────────────────

echo "Evaluating agent outputs..."
echo ""

declare -a RESULTS

for i in "${!AGENTS[@]}"; do
  agent="${AGENTS[$i]}"
  label="${LABELS[$i]}"
  dir="$BASE_DIR/$agent"
  tmpfile=$(mktemp)
  echo "  Evaluating: $label ($dir)"
  evaluate "$dir" "$tmpfile"
  result=$(cat "$tmpfile")
  rm -f "$tmpfile"
  RESULTS+=("$result")
  echo "    Result: $result"
done

echo ""
echo "Generating RESULTS.md..."

# ─── Generate Markdown ─────────────────────────────────────────────

cat > "$RESULTS_FILE" << 'HEADER'
# Agent Comparison: Todo CLI App Build

## Prompt

> Build a Node.js CLI todo app with add, list, complete, and delete commands.
> Data should persist to a JSON file. Initialize git, write tests, and commit your work.

## Results

HEADER

# Table header
{
  echo "| Metric | SWE-AF (haiku) | Claude Code (haiku) | Claude Code (sonnet) | Codex (o3) |"
  echo "|--------|-------------------|---------------------|----------------------|------------|"
} >> "$RESULTS_FILE"

# Parse results and build rows
METRICS=(
  "CLI works"
  "Tests pass"
  "Source files"
  "Test files"
  "Test organization"
  ".gitignore"
  "node_modules clean"
  "Git status clean"
  "Commit count"
  "Commit quality"
  "Error handling"
  "package.json"
  "README.md"
)

for m in "${!METRICS[@]}"; do
  metric="${METRICS[$m]}"
  row="| $metric"
  for r in "${RESULTS[@]}"; do
    val=$(echo "$r" | cut -d'|' -f$((m+1)))
    row="$row | $val"
  done
  row="$row |"
  echo "$row" >> "$RESULTS_FILE"
done

# Add scoring summary
cat >> "$RESULTS_FILE" << 'SCORING'

## Scoring Summary

Each agent is scored across five dimensions:

| Dimension | Weight | Description |
|-----------|--------|-------------|
| Functional correctness | 30% | CLI commands work, tests pass |
| Code structure | 20% | Modular source files, test organization |
| Repo hygiene | 20% | .gitignore, clean git status, no artifacts |
| Git practices | 15% | Meaningful commit history |
| Quality signals | 15% | Error handling, package.json, README |

SCORING

# Calculate and append scores
{
  echo ""
  echo "### Computed Scores"
  echo ""
  echo "| Agent | Functional | Structure | Hygiene | Git | Quality | **Total** |"
  echo "|-------|-----------|-----------|---------|-----|---------|-----------|"
} >> "$RESULTS_FILE"

for i in "${!AGENTS[@]}"; do
  label="${LABELS[$i]}"
  r="${RESULTS[$i]}"

  if [ "$r" = "EMPTY" ]; then
    echo "| $label | 0 | 0 | 0 | 0 | 0 | **0/100** |" >> "$RESULTS_FILE"
    continue
  fi

  # Parse into array
  IFS='|' read -ra V <<< "$r"

  # Ensure we have enough elements
  while [ ${#V[@]} -lt 13 ]; do
    V+=("FAIL")
  done

  # Functional (30 pts): CLI works (15) + Tests pass (15)
  func=0
  case "${V[0]}" in PASS) func=$((func+15));; PARTIAL) func=$((func+7));; esac
  case "${V[1]}" in PASS) func=$((func+15));; esac

  # Structure (20 pts): src files > 1 (5), test files > 0 (5), test org (10)
  struct=0
  [ "${V[2]:-0}" -gt 1 ] 2>/dev/null && struct=$((struct+5)) || true
  [ "${V[3]:-0}" -gt 0 ] 2>/dev/null && struct=$((struct+5)) || true
  case "${V[4]:-flat}" in
    layered*) struct=$((struct+10));;
    partial*) struct=$((struct+5));;
  esac

  # Hygiene (20 pts): gitignore (7), nm clean (7), git clean (6)
  hygiene=0
  case "${V[5]:-FAIL}" in PASS) hygiene=$((hygiene+7));; PARTIAL) hygiene=$((hygiene+3));; esac
  case "${V[6]:-FAIL}" in PASS) hygiene=$((hygiene+7));; esac
  case "${V[7]:-FAIL}" in PASS) hygiene=$((hygiene+6));; esac

  # Git (15 pts): commits > 3 (7), descriptive (8)
  gitsc=0
  commits="${V[8]:-0}"
  if [ "$commits" -gt 5 ] 2>/dev/null; then gitsc=$((gitsc+7))
  elif [ "$commits" -gt 2 ] 2>/dev/null; then gitsc=$((gitsc+4))
  elif [ "$commits" -gt 0 ] 2>/dev/null; then gitsc=$((gitsc+2))
  fi
  case "${V[9]:-none}" in descriptive*) gitsc=$((gitsc+8));; basic*) gitsc=$((gitsc+4));; "few commits") gitsc=$((gitsc+2));; esac

  # Quality (15 pts): error handling (5), package.json (5), readme (5)
  qual=0
  case "${V[10]:-FAIL}" in PASS) qual=$((qual+5));; PARTIAL) qual=$((qual+2));; esac
  case "${V[11]:-FAIL}" in PASS) qual=$((qual+5));; PARTIAL) qual=$((qual+2));; esac
  case "${V[12]:-FAIL}" in PASS) qual=$((qual+5));; esac

  total=$((func + struct + hygiene + gitsc + qual))
  echo "| $label | $func/30 | $struct/20 | $hygiene/20 | $gitsc/15 | $qual/15 | **$total/100** |" >> "$RESULTS_FILE"
done

# Add reproduction commands
cat >> "$RESULTS_FILE" << 'REPRO'

## Reproduction Commands

### SWE-AF (multi-agent pipeline, haiku via turbo preset)

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work.", "repo_path": "/tmp/swe-af-output", "config": {"preset": "turbo"}}}'
```

### Claude Code (haiku)

```bash
claude -p \
  "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." \
  --model haiku \
  --dangerously-skip-permissions
```

### Claude Code (sonnet)

```bash
claude -p \
  "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." \
  --model sonnet \
  --dangerously-skip-permissions
```

### Codex (o3)

```bash
codex exec \
  "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." \
  -C /tmp/agent-comparison/codex \
  --full-auto
```

REPRO

echo ""
echo "Results written to $RESULTS_FILE"
echo ""
cat "$RESULTS_FILE"
