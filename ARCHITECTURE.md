# Architecture

AgentNode is an AgentField node that exposes a set of reasoner endpoints for autonomous software delivery.

## Runtime model

- The node process (`main.py`) registers as `swe-planner`.
- Each reasoner endpoint is callable through AgentField async execution APIs.
- Long-running state is persisted in artifact files so runs can resume after crashes.

## Pipeline shape

The core flow in `build` is:

1. `plan` and `run_git_init` execute in parallel.
2. `execute` runs issue DAG levels with per-issue coding loops.
3. `run_verifier` checks output against acceptance criteria.
4. Optional fix cycles (`generate_fix_issues` + `execute`) run when verification fails.

## Planning

`plan` orchestrates:

1. Product Manager -> PRD.
2. Architect -> system design.
3. Tech Lead review loop (bounded iterations).
4. Sprint Planner -> issue DAG.
5. Issue Writer fan-out across all issues.

Dependency levels and file conflict checks are computed before issue execution.

## Execution and recovery

`execute` uses `execution/dag_executor.py` and supports:

- Parallel issue execution by DAG level.
- Bounded coder/QA/review retry loops.
- Issue Advisor adaptations and debt recording.
- Replanning when repeated failures block progress.
- Optional per-issue git worktrees and merge/integration phases.
- Checkpoint-based resume (`resume_build`).

## AI provider abstraction

`agent_ai/` provides provider-neutral orchestration with adapters for:

- Claude (`claude`).
- Codex (`codex`).

Provider selection is done through `BuildConfig.ai_provider` and `ExecutionConfig.ai_provider`.

## Artifact layout

Default artifact root is `.artifacts/`:

- `plan/`: PRD, architecture, generated issue specs.
- `execution/`: checkpoints, iteration history, runtime details.
- `verification/`: acceptance criteria verification output.
- `logs/`: per-agent JSONL traces.
