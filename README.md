# SWE-AF

Autonomous software engineering team built on [AgentField](https://github.com/Agent-Field/agentfield). One API call spins up hundred and thousands of agents that decompose, parallelize, code, test, review, and self-correct. Merge-ready code, not demos.

**95/100 with haiku.** The cheapest model. SWE-AF with haiku outscores Claude Code with sonnet (73), Codex (62), and Claude Code with haiku (59) on the same task.

<details>
<summary><strong>Full benchmark details</strong></summary>

Same prompt, four agents. **SWE-AF uses only haiku** (turbo preset) across all 400+ agent instances.

> **Prompt:** Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work.

### Scoring Framework

| Dimension      | Points | What it measures                                                | Why it matters                              |
| -------------- | ------ | --------------------------------------------------------------- | ------------------------------------------- |
| **Functional** | 30     | CLI commands work + tests pass                                  | Does the code do what it claims?            |
| **Structure**  | 20     | Modular source files + layered test organization                | Can a team maintain and extend this?        |
| **Hygiene**    | 20     | .gitignore coverage + clean git status + no committed artifacts | Is the repo ready for collaborators?        |
| **Git**        | 15     | Commit count + descriptive, feature-scoped messages             | Can you review, bisect, and rollback?       |
| **Quality**    | 15     | Error handling + package.json completeness + README             | Does it meet minimum open-source standards? |

### Score Breakdown

| Dimension           | SWE-AF (haiku) | CC Sonnet | Codex  | CC Haiku |
| ------------------- | -------------- | --------- | ------ | -------- |
| **Functional** (30) | **30**         | **30**    | **30** | **30**   |
| **Structure** (20)  | **20**         | 10        | 10     | 10       |
| **Hygiene** (20)    | **20**         | 16        | 10     | 7        |
| **Git** (15)        | **15**         | 2         | 2      | 2        |
| **Quality** (15)    | 10             | **15**    | 10     | 10       |
| **Total**           | **95**         | **73**    | **62** | **59**   |

Every agent produces working code. The gap is structure, git discipline, and repo hygiene: the things that make code shippable.

### Detailed Metrics (13 checks)

| Metric             | SWE-AF          | CC Haiku   | CC Sonnet  | Codex      |
| ------------------ | --------------- | ---------- | ---------- | ---------- |
| CLI works          | PASS            | PASS       | PASS       | PASS       |
| Tests pass         | PASS            | PASS       | PASS       | PASS       |
| Source files       | 4               | 2          | 2          | 3          |
| Test files         | **14**          | 1          | 1          | 1          |
| Test organization  | **4 tiers**     | flat       | flat       | flat       |
| .gitignore         | PASS            | FAIL       | PARTIAL    | PARTIAL    |
| node_modules clean | PASS            | PASS       | PASS       | PASS       |
| Git status clean   | PASS            | FAIL       | PASS       | FAIL       |
| Commit count       | **16**          | 1          | 1          | 1          |
| Commit quality     | **descriptive** | monolithic | monolithic | monolithic |
| Error handling     | PASS            | PASS       | PASS       | PASS       |
| package.json       | PASS            | PASS       | PASS       | PASS       |
| README.md          | FAIL            | FAIL       | PASS       | FAIL       |

### Agents Tested

| Agent       | Model                    | Approach              | Time    |
| ----------- | ------------------------ | --------------------- | ------- |
| **SWE-AF**  | **haiku** (turbo preset) | SWE-AF (turbo preset) | ~43 min |
| Claude Code | sonnet                   | Single-agent CLI      | ~2 min  |
| Codex       | gpt-5.3-codex            | Single-agent CLI      | ~1 min  |
| Claude Code | haiku                    | Single-agent CLI      | ~1 min  |

### Reproduction

```bash
# SWE-AF (haiku, turbo preset)
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work.", "repo_path": "/tmp/swe-af-output", "config": {"preset": "turbo"}}}'

# Claude Code (haiku)
claude -p \
  "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." \
  --model haiku --dangerously-skip-permissions

# Claude Code (sonnet)
claude -p \
  "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." \
  --model sonnet --dangerously-skip-permissions

# Codex (gpt-5.3-codex)
codex exec \
  "Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work." \
  --full-auto
```

Full source code from all four agents, the automated evaluation script, and agent logs are in [`examples/agent-comparison/`](examples/agent-comparison/).

This benchmark used a simple prompt to make comparison fair. SWE-AF's advantage grows with task complexity: more modules to coordinate, more integration points to test, more merge conflicts to resolve.

</details>

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Add JWT auth to all API endpoints", "repo_path": "/path/to/repo"}}'
```

**What happens:**

- Architecture designed and peer-reviewed before any code is written. Review costs one API call, so every design gets one
- Issues dependency-sorted and executed in parallel across isolated worktrees. 20 agents code simultaneously. No standups, no Slack threads. Coordination is programmatic
- Every issue gets a dedicated coder, tester, and reviewer. A single agent gives you one shot. A human team can't afford this staffing. Agents make it free
- Failures self-correct up to N times per issue. When exhausted, a specialist agent diagnoses the failure and adapts: new approach, split, relaxed scope (tracked as debt), or escalation
- Context flows between agents instantly. Conventions from issue 1 appear in issue 20 before its coder starts. No onboarding, no knowledge silos
- Plans restructure at runtime when issues escalate. Dependencies recomputed, blocked work rerouted. What takes a PM a day takes agents seconds
- Every compromise tracked in a **debt register**. Nothing silently dropped

![SWE-AF Architecture](assets/archi.png)

> Every box is an independent agent instance. A typical build deploys **400-500+ agent instances** across parallel worktrees, each with full tool use, file system access, and git operations.

## From coding agent to engineering team

Claude Code, Codex, and Devin are agent harnesses: an LLM with tools, memory, and retry logic. Capable solo developers. But still one context window, one attempt, one commit.

SWE-AF orchestrates hundreds of harnesses into an engineering team. Roles (PM, architect, coder, QA, reviewer), parallel execution across isolated worktrees, three self-correction loops, and shared memory that propagates instantly.

**Self-correction at three levels.** Inner: coder + QA + reviewer loop up to 5 times per issue. A standalone harness gets one attempt. Middle: when iterations exhaust, an Issue Advisor diagnoses and adapts (new approach, split, relax criteria as debt, or escalate). Outer: a Replanner restructures the remaining plan at runtime. Dependencies recomputed, blocked work removed.

**Parallelism with zero coordination cost.** Issues dependency-sorted into levels, executed simultaneously in isolated worktrees. File conflict detection prevents collisions. Merge, integration test, next level. A 20-issue build runs in the time of its longest dependency chain.

**Shared memory, not shared meetings.** Conventions from early issues inject into later ones. Failure patterns accumulate. An interface registry tells downstream agents exactly what upstream provided. The 50th agent is smarter than the 1st.

**Transparent compromise.** Every relaxed requirement tracked in a typed, severity-rated debt register. The PR tells you exactly what shipped and what didn't.

**Crash recovery.** Checkpoints at level and iteration granularity. `resume_build` picks up where the build stopped.

## How it works

Three nested loops drive every build:

| Loop       | Scope     | Budget        | Trigger                     | Action                                                             |
| ---------- | --------- | ------------- | --------------------------- | ------------------------------------------------------------------ |
| **Inner**  | Per issue | 5 iterations  | Tests fail / review rejects | Feed errors back to coder, retry                                   |
| **Middle** | Per issue | 2 invocations | Inner loop exhausted        | Diagnose → new approach / split / relax criteria (debt) / escalate |
| **Outer**  | Per build | 2 replans     | Issues escalated            | Restructure remaining plan, recompute dependencies                 |

## Quick start

```bash
python3 -m pip install -r requirements.txt
af                 # control plane on :8080
python3 -m swe_af  # registers "swe-planner" node
```

Stateless nodes register with the [AgentField](https://github.com/Agent-Field/agentfield) control plane. Run on a laptop, container, or Lambda. Crash-safe: call `resume_build` to pick up where you left off.

<details>
<summary><strong>Docker</strong></summary>

```bash
# Set your API keys
cp .env.example .env
# Edit .env with your ANTHROPIC_API_KEY (and GH_TOKEN for draft PRs)

# Start control plane + SWE agent
docker compose up -d

# Submit a build (local repo on shared volume)
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Add JWT auth", "repo_path": "/workspaces/my-repo"}}'

# Or: clone from GitHub and get a draft PR back
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"repo_url": "https://github.com/user/my-repo", "goal": "Add JWT auth"}}'

