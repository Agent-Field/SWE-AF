"""Internal reasoners for the SWE planning pipeline.

Each reasoner wraps a single agent role (PM, Architect, Tech Lead, Sprint Planner)
and uses AgentAI for actual AI execution. The @router.reasoner() decorator provides
FastAPI endpoints, workflow DAG tracking, and observability via router.note().
"""

from __future__ import annotations

import os
from collections import defaultdict, deque
from pathlib import Path

from pydantic import BaseModel

from agent_ai import AgentAI, AgentAIConfig
from agent_ai.types import Tool
from execution.schemas import DEFAULT_AGENT_MAX_TURNS
from prompts.architect import architect_prompts
from prompts.product_manager import product_manager_prompts
from prompts.sprint_planner import sprint_planner_prompts
from prompts.tech_lead import tech_lead_prompts
from reasoners.schemas import (
    Architecture,
    PlannedIssue,
    PRD,
    ReviewResult,
)

from . import router


# ---------------------------------------------------------------------------
# Pure helpers (NOT reasoners)
# ---------------------------------------------------------------------------

def _ensure_paths(base: str) -> dict[str, str]:
    """Create artifact directories under *base* and return a path map."""
    paths = {
        "base": base,
        "logs": os.path.join(base, "logs"),
        "plan": os.path.join(base, "plan"),
        "issues": os.path.join(base, "plan", "issues"),
        "prd": os.path.join(base, "plan", "prd.md"),
        "architecture": os.path.join(base, "plan", "architecture.md"),
        "review": os.path.join(base, "plan", "review.md"),
        "rationale": os.path.join(base, "rationale.md"),
    }
    for d in ("logs", "plan", "issues"):
        Path(paths[d]).mkdir(parents=True, exist_ok=True)
    return paths


def _compute_levels(issues: list[dict]) -> list[list[str]]:
    """Topological sort of issues into parallel execution levels (Kahn's algorithm).

    Accepts a list of issue dicts (each must have ``name`` and ``depends_on`` keys).
    Returns a list of levels where each level is a list of issue names that can
    execute concurrently (all their dependencies are in prior levels).

    Raises ValueError on dependency cycles.
    """
    name_set = {i["name"] for i in issues}
    in_degree: dict[str, int] = {i["name"]: 0 for i in issues}
    dependents: dict[str, list[str]] = defaultdict(list)

    for issue in issues:
        for dep in issue.get("depends_on", []):
            if dep in name_set:
                in_degree[issue["name"]] += 1
                dependents[dep].append(issue["name"])

    queue: deque[str] = deque(n for n, d in in_degree.items() if d == 0)
    levels: list[list[str]] = []
    processed = 0

    while queue:
        level = list(queue)
        levels.append(level)
        processed += len(level)
        queue.clear()
        for name in level:
            for dep_name in dependents[name]:
                in_degree[dep_name] -= 1
                if in_degree[dep_name] == 0:
                    queue.append(dep_name)

    if processed != len(issues):
        cycle_nodes = [n for n, d in in_degree.items() if d > 0]
        raise ValueError(f"Dependency cycle detected among issues: {cycle_nodes}")

    return levels


def _validate_file_conflicts(
    issues: list[dict], levels: list[list[str]]
) -> list[dict]:
    """Detect file conflicts between issues scheduled at the same parallel level.

    For each level, collects ``files_to_modify`` and ``files_to_create`` across
    all issues in that level.  If any file appears in more than one issue at the
    same level, it is reported as a conflict (parallel agents would overwrite
    each other).

    Returns a list of conflict dicts, e.g.::

        [{"level": 0, "file": "src/ops.rs", "issues": ["arithmetic-ops", "logical-ops"]}]

    An empty list means no conflicts were detected.
    """
    issue_by_name: dict[str, dict] = {i["name"]: i for i in issues}
    conflicts: list[dict] = []

    for level_idx, level_names in enumerate(levels):
        file_to_issues: dict[str, list[str]] = defaultdict(list)
        for name in level_names:
            issue = issue_by_name.get(name)
            if issue is None:
                continue
            for f in issue.get("files_to_create", []):
                file_to_issues[f].append(name)
            for f in issue.get("files_to_modify", []):
                file_to_issues[f].append(name)

        for filepath, touching_issues in file_to_issues.items():
            if len(touching_issues) > 1:
                conflicts.append(
                    {
                        "level": level_idx,
                        "file": filepath,
                        "issues": touching_issues,
                    }
                )

    return conflicts


def _assign_sequence_numbers(issues: list[dict], levels: list[list[str]]) -> list[dict]:
    """Assign 1-based sequential numbers based on topo-sorted level order.

    Numbers are assigned by flattening levels in order. Within each level,
    the sprint planner's original ordering is preserved. The ``sequence_number``
    is used only for display/file naming — ``name`` remains the canonical ID.
    """
    issue_by_name = {i["name"]: i for i in issues}
    counter = 1
    for level_names in levels:
        level_set = set(level_names)
        # Preserve sprint planner's ordering within each level
        for issue in issues:
            if issue["name"] in level_set:
                issue_by_name[issue["name"]]["sequence_number"] = counter
                counter += 1
    return list(issue_by_name.values())


