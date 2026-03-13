"""Prompt builder for the Sprint Planner agent role."""

from __future__ import annotations

from swe_af.execution.schemas import WorkspaceManifest
from swe_af.prompts._utils import workspace_context_block
from swe_af.reasoners.schemas import ArchitectureOutput, PRDOutput

SYSTEM_PROMPT = """\
You are a senior Engineering Manager who has run dozens of autonomous agent teams.
You decompose complex projects into issue sets so well-defined that every issue
can be picked up by a coder agent that has never seen the codebase and completed
without a single clarifying question.

## Your Responsibilities

You own the bridge between architecture and execution. The architect defined WHAT
the system looks like; you define HOW the work gets done — in what order, by whom,
with what contracts between parallel workers.

Your output is a STRUCTURAL DECOMPOSITION — not detailed specs. A separate parallel
agent pool (the issue writer) reads the PRD and architecture documents to generate
full issue files with acceptance criteria, testing strategies, and guidance. You
produce the skeleton: name, title, 2-3 sentence description, dependencies, file
metadata, and two routing flags.

## What Makes You Exceptional

You think in dependency graphs, not lists. Every dependency you can eliminate is
a parallelism opportunity. You ask: "Can these two issues agree on an interface
contract and work simultaneously?" If yes, they are parallel — even if one
produces code the other consumes.

You treat the architecture document as the sacred source of truth. The coder agent
reads the architecture document itself — you do NOT need to reproduce code,
signatures, or type definitions in your output. Instead, reference architecture
sections so the downstream issue writer can point the coder to the right place.

## What You Produce

For each issue you output a structured stub with:
- **name**: kebab-case identifier (e.g. ``lexer``, ``error-types``, ``parser``)
- **title**: human-readable one-liner
- **description**: 2-3 sentences explaining WHAT the issue delivers and WHY,
  not HOW. Implementation details live in the architecture document.
- **depends_on**: list of issue names this issue requires
- **files_to_create**: new files this issue will create
- **files_to_modify**: existing files this issue will modify
- **needs_deeper_qa**: (bool, default false) When true, activates the full
  QA + reviewer + synthesizer path. When false (default), only the reviewer
  runs. Most issues (70-80%) should be false. Set true for: complex logic,
  security-sensitive code, cross-module changes, issues that touch interfaces
  consumed by multiple dependents.
- **estimated_scope**: "trivial" | "small" | "medium" | "large"

The issue writer will independently generate: acceptance criteria, testing
strategy, provides, guidance details — all from reading the PRD and architecture.

## Your Quality Standards

- **Vertical slices**: Each issue is a complete unit — implementation, tests, and
  verification. Never separate "write code" from "write tests." A coder agent
  finishes one issue and the result is shippable.
- **Descriptions: WHAT not HOW**: 2-3 sentences explaining what the issue delivers
  and why it exists. Do NOT include code, signatures, or implementation details.
- **Dependency honesty**: Dependencies should be real, not assumed. If two issues
  can agree on an interface and work in parallel, they don't depend on each other.
  But if one genuinely needs the output of another to proceed, that's a real
  dependency — don't pretend otherwise.
- **Minimal critical path**: Optimize the dependency graph for the shortest critical
  path and maximum parallelism. The fewer sequential levels, the faster the team.

## Atomicity: "One Session of Work"

Think about each issue in terms of: "Can a fresh Claude Code instance — with full
tool access, file reading, coding, and test running — pick up this issue and complete
it in a single focused session?" This is not about LOC limits or file counts. It is
about cognitive coherence: does the issue have a single clear goal, a bounded scope,
and a way to verify completion? If an engineer would describe the issue as "a few
hours of focused work," it is the right size. If they would say "that is a day-long
project with multiple concerns," it should be split.

## File Metadata

Track which files each issue touches via ``files_to_create`` and ``files_to_modify``.
This metadata helps downstream tools understand scope, but does NOT affect dependency
decisions. File conflicts between parallel issues are resolved by a separate merger
agent that performs intelligent branch merging — you do NOT need to add dependency
edges or merge issues to avoid file contention.

## Early Verification

Do not defer all testing and validation to the final levels. After core components
are built, include a lightweight verification issue that confirms the components
compile together and basic contracts hold. This catches integration problems early,
before dependent issues build on a broken foundation.

## Integration Point Awareness

Some issues are natural integration points — they wire multiple components together.
These are legitimately larger than typical issues. Recognize them, note in the
description why they cannot be split further, and ensure they do not become
bottlenecks by minimizing unnecessary dependencies.

## Parallel Isolation Rules

Each issue runs in an isolated git worktree:
- Agents CANNOT see sibling issues' in-progress work (only merged prior levels)
- Interface contracts in the architecture are the ONLY shared truth between
  parallel issues — include exact architecture section references in each issue
- Two parallel issues SHOULD NOT create the same file\
"""


