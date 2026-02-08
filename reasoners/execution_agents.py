"""Execution-phase reasoners: retry advisor, replanner, issue writer, verifier.

These are registered on the same router as the planning reasoners and become
visible in the AgentField call graph when invoked via ``app.call()``.
"""

from __future__ import annotations

import os

from pydantic import BaseModel

from claude_ai import ClaudeAI, ClaudeAIConfig
from claude_ai.types import Tool
from execution.schemas import (
    DEFAULT_AGENT_MAX_TURNS,
    CodeReviewResult,
    CoderResult,
    GitInitResult,
    IntegrationTestResult,
    MergeResult,
    QAResult,
    QASynthesisResult,
    ReplanAction,
    ReplanDecision,
    RetryAdvice,
    VerificationResult,
    WorkspaceInfo,
)
from prompts.code_reviewer import SYSTEM_PROMPT as CODE_REVIEWER_SYSTEM_PROMPT
from prompts.code_reviewer import code_reviewer_task_prompt
from prompts.coder import SYSTEM_PROMPT as CODER_SYSTEM_PROMPT
from prompts.coder import coder_task_prompt
from prompts.git_init import SYSTEM_PROMPT as GIT_INIT_SYSTEM_PROMPT
from prompts.git_init import git_init_task_prompt
from prompts.integration_tester import SYSTEM_PROMPT as INTEGRATION_TESTER_SYSTEM_PROMPT
from prompts.integration_tester import integration_tester_task_prompt
from prompts.issue_writer import SYSTEM_PROMPT as ISSUE_WRITER_SYSTEM_PROMPT
from prompts.issue_writer import issue_writer_task_prompt
from prompts.merger import SYSTEM_PROMPT as MERGER_SYSTEM_PROMPT
from prompts.merger import merger_task_prompt
from prompts.qa import SYSTEM_PROMPT as QA_SYSTEM_PROMPT
from prompts.qa import qa_task_prompt
from prompts.qa_synthesizer import SYSTEM_PROMPT as QA_SYNTHESIZER_SYSTEM_PROMPT
from prompts.qa_synthesizer import qa_synthesizer_task_prompt
from prompts.replanner import SYSTEM_PROMPT as REPLANNER_SYSTEM_PROMPT
from prompts.replanner import replanner_task_prompt
from prompts.retry_advisor import SYSTEM_PROMPT as RETRY_ADVISOR_SYSTEM_PROMPT
from prompts.retry_advisor import retry_advisor_task_prompt
from prompts.verifier import SYSTEM_PROMPT as VERIFIER_SYSTEM_PROMPT
from prompts.verifier import verifier_task_prompt
from prompts.workspace import CLEANUP_SYSTEM_PROMPT as WORKSPACE_CLEANUP_SYSTEM_PROMPT
from prompts.workspace import SETUP_SYSTEM_PROMPT as WORKSPACE_SETUP_SYSTEM_PROMPT
from prompts.workspace import workspace_cleanup_task_prompt, workspace_setup_task_prompt

from . import router


# ---------------------------------------------------------------------------
# Helper for the replanner: reconstruct DAGState from dict
# ---------------------------------------------------------------------------

def _build_dag_state(dag_state_dict: dict):
    """Reconstruct a DAGState from a dict (for prompt building)."""
    from execution.schemas import DAGState
    return DAGState(**dag_state_dict)


def _build_issue_results(failed_issues: list[dict]):
    """Reconstruct IssueResult list from dicts (for prompt building)."""
    from execution.schemas import IssueResult
    return [IssueResult(**f) for f in failed_issues]


# ---------------------------------------------------------------------------
# Reasoners
# ---------------------------------------------------------------------------

