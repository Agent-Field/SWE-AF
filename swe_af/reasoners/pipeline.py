"""Internal reasoners for the SWE planning pipeline.

Each reasoner wraps a single agent role (PM, Architect, Tech Lead, Sprint Planner)
and uses router.harness() for actual AI execution. The @router.reasoner() decorator provides
FastAPI endpoints, workflow DAG tracking, and observability via router.note().
"""

from __future__ import annotations

import json
import os
from collections import defaultdict, deque
from pathlib import Path

from pydantic import BaseModel

from swe_af.execution.fatal_error import check_fatal_harness_error
from swe_af.execution.schemas import DEFAULT_AGENT_MAX_TURNS
from swe_af.reasoners.schemas import (
    Architecture,
    PlannedIssue,
    PRD,
    ReviewResult,
)
from swe_af.runtime.providers import runtime_to_harness_adapter

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


def _validate_file_conflicts(issues: list[dict], levels: list[list[str]]) -> list[dict]:
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
    workspace_manifest: dict | None = None,
    prior_user_responses: list[dict] | None = None,
) -> dict:
    """Run the product manager agent to scope a goal into a PRD."""
    router.note("PM starting", tags=["pm", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)

    from swe_af.prompts.product_manager import product_manager_prompts, pm_task_prompt  # noqa: PLC0415
    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415

    system_prompt, _ = product_manager_prompts(
        goal=goal,
        repo_path=repo_path,
        prd_path=paths["prd"],
        additional_context=additional_context,
        prior_user_responses=prior_user_responses,
    )
    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    provider = runtime_to_harness_adapter(ai_provider)

    from swe_af.hitl import (  # noqa: PLC0415
        AskUserBudget,
        approval_webhook_url,
        build_hax_client_from_env,
        run_with_ask_user,
    )

    async def _invoke_pm(prior_user_responses: list[dict] | None) -> PRD | None:
        task_prompt = pm_task_prompt(
            goal=goal,
            repo_path=repo_path,
            prd_path=paths["prd"],
            additional_context=additional_context,
            workspace_manifest=ws_manifest,
            prior_user_responses=prior_user_responses,
        )
        result = await router.harness(
            prompt=task_prompt,
            schema=PRD,
            provider=provider,
            model=model,
            max_turns=max_turns,
            tools=["Read", "Write", "Glob", "Grep", "Bash"],
            permission_mode=permission_mode or None,
            system_prompt=system_prompt,
            cwd=repo_path,
        )
        check_fatal_harness_error(result)
        return result.parsed

    initial_prior = list(prior_user_responses or [])
    parsed = await run_with_ask_user(
        reasoner_fn=_invoke_pm,
        reasoner_kwargs={"prior_user_responses": initial_prior},
        app=router,
        hax_client=build_hax_client_from_env(),
        budget=AskUserBudget(remaining=2),
        webhook_url=approval_webhook_url(router),
        note_label="product_manager",
    )

    if parsed is None:
        raise RuntimeError("Product manager failed to produce a valid PRD")

    router.note("PM complete", tags=["pm", "complete"])
    return parsed.model_dump()


@router.reasoner()
async def run_environment_scout(
    prd: dict,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
    prior_user_responses: list[dict] | None = None,
) -> dict:
    """Negotiate scoped third-party credentials with the user before architecture.

    Runs once between PM and Architect. Reads the PRD + repo, identifies
    third-party services the build will need to talk to, and asks the user
    via a single Hax form for scoped/temporary tokens. The negotiated values
    are returned in ``scoped_credentials`` (env_var -> value); the caller is
    responsible for stashing them in the in-memory store with
    ``store_scoped_credentials(execution_id, creds)``.

    Returns an empty ``scoped_credentials`` dict when:
      * HAX is disabled (``build_hax_client_from_env`` returns None)
      * The LLM decides no credentials are needed (e.g. purely local PRD)
      * The user opts out by leaving every form field blank
    """
    router.note("Environment scout starting", tags=["scout", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    _ensure_paths(base)  # ensure paths exist; we don't write artifacts here

    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415
    from swe_af.hitl import (  # noqa: PLC0415
        AskUserBudget,
        ScoutResult,
        approval_webhook_url,
        build_hax_client_from_env,
        run_with_ask_user,
        store_scoped_credentials,
    )
    from swe_af.prompts.environment_scout import (  # noqa: PLC0415
        SYSTEM_PROMPT,
        environment_scout_task_prompt,
    )

    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    provider = runtime_to_harness_adapter(ai_provider)

    async def _invoke_scout(prior_user_responses: list[dict] | None) -> ScoutResult | None:
        task_prompt = environment_scout_task_prompt(
            prd=prd,
            repo_path=repo_path,
            workspace_manifest=ws_manifest,
            prior_user_responses=prior_user_responses,
        )
        result = await router.harness(
            prompt=task_prompt,
            schema=ScoutResult,
            provider=provider,
            model=model,
            max_turns=max_turns,
            tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode=permission_mode or None,
            system_prompt=SYSTEM_PROMPT,
            cwd=repo_path,
        )
        check_fatal_harness_error(result)
        return result.parsed

    initial_prior = list(prior_user_responses or [])
    parsed = await run_with_ask_user(
        reasoner_fn=_invoke_scout,
        reasoner_kwargs={"prior_user_responses": initial_prior},
        app=router,
        hax_client=build_hax_client_from_env(),
        budget=AskUserBudget(remaining=2),
        webhook_url=approval_webhook_url(router),
        note_label="environment_scout",
    )

    if parsed is None:
        router.note(
            "Scout produced no parseable result — proceeding without credentials",
            tags=["scout", "fallback"],
        )
        return ScoutResult(
            summary="Scout produced no parseable result; proceeding without credentials.",
        ).model_dump(exclude={"scoped_credentials"})

    # Stash credentials in the process-local in-memory store under the build's
    # run_id (shared across every reasoner in this build). This MUST happen
    # before we strip them out of the return value — otherwise the build()
    # caller has no way to retrieve them.
    ctx = getattr(router, "ctx", None)
    scope_id = (
        getattr(ctx, "run_id", None) or getattr(ctx, "root_workflow_id", None) or ""
    )
    if scope_id and parsed.scoped_credentials:
        store_scoped_credentials(scope_id, parsed.scoped_credentials)

    creds_count = len(parsed.scoped_credentials)
    skipped_count = len(parsed.skipped_services)
    router.note(
        f"Scout complete: {creds_count} credential(s) negotiated, "
        f"{skipped_count} skipped",
        tags=["scout", "complete"],
    )
    # SAFETY: scoped_credentials is EXCLUDED from the returned dict. The
    # control plane logs reasoner return values, so any value in this dict is
    # persisted. The credentials live only in process memory via the call
    # above; downstream reasoners retrieve them with get_scoped_credentials
    # using the same scope_id (router.ctx.run_id).
    return parsed.model_dump(exclude={"scoped_credentials"})


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
    workspace_manifest: dict | None = None,
) -> dict:
    """Run the architect agent to produce a technical architecture."""
    router.note("Architect starting", tags=["architect", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)

    prd_obj = PRD(**prd)
    from swe_af.prompts.architect import architect_prompts, architect_task_prompt  # noqa: PLC0415
    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415

    system_prompt, _ = architect_prompts(
        prd=prd_obj,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        feedback=feedback or None,
    )
    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    task_prompt = architect_task_prompt(
        prd=prd_obj,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        feedback=feedback or None,
        workspace_manifest=ws_manifest,
    )
    provider = runtime_to_harness_adapter(ai_provider)
    result = await router.harness(
        prompt=task_prompt,
        schema=Architecture,
        provider=provider,
        model=model,
        max_turns=max_turns,
        tools=["Read", "Write", "Glob", "Grep", "Bash"],
        permission_mode=permission_mode or None,
        system_prompt=system_prompt,
        cwd=repo_path,
    )
    check_fatal_harness_error(result)
    if result.parsed is None:
        raise RuntimeError("Architect failed to produce a valid architecture")

    router.note("Architect complete", tags=["architect", "complete"])
    return result.parsed.model_dump()


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
    workspace_manifest: dict | None = None,
) -> dict:
    """Run the tech lead agent to review the architecture against the PRD."""
    router.note("Tech Lead starting", tags=["tech_lead", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)

    from swe_af.prompts.tech_lead import tech_lead_prompts, tech_lead_task_prompt  # noqa: PLC0415
    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415

    system_prompt, _ = tech_lead_prompts(
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        revision_number=revision_number,
    )
    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    task_prompt = tech_lead_task_prompt(
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        revision_number=revision_number,
        workspace_manifest=ws_manifest,
    )
    provider = runtime_to_harness_adapter(ai_provider)
    result = await router.harness(
        prompt=task_prompt,
        schema=ReviewResult,
        provider=provider,
        model=model,
        max_turns=max_turns,
        tools=["Read", "Write", "Glob", "Grep"],
        permission_mode=permission_mode or None,
        system_prompt=system_prompt,
        cwd=repo_path,
    )
    check_fatal_harness_error(result)
    if result.parsed is None:
        raise RuntimeError("Tech lead failed to produce a valid review")

    review = result.parsed.model_dump()
    review_json_path = os.path.join(base, "plan", "review.json")
    with open(review_json_path, "w") as f:
        json.dump(review, f, indent=2, default=str)

    router.note("Tech Lead complete", tags=["tech_lead", "complete"])
    return review


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
    workspace_manifest: dict | None = None,
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

    prd_obj = PRD(**prd)
    arch_obj = Architecture(**architecture)
    from swe_af.prompts.sprint_planner import (
        sprint_planner_prompts,
        sprint_planner_task_prompt,
    )  # noqa: PLC0415
    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415

    system_prompt, _ = sprint_planner_prompts(
        prd=prd_obj,
        architecture=arch_obj,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
    )
    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    task_prompt = sprint_planner_task_prompt(
        goal=prd_obj.validated_description,
        prd=prd_obj,
        architecture=arch_obj,
        workspace_manifest=ws_manifest,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
    )
    provider = runtime_to_harness_adapter(ai_provider)
    result = await router.harness(
        prompt=task_prompt,
        schema=SprintPlanOutput,
        provider=provider,
        model=model,
        max_turns=max_turns,
        tools=["Read", "Write", "Glob", "Grep"],
        permission_mode=permission_mode or None,
        system_prompt=system_prompt,
        cwd=repo_path,
    )
    check_fatal_harness_error(result)
    if result.parsed is None:
        raise RuntimeError("Sprint planner failed to produce valid issues")

    router.note("Sprint Planner complete", tags=["sprint_planner", "complete"])
    return {
        "issues": [issue.model_dump() for issue in result.parsed.issues],
        "rationale": result.parsed.rationale,
    }
