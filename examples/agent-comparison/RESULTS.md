# Agent Comparison: Todo CLI App Build

## Prompt

> Build a Node.js CLI todo app with add, list, complete, and delete commands.
> Data should persist to a JSON file. Initialize git, write tests, and commit your work.

## Results

| Metric | SWE-AF (haiku) | Claude Code (haiku) | Claude Code (sonnet) | Codex (o3) |
|--------|----------------|---------------------|----------------------|------------|
| CLI works | PASS | PASS | PASS | PASS |
| Tests pass | PASS | PASS | PASS | PASS |
| Source files | 4 | 2 | 2 | 3 |
| Test files | 14 | 1 | 1 | 1 |
| Test organization | layered (4 tiers) | flat | flat | flat |
| .gitignore | PASS | FAIL | PARTIAL | PARTIAL |
| node_modules clean | PASS | PASS | PASS | PASS |
| Git status clean | PASS | FAIL | PASS | FAIL |
| Commit count | 16 | 1 | 1 | 1 |
| Commit quality | descriptive (16 commits) | monolithic | monolithic | monolithic |
| Error handling | PASS | PASS | PASS | PASS |
| package.json | PASS | PASS | PASS | PASS |
| README.md | FAIL | FAIL | PASS | FAIL |

## Scoring Summary

Each agent is scored across five dimensions:

| Dimension | Weight | Description |
|-----------|--------|-------------|
| Functional correctness | 30% | CLI commands work, tests pass |
| Code structure | 20% | Modular source files, test organization |
| Repo hygiene | 20% | .gitignore, clean git status, no artifacts |
| Git practices | 15% | Meaningful commit history |
| Quality signals | 15% | Error handling, package.json, README |


### Computed Scores

| Agent | Functional | Structure | Hygiene | Git | Quality | **Total** |
|-------|-----------|-----------|---------|-----|---------|-----------|
| SWE-AF (haiku) | 30/30 | 20/20 | 20/20 | 15/15 | 10/15 | **95/100** |
| Claude Code (haiku) | 30/30 | 10/20 | 7/20 | 2/15 | 10/15 | **59/100** |
| Claude Code (sonnet) | 30/30 | 10/20 | 16/20 | 2/15 | 15/15 | **73/100** |
| Codex (o3) | 30/30 | 10/20 | 10/20 | 2/15 | 10/15 | **62/100** |

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
  --full-auto
```
