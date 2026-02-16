<div align="center">

# SWE-AF

### Autonomous Engineering Team Runtime Built on [AgentField](https://github.com/Agent-Field/agentfield)

**Pronounced:** _"swee-AF"_ (one word)

[![Public Beta](https://img.shields.io/badge/status-public%20beta-0ea5e9?style=for-the-badge)](#)
[![Python](https://img.shields.io/badge/python-3.12%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache%202.0-16a34a?style=for-the-badge)](LICENSE)
[![Tests](https://img.shields.io/badge/Tests-make%20check-blue?style=for-the-badge)](.github/workflows/ci.yml)
[![Built with AgentField](https://img.shields.io/badge/Built%20with-AgentField-0A66C2?style=for-the-badge)](https://github.com/Agent-Field/agentfield)
![WorldSpace Community Developer](https://img.shields.io/badge/WorldSpace-Community%20Developer-111827?style=for-the-badge)
[![Example PR](https://img.shields.io/badge/Example-PR%20%23179-ff6b35?style=for-the-badge&logo=github)](https://github.com/Agent-Field/agentfield/pull/179)



<p>
  <a href="#quick-start">Quick Start</a> •
  <a href="#why-swe-af">Why SWE-AF</a> •
  <a href="#in-action">In Action</a> •
  <a href="#adaptive-factory-control">Factory Control</a> •
  <a href="#benchmark-snapshot">Benchmark</a> •
  <a href="#api-reference">API</a> •
  <a href="docs/ARCHITECTURE.md">Architecture Doc</a>
</p>

</div>

One API call spins up a full autonomous engineering team that can scope, build, adapt, and ship complex software end to end.
SWE-AF is a first step toward **autonomous software engineering factories**, scaling from simple goals to hard multi-issue programs with hundreds to thousands of agent invocations.

<p align="center">
  <img src="assets/banner.jpg" alt="SWE-AF autonomous engineering fleet banner" width="100%" />
</p>

## Why SWE-AF

Most agent frameworks are harnesses around a single coder loop. SWE-AF is a software engineering factory built from coordinated harnesses.

- Hardness-aware execution: easy issues pass through quickly, while hard issues trigger deeper adaptation and DAG-level replanning instead of blind retries.
- Factory architecture: planning, execution, and governance agents run as a coordinated control stack.
- Continual learning (optional): with `enable_learning=true`, conventions and failure patterns discovered early are injected into downstream issues.
- Agent-scale parallelism: dependency-level scheduling + isolated git worktrees allow large fan-out without branch collisions.
- Fleet-scale orchestration with AgentField: many SWE-AF nodes can run continuously in parallel, driving thousands of agent invocations across concurrent builds.
- Explicit compromise tracking: when scope is relaxed, debt is typed, severity-rated, and propagated.
- Long-run reliability: checkpointed execution supports `resume_build` after crashes or interruptions.

## In Action

[PR #179: Go SDK DID/VC Registration](https://github.com/Agent-Field/agentfield/pull/179) — built entirely by SWE-AF (haiku, turbo preset). One API call, zero human code.

| Metric | Value |
| --- | --- |
| Issues completed | 10/10 |
| Tests passing | 217 |
| Acceptance criteria | 34/34 |
| Agent invocations | 79 |
| Model | `claude-haiku-4-5` |
| **Total cost** | **$19.23** |

<details>
<summary>Cost breakdown by agent role</summary>

| Role | Cost | % |
| --- | --- | --- |
| Coder | $5.88 | 30.6% |
| Code Reviewer | $3.48 | 18.1% |
| QA | $1.78 | 9.2% |
| GitHub PR | $1.66 | 8.6% |
| Integration Tester | $1.59 | 8.3% |
| Merger | $1.22 | 6.3% |
| Workspace Ops | $1.77 | 9.2% |
| Planning (PM + Arch + TL + Sprint) | $0.79 | 4.1% |
| Verifier + Finalize | $0.34 | 1.8% |
| Synthesizer | $0.05 | 0.2% |

79 invocations, 2,070 conversation turns. Planning agents scope and decompose; coders work in parallel isolated worktrees; reviewers and QA validate each issue; merger integrates branches; verifier checks acceptance criteria against the PRD.

</details>

## Adaptive Factory Control

SWE-AF uses three nested control loops to adapt to task difficulty in real time:

| Loop        | Scope         | Trigger              | Action                                                                             |
| ----------- | ------------- | -------------------- | ---------------------------------------------------------------------------------- |
| Inner loop  | Single issue  | QA/review fails      | Coder retries with feedback                                                        |
| Middle loop | Single issue  | Inner loop exhausted | `run_issue_advisor` retries with a new approach, splits work, or accepts with debt |
| Outer loop  | Remaining DAG | Escalated failures   | `run_replanner` restructures remaining issues and dependencies                     |

This is the core factory-control behavior: control agents supervise worker agents and continuously reshape the plan as reality changes.

## Quick Start

### 1. Requirements

- Python 3.12+
- AgentField control plane (`af`)
- One AI provider key: Anthropic or OpenAI-compatible setup

### 2. Install

```bash
python3.12 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e ".[dev]"
```

### 3. Run

```bash
af                 # starts AgentField control plane on :8080
python -m swe_af   # registers node id "swe-planner"
```

### 4. Trigger a build

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Add JWT auth to all API endpoints", "repo_path": "/path/to/repo"}}'
```

Enable continual learning for harder, longer builds:

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Refactor and harden auth + billing flows", "repo_path": "/path/to/repo", "enable_learning": true, "config": {"preset": "fast"}}}'
```

## What Happens In One Build

- Architecture is generated and reviewed before coding starts
- Issues are dependency-sorted and run in parallel across isolated worktrees
- Each issue gets dedicated coder, tester, and reviewer passes
- Failed issues trigger advisor-driven adaptation (split, re-scope, or escalate)
- Escalations trigger replanning of the remaining DAG
- End result is merged, integration-tested, and verified against acceptance criteria

<p align="center">
  <img src="assets/archi.png" alt="SWE-AF architecture" width="100%" />
</p>

> Typical runs spin up 400-500+ agent instances across planning, execution, QA, and verification. For larger DAGs and repeated adaptation/replanning cycles, SWE-AF can scale into the high hundreds to thousands of agent invocations in a single build.

## Benchmark Snapshot

**95/100 with haiku**: SWE-AF (haiku, turbo preset) outscored Claude Code sonnet (73), Codex (62), and Claude Code haiku (59) on the same prompt.

| Dimension       | SWE-AF (haiku) | CC Sonnet | Codex  | CC Haiku |
| --------------- | -------------- | --------- | ------ | -------- |
| Functional (30) | **30**         | **30**    | **30** | **30**   |
| Structure (20)  | **20**         | 10        | 10     | 10       |
| Hygiene (20)    | **20**         | 16        | 10     | 7        |
| Git (15)        | **15**         | 2         | 2      | 2        |
| Quality (15)    | 10             | **15**    | 10     | 10       |
| Total           | **95**         | **73**    | **62** | **59**   |

<details>
<summary><strong>Full benchmark details and reproduction</strong></summary>

Same prompt, four agents. SWE-AF used only haiku (turbo preset) across 400+ agent instances.

**Prompt used for all agents:**

> Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work.

### Scoring framework

| Dimension  | Points | What it measures                                 |
| ---------- | ------ | ------------------------------------------------ |
| Functional | 30     | CLI behavior and passing tests                   |
| Structure  | 20     | Modular source layout and test organization      |
| Hygiene    | 20     | `.gitignore`, clean status, no junk artifacts    |
| Git        | 15     | Commit discipline and message quality            |
| Quality    | 15     | Error handling, package metadata, README quality |

### Reproduction

```bash
# SWE-AF (haiku, turbo preset)
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work.", "repo_path": "/tmp/swe-af-output", "config": {"preset": "turbo"}}}'

# Claude Code (haiku)
claude -p "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." --model haiku --dangerously-skip-permissions

# Claude Code (sonnet)
claude -p "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." --model sonnet --dangerously-skip-permissions

# Codex (gpt-5.3-codex)
codex exec "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." --full-auto
```

Benchmark assets, logs, evaluator, and generated projects live in [`examples/agent-comparison/`](examples/agent-comparison/).

</details>

## Docker

```bash
cp .env.example .env
# Fill ANTHROPIC_API_KEY (and GH_TOKEN if using draft PR workflow)

docker compose up -d
```

Submit a build:

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Add JWT auth", "repo_path": "/workspaces/my-repo"}}'
```

Scale workers:

```bash
docker compose up --scale swe-agent=3 -d
```

Use a host control plane instead of Docker control-plane service:

```bash
docker compose -f docker-compose.local.yml up -d
```

## GitHub Repo Workflow (Clone -> Build -> Draft PR)

Pass `repo_url` instead of `repo_path` to let SWE-AF clone and open a draft PR after execution.

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {
    "repo_url": "https://github.com/user/my-project",
    "goal": "Add comprehensive test coverage",
    "config": {"preset": "quality"}
  }}'
```

Requirements:

- `GH_TOKEN` in `.env` with `repo` scope
- Repo access for that token

## API Reference

Core async endpoints (returns an `execution_id` immediately):

```bash
# Full build: plan -> execute -> verify
POST /api/v1/execute/async/swe-planner.build

# Plan only
POST /api/v1/execute/async/swe-planner.plan

# Execute a prebuilt plan
POST /api/v1/execute/async/swe-planner.execute

# Resume after interruption
POST /api/v1/execute/async/swe-planner.resume_build
```

Monitoring:

```bash
curl http://localhost:8080/api/v1/executions/<execution_id>
```

Every specialist is also callable directly:

`POST /api/v1/execute/async/swe-planner.<agent>`

| Agent                    | In -> Out                                            |
| ------------------------ | ---------------------------------------------------- |
| `run_product_manager`    | goal -> PRD                                          |
| `run_architect`          | PRD -> architecture                                  |
| `run_tech_lead`          | architecture -> review                               |
| `run_sprint_planner`     | architecture -> issue DAG                            |
| `run_issue_writer`       | issue spec -> detailed issue                         |
| `run_coder`              | issue + worktree -> code + tests + commit            |
| `run_qa`                 | worktree -> test results                             |
| `run_code_reviewer`      | worktree -> quality/security review                  |
| `run_qa_synthesizer`     | QA + review -> FIX / APPROVE / BLOCK                 |
| `run_issue_advisor`      | failure context -> adapt / split / accept / escalate |
| `run_replanner`          | build state + failures -> restructured plan          |
| `run_merger`             | branches -> merged output                            |
| `run_integration_tester` | merged repo -> integration results                   |
| `run_verifier`           | repo + PRD -> acceptance pass/fail                   |
| `generate_fix_issues`    | failed criteria -> targeted fix issues               |
| `run_github_pr`          | branch -> push + draft PR                            |

## Configuration

Pass `config` to `build` or `execute`. Full schema: [`swe_af/execution/schemas.py`](swe_af/execution/schemas.py)

| Key                       | Default    | Description                                           |
| ------------------------- | ---------- | ----------------------------------------------------- |
| `max_coding_iterations`   | `5`        | Inner-loop retry budget                               |
| `max_advisor_invocations` | `2`        | Middle-loop advisor budget                            |
| `max_replans`             | `2`        | Build-level replanning budget                         |
| `enable_issue_advisor`    | `true`     | Enable issue adaptation                               |
| `enable_replanning`       | `true`     | Enable global replanning                              |
| `enable_learning`         | `false`    | Enable cross-issue shared memory (continual learning) |
| `agent_timeout_seconds`   | `2700`     | Per-agent timeout                                     |
| `ai_provider`             | `"claude"` | `claude` or `codex`                                   |
| `coder_model`             | `"sonnet"` | Coding model                                          |
| `agent_max_turns`         | `150`      | Tool-use turn budget                                  |

### Presets

| Preset     | Planning | Coding | Orchestration | Lightweight | Use case                        |
| ---------- | -------- | ------ | ------------- | ----------- | ------------------------------- |
| `turbo`    | haiku    | haiku  | haiku         | haiku       | Fastest turnaround              |
| `fast`     | sonnet   | sonnet | haiku         | haiku       | Cost-efficient quality          |
| `balanced` | sonnet   | sonnet | sonnet        | haiku       | Default profile                 |
| `thorough` | sonnet   | sonnet | sonnet        | sonnet      | Uniform quality                 |
| `quality`  | opus     | opus   | sonnet        | haiku       | Maximum planning/coding quality |

### Resolution order

`defaults` < `preset` < `role groups` < individual `*_model` fields

## Artifacts

```text
.artifacts/
├── plan/           # PRD, architecture, issue specs
├── execution/      # checkpoints, per-issue logs, agent outputs
└── verification/   # acceptance criteria results
```

## Development

```bash
make test
make check
make clean
make clean-examples
```

## Security and Community

- Contribution guide: [`docs/CONTRIBUTING.md`](docs/CONTRIBUTING.md)
- Code of conduct: [`CODE_OF_CONDUCT.md`](CODE_OF_CONDUCT.md)
- Security policy: [`SECURITY.md`](SECURITY.md)
- Changelog: [`CHANGELOG.md`](CHANGELOG.md)
- License: [`Apache-2.0`](LICENSE)

---

SWE-AF is built on [AgentField](https://github.com/Agent-Field/agentfield) as a first step from single-agent harnesses to autonomous software engineering factories.