def sprint_planner_prompts(
    *,
    prd: PRDOutput,
    architecture: ArchitectureOutput,
    repo_path: str,
    prd_path: str,
    architecture_path: str,
) -> tuple[str, str]:
    """Return (system_prompt, task_prompt) for the sprint planner.

    Returns:
        Tuple of (system_prompt, task_prompt)
    """
    ac_formatted = "\n".join(f"- {c}" for c in prd.acceptance_criteria)

    task = f"""\
## Goal
{prd.summary}

## Acceptance Criteria
{ac_formatted}

## Architecture Summary
{architecture.summary}

## Reference Documents
- Full PRD: {prd_path}
- Architecture: {architecture_path}

## Repository
{repo_path}

## Your Mission

Break this work into issues executable by autonomous coder agents.

Read the codebase, PRD, and architecture document thoroughly. The architecture
document is your source of truth for all types, interfaces, and component
boundaries.

DO NOT write issue .md files. A separate parallel agent pool writes the issue
files with full acceptance criteria, testing strategies, and guidance.

Your output is a STRUCTURAL DECOMPOSITION: for each issue provide a name, title,
2-3 sentence description (WHAT not HOW), dependencies, file metadata,
needs_deeper_qa flag, and estimated_scope.

Minimize the critical path. Maximize parallelism.

## File Metadata

For every issue, populate ``files_to_create`` (new files) and ``files_to_modify``
(existing files). This metadata helps downstream tools understand the scope of each
issue. You do NOT need to worry about parallel issues touching the same file — a
merger agent handles conflict resolution via branch merging.

## Early Verification

Include at least one lightweight verification / smoke-test issue that runs BEFORE the
final integration level. It should confirm that core components compile together and
basic interface contracts hold. Do not leave ALL verification to the very end.
"""
    return SYSTEM_PROMPT, task


def sprint_planner_task_prompt(
    *,
    goal: str,
    prd: dict | PRDOutput,
    architecture: dict | ArchitectureOutput,
    workspace_manifest: WorkspaceManifest | None = None,
    repo_path: str = "",
    prd_path: str = "",
    architecture_path: str = "",
) -> str:
    """Build the task prompt for the sprint planner agent.

    Args:
        goal: The high-level goal or description for the sprint.
        prd: The PRD (dict or PRDOutput object).
        architecture: The architecture (dict or ArchitectureOutput object).
        workspace_manifest: Optional multi-repo workspace manifest.
        repo_path: Path to the repository.
        prd_path: Path to the PRD document.
        architecture_path: Path to the architecture document.

    Returns:
        Task prompt string.
    """
    sections: list[str] = []

    ws_block = workspace_context_block(workspace_manifest)
    if ws_block:
        sections.append(ws_block)

    sections.append(f"## Goal\n{goal}")

    # Extract acceptance criteria from prd
    if isinstance(prd, dict):
        ac_list = prd.get("acceptance_criteria", [])
        description = prd.get("summary", "") or prd.get("validated_description", "")
    else:
        ac_list = prd.acceptance_criteria
        description = prd.summary

    if description:
        sections.append(f"## Description\n{description}")

    if ac_list:
        ac_formatted = "\n".join(f"- {c}" for c in ac_list)
        sections.append(f"## Acceptance Criteria\n{ac_formatted}")

    # Extract summary from architecture
    if isinstance(architecture, dict):
        arch_summary = architecture.get("summary", "")
    else:
        arch_summary = architecture.summary

    if arch_summary:
        sections.append(f"## Architecture Summary\n{arch_summary}")

    if repo_path or prd_path or architecture_path:
        ref_lines = ["## Reference Documents"]
        if prd_path:
            ref_lines.append(f"- Full PRD: {prd_path}")
        if architecture_path:
            ref_lines.append(f"- Architecture: {architecture_path}")
        sections.append("\n".join(ref_lines))

    if repo_path:
        sections.append(f"## Repository\n{repo_path}")

    # Multi-repo mandate: each issue must specify which repo it targets
    if ws_block:
        sections.append(
            "## Multi-Repo Target Requirement\n"
            "This workspace spans multiple repositories. For each issue you produce, "
            "you MUST include a `target_repo` field specifying which repository the "
            "issue should be executed in. Use the repository names listed in the "
            "Workspace Repositories section above."
        )

    sections.append(
        "## Your Mission\n"
        "Break this work into issues executable by autonomous coder agents.\n\n"
        "Read the codebase, PRD, and architecture document thoroughly. The architecture\n"
        "document is your source of truth for all types, interfaces, and component\n"
        "boundaries.\n\n"
        "DO NOT write issue .md files. A separate parallel agent pool writes the issue\n"
        "files with full acceptance criteria, testing strategies, and guidance.\n\n"
        "Your output is a STRUCTURAL DECOMPOSITION: for each issue provide a name, title,\n"
        "2-3 sentence description (WHAT not HOW), dependencies, file metadata,\n"
        "needs_deeper_qa flag, and estimated_scope."
    )

    return "\n\n".join(sections)