# Scale to 3 replicas for parallel builds
docker compose up --scale swe-agent=3 -d
```

All replicas share a `/workspaces` volume for repos, worktrees, and artifacts. The control plane load-balances `app.call()` across all registered nodes.

**Using an existing control plane** (no Docker control plane):

```bash
# If you already have `af` running on localhost:8080
docker compose -f docker-compose.local.yml up -d
```

This uses `host.docker.internal` to connect from the container to your host's control plane.

</details>

<details>
<summary><strong>GitHub Workflow (Clone → Build → Draft PR)</strong></summary>

Pass `repo_url` instead of `repo_path` to clone a GitHub repo automatically. After the build completes, a draft PR is created.

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {
    "repo_url": "https://github.com/user/my-project",
    "goal": "Add comprehensive test coverage",
    "config": {"preset": "quality"}
  }}'
```

**What happens:**
1. Repo cloned to `/workspaces/my-project`
2. Full build: plan → execute → verify
3. Integration branch pushed to origin
4. Draft PR created with build summary

**Requirements:**
- `GH_TOKEN` set in `.env` (GitHub personal access token with `repo` scope)
- Remote repo must be accessible (public or token has access)

**Config options:**
| Key                | Default |                                                    |
| ------------------ | ------- | -------------------------------------------------- |
| `repo_url`         | `""`    | GitHub URL to clone                                |
| `enable_github_pr` | `true`  | Create draft PR after build                        |
| `github_pr_base`   | `""`    | PR base branch (auto-detected from remote default) |

