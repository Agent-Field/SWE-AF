---
name: swe-af
description: Autonomous engineering team runtime — one API call spins up coordinated AI agents to scope, build, and ship software.
license: MIT
compatibility: opencode
---

# SWE-AF Usage Guide

> Autonomous engineering team runtime — one API call spins up coordinated AI agents to scope, build, and ship software.

## What It Does

SWE-AF creates a coordinated team of AI agents (planning, coding, review, QA, merge, verification) that execute in parallel based on DAG dependencies. Issues with no dependencies run simultaneously; dependent issues run sequentially.

## Installation

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

## Running

**Terminal 1 — Control Plane:**
```bash
af                  # starts AgentField on port 8080
```

**Terminal 2 — Register Node:**
```bash
python -m swe_af    # registers the swe-planner node
```

## Triggering a Build

**With local repo:**
```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "goal": "Add JWT auth to all API endpoints",
      "repo_path": "/path/to/your/repo",
      "config": {
        "runtime": "open_code",
        "models": {
          "default": "zai-coding-plan/glm-5"
        }
      }
    }
  }'
```

**With GitHub repo (clones + creates draft PR):**
```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "goal": "Add comprehensive test coverage",
      "repo_url": "https://github.com/user/my-project",
      "config": {
        "runtime": "open_code",
        "models": {
          "default": "zai-coding-plan/glm-5"
        }
      }
    }
  }'
```

## Configuration

| Key | Values | Description |
|-----|--------|-------------|
| `runtime` | `"claude_code"`, `"open_code"` | AI backend to use |
| `models.default` | model ID string | Default model for all agents |
| `models.coder` | model ID string | Override for coder role |
| `models.qa` | model ID string | Override for QA role |
| `repo_path` | local path | Local workspace (new or existing) |
| `repo_url` | GitHub URL | Clone + draft PR workflow |

### Role-Specific Model Overrides

```json
{
  "config": {
    "runtime": "open_code",
    "models": {
      "default": "zai-coding-plan/glm-5",
      "coder": "zai-coding-plan/glm-5",
      "qa": "zai-coding-plan/glm-5",
      "verifier": "zai-coding-plan/glm-5"
    }
  }
}
```

Available roles: `pm`, `architect`, `tech_lead`, `sprint_planner`, `coder`, `qa`, `code_reviewer`, `qa_synthesizer`, `replan`, `retry_advisor`, `issue_writer`, `issue_advisor`, `verifier`, `git`, `merger`, `integration_tester`

## Requirements for open_code Runtime

1. `opencode` CLI installed and in PATH
2. Model provider credentials configured in OpenCode (e.g., `OPENAI_API_KEY` for z.ai)
3. Model ID format matches what OpenCode expects

## Monitoring

```bash
# Check build status
curl http://localhost:8080/api/v1/executions/<execution_id>
```

Artifacts are saved to:
```
.artifacts/
├── plan/           # PRD, architecture, issue specs
├── execution/      # checkpoints, per-issue logs
└── verification/   # acceptance criteria results
```

## What Happens in a Build

1. **Planning** — PM → Architect → Tech Lead → Sprint Planner (generates issue DAG)
2. **Issue Writing** — All issues written in parallel
3. **Execution** — Issues run level-by-level (parallel within levels)
   - Each issue: Coder → QA + Reviewer (parallel) → Synthesizer
   - Failures trigger advisor (retry/split/accept with debt/escalate)
4. **Merge** — Branches merged to integration branch
5. **Integration Test** — Full suite on merged code
6. **Verification** — Acceptance criteria checked against PRD

## Key Endpoints

```bash
POST /api/v1/execute/async/swe-planner.build     # Full build
POST /api/v1/execute/async/swe-planner.plan      # Plan only
POST /api/v1/execute/async/swe-planner.execute   # Execute existing plan
POST /api/v1/execute/async/swe-planner.resume_build  # Resume after crash
```