# ---------------------------------------------------------------------------
# Reasoners
# ---------------------------------------------------------------------------

@router.reasoner()
async def run_product_manager(
    goal: str,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    additional_context: str = "",
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Run the product manager agent to scope a goal into a PRD."""
    router.note("PM starting", tags=["pm", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)
    log_path = os.path.join(base, "logs", "product_manager.jsonl")

    ai = AgentAI(AgentAIConfig(
        model=model,
        provider=ai_provider,
        cwd=repo_path,
        max_turns=max_turns,
        allowed_tools=[Tool.READ, Tool.GLOB, Tool.GREP, Tool.BASH],
        permission_mode=permission_mode or None,
    ))

    system_prompt, task_prompt = product_manager_prompts(
        goal=goal,
        repo_path=repo_path,
        prd_path=paths["prd"],
        additional_context=additional_context,
    )
    response = await ai.run(
        task_prompt,
        system_prompt=system_prompt,
        output_schema=PRD,
        log_file=log_path,
    )
    if response.parsed is None:
        raise RuntimeError("Product manager failed to produce a valid PRD")

    router.note("PM complete", tags=["pm", "complete"])
    return response.parsed.model_dump()


@router.reasoner()
async def run_architect(
    prd: dict,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    feedback: str = "",
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Run the architect agent to produce a technical architecture."""
    router.note("Architect starting", tags=["architect", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)
    log_path = os.path.join(base, "logs", "architect.jsonl")

    ai = AgentAI(AgentAIConfig(
        model=model,
        provider=ai_provider,
        cwd=repo_path,
        max_turns=max_turns,
        allowed_tools=[Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP, Tool.BASH],
        permission_mode=permission_mode or None,
    ))

    prd_obj = PRD(**prd)
    system_prompt, task_prompt = architect_prompts(
        prd=prd_obj,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        feedback=feedback or None,
    )
    response = await ai.run(
        task_prompt,
        system_prompt=system_prompt,
        output_schema=Architecture,
        log_file=log_path,
    )
    if response.parsed is None:
        raise RuntimeError("Architect failed to produce a valid architecture")

    router.note("Architect complete", tags=["architect", "complete"])
    return response.parsed.model_dump()


@router.reasoner()
async def run_tech_lead(
    prd: dict,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    revision_number: int = 0,
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Run the tech lead agent to review the architecture against the PRD."""
    router.note("Tech Lead starting", tags=["tech_lead", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)
    log_path = os.path.join(base, "logs", "tech_lead.jsonl")

    ai = AgentAI(AgentAIConfig(
        model=model,
        provider=ai_provider,
        cwd=repo_path,
        max_turns=max_turns,
        allowed_tools=[Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP],
        permission_mode=permission_mode or None,
    ))

    system_prompt, task_prompt = tech_lead_prompts(
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        review_path=paths["review"],
        revision_number=revision_number,
    )
    response = await ai.run(
        task_prompt,
        system_prompt=system_prompt,
        output_schema=ReviewResult,
        log_file=log_path,
    )
    if response.parsed is None:
        raise RuntimeError("Tech lead failed to produce a valid review")

    router.note("Tech Lead complete", tags=["tech_lead", "complete"])
    return response.parsed.model_dump()


@router.reasoner()
async def run_sprint_planner(
    prd: dict,
    architecture: dict,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Run the sprint planner to decompose work into executable issues.

    Returns a dict with ``issues`` (list of issue dicts) and ``rationale`` (str).
    """
    router.note("Sprint Planner starting", tags=["sprint_planner", "start"])

    class SprintPlanOutput(BaseModel):
        issues: list[PlannedIssue]
        rationale: str

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)
    log_path = os.path.join(base, "logs", "sprint_planner.jsonl")

    ai = AgentAI(AgentAIConfig(
        model=model,
        provider=ai_provider,
        cwd=repo_path,
        max_turns=max_turns,
        allowed_tools=[Tool.READ, Tool.GLOB, Tool.GREP],
        permission_mode=permission_mode or None,
    ))

    prd_obj = PRD(**prd)
    arch_obj = Architecture(**architecture)
    system_prompt, task_prompt = sprint_planner_prompts(
        prd=prd_obj,
        architecture=arch_obj,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
    )
    response = await ai.run(
        task_prompt,
        system_prompt=system_prompt,
        output_schema=SprintPlanOutput,
        log_file=log_path,
    )
    if response.parsed is None:
        raise RuntimeError("Sprint planner failed to produce valid issues")

    router.note("Sprint Planner complete", tags=["sprint_planner", "complete"])
    return {
        "issues": [issue.model_dump() for issue in response.parsed.issues],
        "rationale": response.parsed.rationale,
    }