@router.reasoner()
async def run_retry_advisor(
    issue: dict,
    error_message: str,
    error_context: str,
    attempt_number: int,
    repo_path: str,
    prd_summary: str = "",
    architecture_summary: str = "",
    prd_path: str = "",
    architecture_path: str = "",
    artifacts_dir: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Diagnose a coding agent failure and advise whether to retry.

    Returns a RetryAdvice dict. On agent failure, returns a safe default
    (should_retry=False) so the executor can proceed.
    """
    router.note(
        f"Retry advisor analyzing {issue.get('name', '?')} (attempt {attempt_number})",
        tags=["retry_advisor", "start"],
    )

    task_prompt = retry_advisor_task_prompt(
        issue=issue,
        error_message=error_message,
        error_context=error_context,
        attempt_number=attempt_number,
        prd_summary=prd_summary,
        architecture_summary=architecture_summary,
        prd_path=prd_path,
        architecture_path=architecture_path,
    )

    issue_name = issue.get("name", "unknown")
    log_dir = os.path.join(artifacts_dir, "logs") if artifacts_dir else None
    log_path = os.path.join(log_dir, f"retry_advisor_{issue_name}_{attempt_number}.jsonl") if log_dir else None

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.READ, Tool.GLOB, Tool.GREP, Tool.BASH],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=RETRY_ADVISOR_SYSTEM_PROMPT,
            output_schema=RetryAdvice,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Retry advisor: should_retry={response.parsed.should_retry}, "
                f"confidence={response.parsed.confidence}",
                tags=["retry_advisor", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Retry advisor agent failed: {e}",
            tags=["retry_advisor", "error"],
        )

    # Fallback: don't retry if the advisor itself failed
    return RetryAdvice(
        should_retry=False,
        diagnosis="Retry advisor agent failed to produce a valid analysis.",
        strategy="Cannot advise — advisor failure.",
        modified_context="",
        confidence=0.0,
    ).model_dump()


@router.reasoner()
async def run_replanner(
    dag_state: dict,
    failed_issues: list[dict],
    replan_model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Invoke the replanner to decide how to handle unrecoverable failures.

    Returns a ReplanDecision dict. On agent failure, falls back to CONTINUE
    (not ABORT) — Pitfall 5 fix: a replanner crash should not kill the pipeline.
    """
    state = _build_dag_state(dag_state)
    failures = _build_issue_results(failed_issues)

    router.note(
        f"Replanner starting (attempt {state.replan_count + 1}/{state.max_replans}): "
        f"failed = {[f.issue_name for f in failures]}",
        tags=["replanner", "start"],
    )

    task_prompt = replanner_task_prompt(state, failures)

    log_dir = os.path.join(state.artifacts_dir, "logs") if state.artifacts_dir else None
    log_path = os.path.join(log_dir, f"replanner_{state.replan_count}.jsonl") if log_dir else None

    ai = ClaudeAI(ClaudeAIConfig(
        model=replan_model,
        cwd=state.repo_path or ".",
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.READ, Tool.GLOB, Tool.GREP, Tool.BASH],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=REPLANNER_SYSTEM_PROMPT,
            output_schema=ReplanDecision,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Replan decision: {response.parsed.action.value} — {response.parsed.summary}",
                tags=["replanner", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Replanner agent failed: {e}",
            tags=["replanner", "error"],
        )

    # Pitfall 5 fix: fall back to CONTINUE, not ABORT
    # Skip downstream of failed issues but don't kill the pipeline
    failed_names = [f.issue_name for f in failures]
    fallback = ReplanDecision(
        action=ReplanAction.CONTINUE,
        rationale=(
            "Replanner agent failed to produce a valid decision. "
            "Falling back to CONTINUE — downstream of failed issues will be "
            "notified of the gap but the pipeline will proceed."
        ),
        skipped_issue_names=[],
        summary=f"Replanner failure — continuing with gap notification for: {failed_names}",
    )
    router.note(
        "Replanner failed — falling back to CONTINUE (not ABORT)",
        tags=["replanner", "fallback"],
    )
    return fallback.model_dump()


@router.reasoner()
async def run_issue_writer(
    issue: dict,
    prd_summary: str,
    architecture_summary: str,
    issues_dir: str,
    repo_path: str,
    prd_path: str = "",
    architecture_path: str = "",
    sibling_issues: list[dict] | None = None,
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Write a lean issue-*.md file for a new or updated issue.

    Returns {issue_name, issue_file_path, success}.
    Multiple instances can run in parallel (one per issue).
    """
    issue_name = issue.get("name", "unknown")
    # issues_dir is <artifacts>/plan/issues — derive log_dir from grandparent
    _artifacts_base = os.path.dirname(os.path.dirname(issues_dir)) if issues_dir else ""
    log_dir = os.path.join(_artifacts_base, "logs") if _artifacts_base else None
    log_path = os.path.join(log_dir, f"issue_writer_{issue_name}.jsonl") if log_dir else None

    router.note(
        f"Issue writer starting for {issue_name}",
        tags=["issue_writer", "start"],
    )

    task_prompt = issue_writer_task_prompt(
        issue=issue,
        prd_summary=prd_summary,
        architecture_summary=architecture_summary,
        issues_dir=issues_dir,
        prd_path=prd_path,
        architecture_path=architecture_path,
        sibling_issues=sibling_issues,
    )

    class IssueWriterOutput(BaseModel):
        issue_name: str
        issue_file_path: str
        success: bool

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=ISSUE_WRITER_SYSTEM_PROMPT,
            output_schema=IssueWriterOutput,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Issue writer complete for {issue_name}: {response.parsed.issue_file_path}",
                tags=["issue_writer", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Issue writer failed for {issue_name}: {e}",
            tags=["issue_writer", "error"],
        )

    # Fallback: issue file wasn't written but we don't block on it
    return {
        "issue_name": issue_name,
        "issue_file_path": "",
        "success": False,
    }


@router.reasoner()
async def run_verifier(
    prd: dict,
    repo_path: str,
    artifacts_dir: str,
    completed_issues: list[dict],
    failed_issues: list[dict],
    skipped_issues: list[str],
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Run final acceptance verification against the PRD.

    Returns a VerificationResult dict.
    """
    log_dir = os.path.join(artifacts_dir, "logs") if artifacts_dir else None
    log_path = os.path.join(log_dir, "verifier.jsonl") if log_dir else None

    router.note("Verifier starting", tags=["verifier", "start"])

    task_prompt = verifier_task_prompt(
        prd=prd,
        artifacts_dir=artifacts_dir,
        completed_issues=completed_issues,
        failed_issues=failed_issues,
        skipped_issues=skipped_issues,
    )

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.READ, Tool.GLOB, Tool.GREP, Tool.BASH],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=VERIFIER_SYSTEM_PROMPT,
            output_schema=VerificationResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Verifier complete: passed={response.parsed.passed}, "
                f"summary={response.parsed.summary}",
                tags=["verifier", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Verifier agent failed: {e}",
            tags=["verifier", "error"],
        )

    # Fallback: verification inconclusive
    return VerificationResult(
        passed=False,
        criteria_results=[],
        summary="Verifier agent failed to produce a valid result.",
        suggested_fixes=["Re-run verification manually."],
    ).model_dump()


# ---------------------------------------------------------------------------
# Phase 3: Git workflow reasoners
# ---------------------------------------------------------------------------


@router.reasoner()
async def run_git_init(
    repo_path: str,
    goal: str,
    artifacts_dir: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Initialize git repo and create integration branch for feature work.

    Returns a GitInitResult dict.
    """
    log_dir = os.path.join(artifacts_dir, "logs") if artifacts_dir else None
    log_path = os.path.join(log_dir, "git_init.jsonl") if log_dir else None

    router.note(
        f"Git init starting for: {goal[:80]}",
        tags=["git_init", "start"],
    )

    task_prompt = git_init_task_prompt(repo_path=repo_path, goal=goal)

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.BASH],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=GIT_INIT_SYSTEM_PROMPT,
            output_schema=GitInitResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Git init complete: mode={response.parsed.mode}, "
                f"integration_branch={response.parsed.integration_branch}",
                tags=["git_init", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Git init agent failed: {e}",
            tags=["git_init", "error"],
        )

    # Fallback: report failure
    return GitInitResult(
        mode="unknown",
        original_branch="",
        integration_branch="",
        initial_commit_sha="",
        success=False,
        error_message="Git init agent failed to produce a valid result.",
    ).model_dump()


@router.reasoner()
async def run_workspace_setup(
    repo_path: str,
    integration_branch: str,
    issues: list[dict],
    worktrees_dir: str,
    artifacts_dir: str = "",
    level: int = 0,
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Create git worktrees for parallel issue isolation.

    Returns {workspaces: [WorkspaceInfo, ...], success: bool}.
    """
    log_dir = os.path.join(artifacts_dir, "logs") if artifacts_dir else None
    log_path = os.path.join(log_dir, f"workspace_setup_level_{level}.jsonl") if log_dir else None

    issue_names = [i.get("name", "?") for i in issues]
    router.note(
        f"Workspace setup: creating {len(issues)} worktrees for {issue_names}",
        tags=["workspace_setup", "start"],
    )

    task_prompt = workspace_setup_task_prompt(
        repo_path=repo_path,
        integration_branch=integration_branch,
        issues=issues,
        worktrees_dir=worktrees_dir,
    )

    class WorkspaceSetupResult(BaseModel):
        workspaces: list[WorkspaceInfo]
        success: bool

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.BASH],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=WORKSPACE_SETUP_SYSTEM_PROMPT,
            output_schema=WorkspaceSetupResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Workspace setup complete: {len(response.parsed.workspaces)} worktrees created",
                tags=["workspace_setup", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Workspace setup agent failed: {e}",
            tags=["workspace_setup", "error"],
        )

    return {"workspaces": [], "success": False}


@router.reasoner()
async def run_merger(
    repo_path: str,
    integration_branch: str,
    branches_to_merge: list[dict],
    file_conflicts: list[dict],
    prd_summary: str,
    architecture_summary: str,
    artifacts_dir: str = "",
    level: int = 0,
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Merge level branches into the integration branch with AI conflict resolution.

    Returns a MergeResult dict.
    """
    branch_names = [b.get("branch_name", "?") for b in branches_to_merge]
    log_dir = os.path.join(artifacts_dir, "logs") if artifacts_dir else None
    log_path = os.path.join(log_dir, f"merger_level_{level}.jsonl") if log_dir else None

    router.note(
        f"Merger starting: {len(branches_to_merge)} branches {branch_names}",
        tags=["merger", "start"],
    )

    task_prompt = merger_task_prompt(
        repo_path=repo_path,
        integration_branch=integration_branch,
        branches_to_merge=branches_to_merge,
        file_conflicts=file_conflicts,
        prd_summary=prd_summary,
        architecture_summary=architecture_summary,
    )

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.BASH, Tool.READ, Tool.GLOB, Tool.GREP],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=MERGER_SYSTEM_PROMPT,
            output_schema=MergeResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Merger complete: merged={response.parsed.merged_branches}, "
                f"failed={response.parsed.failed_branches}, "
                f"needs_test={response.parsed.needs_integration_test}",
                tags=["merger", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Merger agent failed: {e}",
            tags=["merger", "error"],
        )

    return MergeResult(
        success=False,
        merged_branches=[],
        failed_branches=branch_names,
        needs_integration_test=False,
        summary="Merger agent failed to produce a valid result.",
    ).model_dump()


@router.reasoner()
async def run_integration_tester(
    repo_path: str,
    integration_branch: str,
    merged_branches: list[dict],
    prd_summary: str,
    architecture_summary: str,
    conflict_resolutions: list[dict],
    artifacts_dir: str = "",
    level: int = 0,
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Run integration tests on merged code to verify cross-feature interactions.

    Returns an IntegrationTestResult dict.
    """
    log_dir = os.path.join(artifacts_dir, "logs") if artifacts_dir else None
    log_path = os.path.join(log_dir, f"integration_tester_level_{level}.jsonl") if log_dir else None

    router.note(
        f"Integration tester starting: {len(merged_branches)} merged branches",
        tags=["integration_tester", "start"],
    )

    task_prompt = integration_tester_task_prompt(
        repo_path=repo_path,
        integration_branch=integration_branch,
        merged_branches=merged_branches,
        prd_summary=prd_summary,
        architecture_summary=architecture_summary,
        conflict_resolutions=conflict_resolutions,
    )

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.BASH, Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=INTEGRATION_TESTER_SYSTEM_PROMPT,
            output_schema=IntegrationTestResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Integration tester complete: passed={response.parsed.passed}, "
                f"{response.parsed.tests_passed}/{response.parsed.tests_run} tests passed",
                tags=["integration_tester", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Integration tester agent failed: {e}",
            tags=["integration_tester", "error"],
        )

    return IntegrationTestResult(
        passed=False,
        tests_run=0,
        tests_passed=0,
        tests_failed=0,
        summary="Integration tester agent failed to produce a valid result.",
    ).model_dump()


@router.reasoner()
async def run_workspace_cleanup(
    repo_path: str,
    worktrees_dir: str,
    branches_to_clean: list[str],
    artifacts_dir: str = "",
    level: int = 0,
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Remove worktrees and optionally delete merged branches.

    Returns {success: bool, cleaned: list[str]}.
    """
    log_dir = os.path.join(artifacts_dir, "logs") if artifacts_dir else None
    log_path = os.path.join(log_dir, f"workspace_cleanup_level_{level}.jsonl") if log_dir else None

    router.note(
        f"Workspace cleanup: {len(branches_to_clean)} branches to clean",
        tags=["workspace_cleanup", "start"],
    )

    task_prompt = workspace_cleanup_task_prompt(
        repo_path=repo_path,
        worktrees_dir=worktrees_dir,
        branches_to_clean=branches_to_clean,
    )

    class WorkspaceCleanupResult(BaseModel):
        success: bool
        cleaned: list[str] = []

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=repo_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.BASH],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=WORKSPACE_CLEANUP_SYSTEM_PROMPT,
            output_schema=WorkspaceCleanupResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Workspace cleanup complete: {len(response.parsed.cleaned)} cleaned",
                tags=["workspace_cleanup", "complete"],
            )
            return response.parsed.model_dump()
    except Exception as e:
        router.note(
            f"Workspace cleanup agent failed: {e}",
            tags=["workspace_cleanup", "error"],
        )

    return {"success": False, "cleaned": []}


# ---------------------------------------------------------------------------
# Phase 4: Coding loop reasoners
# ---------------------------------------------------------------------------


@router.reasoner()
async def run_coder(
    issue: dict,
    worktree_path: str,
    feedback: str = "",
    iteration: int = 1,
    iteration_id: str = "",
    project_context: dict = {},
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Implement an issue: write code, tests, and commit.

    Returns a CoderResult dict with files_changed, summary, complete.
    """
    issue_name = issue.get("name", "?")
    _artifacts_dir = project_context.get("artifacts_dir", "")
    log_dir = os.path.join(_artifacts_dir, "logs") if _artifacts_dir else None
    log_path = os.path.join(log_dir, f"coder_{issue_name}_iter_{iteration}.jsonl") if log_dir else None

    router.note(
        f"Coder starting: {issue_name} (iteration {iteration})",
        tags=["coder", "start"],
    )

    task_prompt = coder_task_prompt(
        issue=issue,
        worktree_path=worktree_path,
        feedback=feedback,
        iteration=iteration,
        project_context=project_context,
    )

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=worktree_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[
            Tool.READ, Tool.WRITE, Tool.EDIT,
            Tool.BASH, Tool.GLOB, Tool.GREP,
        ],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=CODER_SYSTEM_PROMPT,
            output_schema=CoderResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Coder complete: {issue_name}, "
                f"files={len(response.parsed.files_changed)}, "
                f"complete={response.parsed.complete}",
                tags=["coder", "complete"],
            )
            result = response.parsed.model_dump()
            result["iteration_id"] = iteration_id
            return result
    except Exception as e:
        router.note(
            f"Coder agent failed: {issue_name}: {e}",
            tags=["coder", "error"],
        )

    return CoderResult(
        files_changed=[],
        summary=f"Coder agent failed for {issue_name}",
        complete=False,
        iteration_id=iteration_id,
    ).model_dump()


@router.reasoner()
async def run_qa(
    worktree_path: str,
    coder_result: dict,
    issue: dict,
    iteration_id: str = "",
    project_context: dict = {},
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Review and augment tests, then run the test suite.

    Returns a QAResult dict with passed, summary, failures_file.
    """
    issue_name = issue.get("name", "?")
    _artifacts_dir = project_context.get("artifacts_dir", "")
    log_dir = os.path.join(_artifacts_dir, "logs") if _artifacts_dir else None
    log_path = os.path.join(log_dir, f"qa_{issue_name}_iter_{iteration_id}.jsonl") if log_dir else None

    router.note(
        f"QA starting: {issue_name}",
        tags=["qa", "start"],
    )

    task_prompt = qa_task_prompt(
        worktree_path=worktree_path,
        coder_result=coder_result,
        issue=issue,
        iteration_id=iteration_id,
        project_context=project_context,
    )

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=worktree_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[
            Tool.READ, Tool.WRITE, Tool.EDIT,
            Tool.BASH, Tool.GLOB, Tool.GREP,
        ],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=QA_SYSTEM_PROMPT,
            output_schema=QAResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"QA complete: {issue_name}, passed={response.parsed.passed}",
                tags=["qa", "complete"],
            )
            result = response.parsed.model_dump()
            result["iteration_id"] = iteration_id
            return result
    except Exception as e:
        router.note(
            f"QA agent failed: {issue_name}: {e}",
            tags=["qa", "error"],
        )

    return QAResult(
        passed=False,
        summary=f"QA agent failed for {issue_name}",
        iteration_id=iteration_id,
    ).model_dump()


@router.reasoner()
async def run_code_reviewer(
    worktree_path: str,
    coder_result: dict,
    issue: dict,
    iteration_id: str = "",
    project_context: dict = {},
    model: str = "sonnet",
    permission_mode: str = "",
) -> dict:
    """Review code quality, security, and requirements adherence (read-only).

    Returns a CodeReviewResult dict with approved, blocking, summary, debt_items.
    """
    issue_name = issue.get("name", "?")
    _artifacts_dir = project_context.get("artifacts_dir", "")
    log_dir = os.path.join(_artifacts_dir, "logs") if _artifacts_dir else None
    log_path = os.path.join(log_dir, f"reviewer_{issue_name}_iter_{iteration_id}.jsonl") if log_dir else None

    router.note(
        f"Code reviewer starting: {issue_name}",
        tags=["code_reviewer", "start"],
    )

    task_prompt = code_reviewer_task_prompt(
        worktree_path=worktree_path,
        coder_result=coder_result,
        issue=issue,
        iteration_id=iteration_id,
        project_context=project_context,
    )

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=worktree_path,
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.READ, Tool.GLOB, Tool.GREP],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=CODE_REVIEWER_SYSTEM_PROMPT,
            output_schema=CodeReviewResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"Code reviewer complete: {issue_name}, "
                f"approved={response.parsed.approved}, "
                f"blocking={response.parsed.blocking}",
                tags=["code_reviewer", "complete"],
            )
            result = response.parsed.model_dump()
            result["iteration_id"] = iteration_id
            return result
    except Exception as e:
        router.note(
            f"Code reviewer agent failed: {issue_name}: {e}",
            tags=["code_reviewer", "error"],
        )

    return CodeReviewResult(
        approved=True,  # don't block on reviewer failure
        summary=f"Code reviewer agent failed for {issue_name} — not blocking",
        blocking=False,
        iteration_id=iteration_id,
    ).model_dump()


@router.reasoner()
async def run_qa_synthesizer(
    qa_result: dict,
    review_result: dict,
    iteration_history: list[dict],
    iteration_id: str = "",
    worktree_path: str = "",
    issue_summary: dict = {},
    artifacts_dir: str = "",
    model: str = "haiku",
    permission_mode: str = "",
) -> dict:
    """Merge QA and review feedback, decide fix/approve/block.

    Returns a QASynthesisResult dict with action, summary, feedback_file.
    """
    _issue_name = issue_summary.get("name", "unknown")
    log_dir = os.path.join(artifacts_dir, "logs") if artifacts_dir else None
    log_path = os.path.join(log_dir, f"synthesizer_{_issue_name}_iter_{iteration_id}.jsonl") if log_dir else None

    router.note(
        "QA synthesizer starting",
        tags=["qa_synthesizer", "start"],
    )

    task_prompt = qa_synthesizer_task_prompt(
        qa_result=qa_result,
        review_result=review_result,
        iteration_history=iteration_history,
        iteration_id=iteration_id,
        worktree_path=worktree_path,
        issue_summary=issue_summary,
    )

    ai = ClaudeAI(ClaudeAIConfig(
        model=model,
        cwd=worktree_path or ".",
        max_turns=DEFAULT_AGENT_MAX_TURNS,
        allowed_tools=[Tool.WRITE],
        permission_mode=permission_mode or None,
    ))

    try:
        response = await ai.run(
            task_prompt,
            system_prompt=QA_SYNTHESIZER_SYSTEM_PROMPT,
            output_schema=QASynthesisResult,
            log_file=log_path,
        )
        if response.parsed is not None:
            router.note(
                f"QA synthesizer complete: action={response.parsed.action.value}, "
                f"stuck={response.parsed.stuck}",
                tags=["qa_synthesizer", "complete"],
            )
            result = response.parsed.model_dump()
            result["iteration_id"] = iteration_id
            return result
    except Exception as e:
        router.note(
            f"QA synthesizer agent failed: {e}",
            tags=["qa_synthesizer", "error"],
        )

    # Fallback: if synthesizer fails, check raw results to make a safe decision
    tests_passed = qa_result.get("passed", False)
    review_approved = review_result.get("approved", False)
    review_blocking = review_result.get("blocking", False)

    if tests_passed and review_approved and not review_blocking:
        fallback_action = "approve"
        fallback_summary = "Synthesizer failed but QA passed and review approved — approving."
    elif review_blocking:
        fallback_action = "block"
        fallback_summary = "Synthesizer failed and review has blocking issues — blocking."
    else:
        fallback_action = "fix"
        fallback_summary = (
            "Synthesizer failed — defaulting to FIX. "
            f"QA passed={tests_passed}, review approved={review_approved}."
        )

    return QASynthesisResult(
        action=fallback_action,
        summary=fallback_summary,
        stuck=False,
        iteration_id=iteration_id,
    ).model_dump()
