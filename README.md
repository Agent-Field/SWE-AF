# SWE-AF

Production-grade software engineering, not vibe coding. One API call deploys hundreds of autonomous coding agents that architect, code, test, review, and verify in parallel — and delivers tested, reviewed, integration-verified code with a debt register for anything compromised.

```bash
curl -X POST http://localhost:8080/api/v1/execute/async/swe-planner.build \
  -H "Content-Type: application/json" \
  -d '{"input": {"goal": "Add JWT auth to all API endpoints", "repo_path": "/path/to/repo"}}'
```

**What happens:**

- Architecture designed and peer-reviewed before any code is written
- Issues decomposed, dependency-ordered, and executed in parallel across isolated worktrees
- Every issue coded, tested, and reviewed independently — failures loop back up to 5 times
- Integration tested after each merge tier, verified against original acceptance criteria
- Anything relaxed, skipped, or compromised is tracked in a **debt register**

![SWE-AF Architecture](assets/archi.png)

> Every box is an independent agent instance with full tool use, file system access, and git operations. A typical build deploys **400-500+ agent instances** across parallel worktrees. Tested up to 10,000.

## How it works

Three nested self-correction loops:

**Inner** — Coder → QA → Reviewer → Synthesizer. Tests fail? Feed errors back. Loops up to 5×.
**Middle** — Issue Advisor. Loop exhausted? Change approach, split, relax requirements (recorded as debt), or escalate.
**Outer** — Replanner. Issue stuck? Rewrite remaining issues, reduce scope, route around the failure. The plan reshapes itself at runtime.

## Quick start

```bash
python3 -m pip install -r requirements.txt
af                 # control plane on :8080
python3 main.py    # registers "swe-planner" node
```

Stateless nodes register with the [AgentField](https://agentfield.ai) control plane. Run on a laptop, container, or Lambda. Scale by adding nodes. Crash-safe — call `resume_build` to pick up where you left off.

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
2. Full pipeline: plan → execute → verify
3. Integration branch pushed to origin
4. Draft PR created with build summary

**Requirements:**
- `GH_TOKEN` set in `.env` (GitHub personal access token with `repo` scope)
- Remote repo must be accessible (public or token has access)

**Config options:**
| Key | Default | |
|-----|---------|---|
| `repo_url` | `""` | GitHub URL to clone |
| `enable_github_pr` | `true` | Create draft PR after build |
| `github_pr_base` | `""` | PR base branch (auto-detected from remote default) |

</details>

<details>
<summary><strong>API Reference</strong></summary>

### Core endpoints

Async via the control plane. Returns `execution_id` immediately. All with `-H "Content-Type: application/json"`.

```bash
# Full pipeline: plan → execute → verify
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

| Agent | In → Out |
|-------|----------|
| `run_product_manager` | goal → PRD |
| `run_architect` | PRD → architecture |
| `run_tech_lead` | architecture → review |
| `run_sprint_planner` | architecture → parallelized issues |
| `run_issue_writer` | issue spec → detailed issue file |
| `run_coder` | issue + worktree → code + tests + commit |
| `run_qa` | worktree → test results |
| `run_code_reviewer` | worktree → quality/security review |
| `run_qa_synthesizer` | QA + review → FIX / APPROVE / BLOCK |
| `run_issue_advisor` | failure context → adapt / split / accept / escalate |
| `run_replanner` | DAG state + failures → restructured plan |
| `run_merger` | branches → merged with conflict resolution |
| `run_integration_tester` | merged repo → integration test results |
| `run_verifier` | repo + PRD → acceptance pass/fail |
| `generate_fix_issues` | failed criteria → targeted fix issues |
| `run_github_pr` | integration branch → push + draft PR |

### Monitoring

```bash
curl http://localhost:8080/api/v1/executions/<execution_id>
```

</details>

<details>
<summary><strong>Configuration</strong></summary>

Pass `config` to `build` or `execute`. All optional. Full schema: [`execution/schemas.py`](execution/schemas.py)

| Key | Default | |
|-----|---------|---|
| `max_coding_iterations` | `5` | Inner loop budget per issue |
| `max_advisor_invocations` | `2` | Middle loop budget per issue |
| `max_replans` | `2` | Outer loop budget per build |
| `enable_issue_advisor` | `true` | Enable middle loop |
| `enable_replanning` | `true` | Enable outer loop |
| `agent_timeout_seconds` | `2700` | Per-agent timeout |
| `ai_provider` | `"claude"` | `"claude"` or `"codex"` |
| `coder_model` | `"sonnet"` | Model for coding |
| `agent_max_turns` | `150` | Tool-use turns per agent |

### Model configuration

Use **presets** and **role groups** instead of setting 16 individual `*_model` fields.

#### Presets

| Preset | Planning | Coding | Orchestration | Lightweight | Use case |
|--------|----------|--------|---------------|-------------|----------|
| `turbo` | haiku | haiku | haiku | haiku | Pipeline testing, rapid prototyping |
| `fast` | sonnet | sonnet | haiku | haiku | Good code quality, cheap orchestration |
| **`balanced`** | sonnet | sonnet | sonnet | haiku | **Default.** Production quality |
| `thorough` | sonnet | sonnet | sonnet | sonnet | Uniform sonnet everywhere |
| `quality` | opus | opus | sonnet | haiku | Best planning + coding |

#### Role groups

| Group | Agents |
|-------|--------|
| `planning` | Product Manager, Architect, Tech Lead, Sprint Planner |
| `coding` | Coder, QA, Code Reviewer |
| `orchestration` | Replanner, Retry Advisor, Issue Writer, Verifier, Git, Merger, Integration Tester, Issue Advisor |
| `lightweight` | QA Synthesizer (FIX/APPROVE/BLOCK routing) |

#### Layered resolution

Precedence (lowest → highest): **defaults** < **preset** < **role groups** < **individual fields**

```bash
# Preset only — opus planning+coding, sonnet orchestration, haiku lightweight
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"preset": "quality"}}}'

