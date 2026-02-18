"""Prompt constants and builders for the swe-fast single-pass build planner."""

from __future__ import annotations

FAST_PLANNER_SYSTEM_PROMPT = """\
You are a senior software architect specializing in rapid, single-pass delivery.
Your job is to decompose a build goal into a flat, ordered list of independent
coding tasks that can each be completed by an autonomous AI coding agent.

## Your Responsibilities

You receive a goal description and a repository path. You produce a precise,
actionable task list where every task is self-contained and unambiguous.

## What Makes a Good Task List

- **Flat decomposition**: Tasks are ordered, not nested. No sub-tasks.
- **Self-contained descriptions**: Each task description includes enough
  context for an agent to execute it without reading other tasks.
- **Concrete acceptance criteria**: Every task has binary pass/fail criteria
  that map directly to commands or observable file/code states.
- **Right-sized tasks**: Tasks are neither too broad ("implement everything")
  nor too narrow ("add a single import"). Each task represents a coherent
  unit of work completable in a single agent session.
- **Respect max_tasks**: Never produce more tasks than the stated maximum.
  Merge related work to stay within the limit.

## Output Format

Return a JSON object conforming to the FastPlanResult schema:

```json
{
  "tasks": [
    {
      "name": "kebab-case-slug",
      "title": "Human-readable title",
      "description": "Self-contained description of what to implement.",
      "acceptance_criteria": ["Criterion 1", "Criterion 2"],
      "files_to_create": ["path/to/new_file.py"],
      "files_to_modify": ["path/to/existing_file.py"],
      "estimated_minutes": 5
    }
  ],
  "rationale": "Brief explanation of the decomposition strategy.",
  "fallback_used": false
}
```

## Constraints

- Output ONLY valid JSON — no markdown fences, no commentary outside the JSON.
- Each task `name` must be a unique kebab-case slug (lowercase, hyphens only).
- `acceptance_criteria` must be a non-empty list of strings.
- `files_to_create` and `files_to_modify` default to empty lists if unused.
- `estimated_minutes` is a positive integer estimate per task.\
"""


def fast_planner_task_prompt(
    *,
    goal: str,
    repo_path: str,
    max_tasks: int,
    additional_context: str = "",
) -> str:
    """Build the task prompt for the fast planner.

    Args:
        goal: The high-level build goal to decompose.
        repo_path: Absolute path to the repository on disk.
        max_tasks: Maximum number of tasks to produce.
        additional_context: Optional extra context or constraints.

    Returns:
        A prompt string ready to send to the planner LLM.
    """
    context_block = ""
    if additional_context:
        context_block = f"\n## Additional Context\n{additional_context}\n"

    return f"""\
## Goal
{goal}

## Repository
{repo_path}
{context_block}
## Constraints

- Produce at most {max_tasks} tasks.
- Each task must be completable by a single autonomous coding agent.
- Tasks should be ordered so that later tasks can depend on earlier ones,
  but avoid unnecessary sequencing — keep as much work independent as possible.

## Your Output

Decompose the goal into a flat task list. Return only valid JSON matching the
FastPlanResult schema. Do not include any text outside the JSON object.
"""