</details>

<details>
<summary><strong>API Reference</strong></summary>

### Core endpoints

Async via the control plane. Returns `execution_id` immediately. All with `-H "Content-Type: application/json"`.

```bash
# Full build: plan → execute → verify
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {}}}'

# Plan only
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.plan \
  -d '{"input": {"goal": "...", "repo_path": "..."}}'

# Execute a pre-made plan
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.execute \
  -d '{"input": {"plan_result": { ... }, "repo_path": "..."}}'

# Resume after crash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.resume_build \
  -d '{"input": {"repo_path": "...", "artifacts_dir": ".artifacts"}}'
```

### Every agent is an endpoint

`POST /api/v1/execute/async/swe-planner.<agent>`

| Agent                    | In → Out                                            |
| ------------------------ | --------------------------------------------------- |
| `run_product_manager`    | goal → PRD                                          |
| `run_architect`          | PRD → architecture                                  |
| `run_tech_lead`          | architecture → review                               |
| `run_sprint_planner`     | architecture → parallelized issues                  |
| `run_issue_writer`       | issue spec → detailed issue file                    |
| `run_coder`              | issue + worktree → code + tests + commit            |
| `run_qa`                 | worktree → test results                             |
| `run_code_reviewer`      | worktree → quality/security review                  |
| `run_qa_synthesizer`     | QA + review → FIX / APPROVE / BLOCK                 |
| `run_issue_advisor`      | failure context → adapt / split / accept / escalate |
| `run_replanner`          | build state + failures → restructured plan          |
| `run_merger`             | branches → merged with conflict resolution          |
| `run_integration_tester` | merged repo → integration test results              |
| `run_verifier`           | repo + PRD → acceptance pass/fail                   |
| `generate_fix_issues`    | failed criteria → targeted fix issues               |
| `run_github_pr`          | integration branch → push + draft PR                |

### Monitoring

```bash
curl http://localhost:8080/api/v1/executions/<execution_id>
```

</details>

<details>
<summary><strong>Configuration</strong></summary>

Pass `config` to `build` or `execute`. All optional. Full schema: [`swe_af/execution/schemas.py`](swe_af/execution/schemas.py)