# Group override — opus planning, everything else uses defaults
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"models": {"planning": "opus"}}}}'

# Preset + group override — quality preset but cheap orchestration
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"preset": "quality", "models": {"orchestration": "haiku"}}}}'

# Preset + individual override — balanced but architect uses opus
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"preset": "balanced", "architect_model": "opus"}}}'

# Backward compatible — individual *_model fields still work
curl -X POST .../swe-planner.build \
  -d '{"input": {"goal": "...", "repo_path": "...", "config": {"pm_model": "opus", "architect_model": "opus"}}}'

# Top-level model= convenience — sets all 16 fields to the same value
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
- [AgentField](https://agentfield.ai) control plane
- Anthropic or OpenAI API key

## Development

```bash
make test          # unit tests
make check         # tests + bytecode compile check
make clean         # remove generated Python/editor cache files
make clean-examples  # remove Rust build outputs in example folders
```

`examples/diagrams/` and `examples/pyrust/` are included in git so users can inspect full example outputs, including `.artifacts/logs`. `examples/agent-comparison/` contains the SWE-AF vs single-agent CLI benchmark.

## Benchmark: Pipeline vs Single-Agent CLI

Same prompt. Four agents. One builds production-grade code, three build demos. **SWE-AF uses only haiku** — the cheapest, fastest model — across all 400+ agent instances via the `turbo` preset. It still outscores single-agent CLIs running stronger models.

> **Prompt:** Build a Node.js CLI todo app with add, list, complete, and delete commands. Data should persist to a JSON file. Initialize git, write tests, and commit your work.

### Scoring Framework

We evaluate across **5 dimensions** that separate production code from prototypes. Each dimension targets a specific quality a code reviewer would check before merging to main.

| Dimension | Points | What it measures | Why it matters |
|-----------|--------|------------------|----------------|
| **Functional** | 30 | CLI commands work + tests pass | Does the code do what it claims? |
| **Structure** | 20 | Modular source files + layered test organization | Can a team maintain and extend this? |
| **Hygiene** | 20 | .gitignore coverage + clean git status + no committed artifacts | Is the repo ready for collaborators? |
| **Git** | 15 | Commit count + descriptive, feature-scoped messages | Can you review, bisect, and rollback? |
| **Quality** | 15 | Error handling + package.json completeness + README | Does it meet minimum open-source standards? |

### Results

```
  SWE-AF (haiku)        95/100  ██████████████████████████████████████░░
  Claude Code (sonnet)  73/100  █████████████████████████████░░░░░░░░░░░
  Codex (o3)            62/100  ████████████████████████░░░░░░░░░░░░░░░░
  Claude Code (haiku)   59/100  ███████████████████████░░░░░░░░░░░░░░░░░
```

### Score Breakdown by Dimension

| Dimension | SWE-AF (haiku) | CC Sonnet | Codex | CC Haiku |
|-----------|----------------|-----------|-------|----------|
| **Functional** (30) | **30** | **30** | **30** | **30** |
| **Structure** (20) | **20** | 10 | 10 | 10 |
| **Hygiene** (20) | **20** | 16 | 10 | 7 |
| **Git** (15) | **15** | 2 | 2 | 2 |
| **Quality** (15) | 10 | **15** | 10 | 10 |
| **Total** | **95** | **73** | **62** | **59** |

Every agent scores 30/30 on Functional — they all produce working code that passes its own tests. **The gap is everything else.** SWE-AF with haiku beats Claude Code with sonnet by 22 points because pipeline architecture matters more than model capability for production quality.

### Detailed Metrics

<details>
<summary><strong>Full metric table (13 checks)</strong></summary>

| Metric | SWE-AF | CC Haiku | CC Sonnet | Codex |
|--------|--------|----------|-----------|-------|
| CLI works | PASS | PASS | PASS | PASS |
| Tests pass | PASS | PASS | PASS | PASS |
| Source files | 4 | 2 | 2 | 3 |
| Test files | **14** | 1 | 1 | 1 |
| Test organization | **4 tiers** | flat | flat | flat |
| .gitignore | PASS | FAIL | PARTIAL | PARTIAL |
| node_modules clean | PASS | PASS | PASS | PASS |
| Git status clean | PASS | FAIL | PASS | FAIL |
| Commit count | **16** | 1 | 1 | 1 |
| Commit quality | **descriptive** | monolithic | monolithic | monolithic |
| Error handling | PASS | PASS | PASS | PASS |
| package.json | PASS | PASS | PASS | PASS |
| README.md | FAIL | FAIL | PASS | FAIL |

</details>

### Where the Pipeline Wins

**Structure (+10 pts over every competitor).** SWE-AF decomposes the app into 4 source modules (`store.js`, `utils.js`, `commands.js`, `cli.js`) and 14 test files across 4 tiers: unit, integration, acceptance, and smoke. Single-agent CLIs produce 1-2 source files and 1 flat test file. More modules = easier to change one thing without breaking another. More test tiers = different categories of bugs caught.

**Git (+13 pts over every competitor).** SWE-AF creates 16 descriptive, feature-scoped commits with merge boundaries — one per issue in the plan. Single-agent CLIs dump everything into 1 monolithic commit. Feature-scoped commits make code review possible, `git bisect` useful, and rollbacks safe.

**Hygiene (+4-13 pts).** SWE-AF produces a complete `.gitignore` (node_modules, .env, OS files), clean `git status`, and zero committed artifacts. Single-agent CLIs have partial or missing .gitignore and often leave dirty working trees.

### Where Single Agents Win

**Speed.** Single-agent CLIs finish in 1-3 minutes. SWE-AF took ~43 minutes with the `turbo` preset. The pipeline pays for production quality with time.

**README.** Claude Code (sonnet) was the only agent to generate a README. SWE-AF's pipeline doesn't include a README generation step (yet).

### What This Means

Every agent can write code that works. The question is whether the output is *shippable* — reviewable git history, layered tests, clean repo state, modular structure. That's where a multi-agent pipeline with dedicated planning, coding, QA, review, and verification stages produces fundamentally different output than a single agent doing everything in one pass. And it does this with **haiku only** — the cheapest model available.

### Agents Tested

| Agent | Model | Approach | Time |
|-------|-------|----------|------|
| **SWE-AF** | **haiku** (turbo preset) | Multi-agent pipeline, 400+ agent instances | ~43 min |
| Claude Code | sonnet | Single-agent CLI | ~2 min |
| Codex | gpt-5.3-codex | Single-agent CLI | ~1 min |
| Claude Code | haiku | Single-agent CLI | ~1 min |

<details>
<summary><strong>Reproduction commands</strong></summary>

```bash
# SWE-AF (multi-agent pipeline, haiku via turbo preset)
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

</details>

Full source code from all four agents, the automated evaluation script, and agent logs are in [`examples/agent-comparison/`](examples/agent-comparison/).

## Internals

- Architecture: [ARCHITECTURE.md](ARCHITECTURE.md)
- Contribution guide: [CONTRIBUTING.md](CONTRIBUTING.md)
- License: [Apache-2.0](LICENSE)
