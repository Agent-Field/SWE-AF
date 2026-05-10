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
| `runtime` | `"claude_code"`, `"open_code"`, `"codex"` | AI backend to use |
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

## Multi-Repo Builds

SWE-AF supports coordinated work across multiple repositories in a single build. Pass `config.repos` as an array of repository objects, each with a `repo_url` (or `repo_path`) and a `role`. Single-repo builds remain backward compatible—just use `repo_url` or `repo_path` at the top level.

### Complete Example: Primary App + Dependency

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{
    "input": {
      "goal": "Add JWT auth across API and shared-lib",
      "config": {
        "repos": [
          {
            "repo_url": "https://github.com/org/main-app",
            "role": "primary"
          },
          {
            "repo_url": "https://github.com/org/shared-lib",
            "role": "dependency"
          }
        ],
        "runtime": "claude_code",
        "models": {
          "default": "sonnet"
        }
      }
    }
  }'
```

**Repository roles:**
- `primary`: The main application being built. Changes here drive the build; failures block progress.
- `dependency`: Libraries or services that may be modified to support the primary repo. Failures are captured but don't block primary build progress.

### Use Cases

- **Primary App + Shared Libraries**: Coordinate changes between a web application and its shared utilities/SDK.
- **Monorepo Sub-Projects**: Define multiple repos in a monorepo structure and orchestrate cross-package changes.
- **Microservices**: When a feature spans an API service and a worker service, define roles to manage interdependencies.

## Requirements for open_code Runtime

1. `opencode` CLI installed and in PATH
2. Model provider credentials configured in OpenCode (e.g., `OPENAI_API_KEY` for z.ai)
3. Model ID format matches what OpenCode expects

## Requirements for codex Runtime

1. Codex CLI installed and in PATH.
2. For ChatGPT subscription auth: run `codex login` on the host, set `SWE_CODEX_AUTH_MODE=chatgpt` or `auto`, and leave `OPENAI_API_KEY` unset for the agent process.
3. For OpenAI API-platform billing: set `SWE_CODEX_AUTH_MODE=api_key` and `OPENAI_API_KEY`.

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