| Key                       | Default    |                              |
| ------------------------- | ---------- | ---------------------------- |
| `max_coding_iterations`   | `5`        | Inner loop budget per issue  |
| `max_advisor_invocations` | `2`        | Middle loop budget per issue |
| `max_replans`             | `2`        | Outer loop budget per build  |
| `enable_issue_advisor`    | `true`     | Enable middle loop           |
| `enable_replanning`       | `true`     | Enable outer loop            |
| `agent_timeout_seconds`   | `2700`     | Per-agent timeout            |
| `ai_provider`             | `"claude"` | `"claude"` or `"codex"`      |
| `coder_model`             | `"sonnet"` | Model for coding             |
| `agent_max_turns`         | `150`      | Tool-use turns per agent     |

### Model configuration

Use **presets** and **role groups** instead of setting 16 individual `*_model` fields.

#### Presets

| Preset         | Planning | Coding | Orchestration | Lightweight | Use case                               |
| -------------- | -------- | ------ | ------------- | ----------- | -------------------------------------- |
| `turbo`        | haiku    | haiku  | haiku         | haiku       | Fast iteration, rapid prototyping      |
| `fast`         | sonnet   | sonnet | haiku         | haiku       | Good code quality, cheap orchestration |
| **`balanced`** | sonnet   | sonnet | sonnet        | haiku       | **Default.** Production quality        |
| `thorough`     | sonnet   | sonnet | sonnet        | sonnet      | Uniform sonnet everywhere              |
| `quality`      | opus     | opus   | sonnet        | haiku       | Best planning + coding                 |

#### Role groups

| Group           | Agents                                                                                           |
| --------------- | ------------------------------------------------------------------------------------------------ |
| `planning`      | Product Manager, Architect, Tech Lead, Sprint Planner                                            |
| `coding`        | Coder, QA, Code Reviewer                                                                         |
| `orchestration` | Replanner, Retry Advisor, Issue Writer, Verifier, Git, Merger, Integration Tester, Issue Advisor |
| `lightweight`   | QA Synthesizer (FIX/APPROVE/BLOCK routing)                                                       |

#### Layered resolution

Precedence (lowest → highest): **defaults** < **preset** < **role groups** < **individual fields**

```bash
# Preset only: opus planning+coding, sonnet orchestration, haiku lightweight
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"preset": "quality"}}}'

# Group override: opus planning, everything else uses defaults
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"models": {"planning": "opus"}}}}'

# Preset + group override: quality preset but cheap orchestration
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"preset": "quality", "models": {"orchestration": "haiku"}}}}'

# Preset + individual override: balanced but architect uses opus
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"preset": "balanced", "architect_model": "opus"}}}'

# Backward compatible: individual *_model fields still work
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"pm_model": "opus", "architect_model": "opus"}}}'

# Top-level model= convenience: sets all 16 fields to the same value
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "model": "opus"}}'
```

Presets and groups are the recommended approach. Individual `*_model` fields are available for fine-tuning.

</details>

<details>
<summary><strong>Artifacts</strong></summary>

```
.artifacts/
├── plan/           # PRD, architecture, issue specs
├── execution/      # checkpoint, per-issue iterations, agent logs
└── verification/   # acceptance criteria results
```

</details>

## Requirements

- Python 3.11+
- [AgentField](https://github.com/Agent-Field/agentfield) control plane
- Anthropic or OpenAI API key

## Development

```bash
make test          # unit tests
make check         # tests + bytecode compile check
make clean         # remove generated Python/editor cache files
make clean-examples  # remove Rust build outputs in example folders
```

`examples/diagrams/` and `examples/pyrust/` are included in git so users can inspect full example outputs, including `.artifacts/logs`. `examples/agent-comparison/` contains the SWE-AF vs single-agent CLI benchmark.

## Internals

- Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- Contribution guide: [docs/CONTRIBUTING.md](docs/CONTRIBUTING.md)
- License: [Apache-2.0](LICENSE)

---

SWE-AF is a step toward autonomous software development where AI doesn't just assist a developer, it operates as an engineering team. Built on [AgentField](https://github.com/Agent-Field/agentfield).
