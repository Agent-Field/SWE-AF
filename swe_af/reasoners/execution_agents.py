"""Execution-phase reasoners: retry advisor, replanner, issue writer, verifier.

These are registered on the same router as the planning reasoners and become
visible in the AgentField call graph when invoked via ``app.call()``.
"""

from __future__ import annotations

import os

from pydantic import BaseModel

from swe_af.execution.fatal_error import FatalHarnessError, check_fatal_harness_error
from swe_af.execution.ci_gate import watch_pr_checks
from swe_af.execution.schemas import (
    DEFAULT_AGENT_MAX_TURNS,
    AdvisorAction,
    CIFailedCheck,
    CIFixResult,
    CIWatchResult,
    CodeReviewResult,
    CoderResult,
    GitHubPRResult,
    GitInitResult,
    IntegrationTestResult,
    IssueAdvisorDecision,
    MergeResult,
    PRResolveResult,
    QAResult,
    QASynthesisResult,
    ReplanAction,
    ReplanDecision,
    RepoFinalizeResult,
    RetryAdvice,
    ReviewCommentRef,
    VerificationResult,
    WorkspaceInfo,
)
from swe_af.prompts.ci_fixer import SYSTEM_PROMPT as CI_FIXER_SYSTEM_PROMPT
from swe_af.prompts.ci_fixer import ci_fixer_task_prompt
from swe_af.prompts.pr_resolver import SYSTEM_PROMPT as PR_RESOLVER_SYSTEM_PROMPT
from swe_af.prompts.pr_resolver import pr_resolver_task_prompt
from swe_af.prompts.fix_generator import SYSTEM_PROMPT as FIX_GENERATOR_SYSTEM_PROMPT
from swe_af.prompts.fix_generator import fix_generator_task_prompt
from swe_af.prompts.issue_advisor import SYSTEM_PROMPT as ISSUE_ADVISOR_SYSTEM_PROMPT
from swe_af.prompts.issue_advisor import issue_advisor_task_prompt
from swe_af.prompts.code_reviewer import SYSTEM_PROMPT as CODE_REVIEWER_SYSTEM_PROMPT
from swe_af.prompts.code_reviewer import code_reviewer_task_prompt
from swe_af.prompts.coder import SYSTEM_PROMPT as CODER_SYSTEM_PROMPT
from swe_af.prompts.coder import coder_task_prompt
from swe_af.prompts.git_init import SYSTEM_PROMPT as GIT_INIT_SYSTEM_PROMPT
from swe_af.prompts.git_init import git_init_task_prompt
from swe_af.prompts.github_pr import SYSTEM_PROMPT as GITHUB_PR_SYSTEM_PROMPT
from swe_af.prompts.github_pr import github_pr_task_prompt
from swe_af.prompts.repo_finalize import SYSTEM_PROMPT as REPO_FINALIZE_SYSTEM_PROMPT
from swe_af.prompts.repo_finalize import repo_finalize_task_prompt
from swe_af.prompts.integration_tester import (
    SYSTEM_PROMPT as INTEGRATION_TESTER_SYSTEM_PROMPT,
)
from swe_af.prompts.integration_tester import integration_tester_task_prompt
from swe_af.prompts.issue_writer import SYSTEM_PROMPT as ISSUE_WRITER_SYSTEM_PROMPT
from swe_af.prompts.issue_writer import issue_writer_task_prompt
from swe_af.prompts.merger import SYSTEM_PROMPT as MERGER_SYSTEM_PROMPT
from swe_af.prompts.merger import merger_task_prompt
from swe_af.prompts.qa import SYSTEM_PROMPT as QA_SYSTEM_PROMPT
from swe_af.prompts.qa import qa_task_prompt
from swe_af.prompts.qa_synthesizer import SYSTEM_PROMPT as QA_SYNTHESIZER_SYSTEM_PROMPT
from swe_af.prompts.qa_synthesizer import qa_synthesizer_task_prompt
from swe_af.prompts.replanner import SYSTEM_PROMPT as REPLANNER_SYSTEM_PROMPT
from swe_af.prompts.replanner import replanner_task_prompt
from swe_af.prompts.retry_advisor import SYSTEM_PROMPT as RETRY_ADVISOR_SYSTEM_PROMPT
from swe_af.prompts.retry_advisor import retry_advisor_task_prompt
from swe_af.prompts.verifier import SYSTEM_PROMPT as VERIFIER_SYSTEM_PROMPT
from swe_af.prompts.verifier import verifier_task_prompt
from swe_af.prompts.workspace import (
    CLEANUP_SYSTEM_PROMPT as WORKSPACE_CLEANUP_SYSTEM_PROMPT,
)
from swe_af.prompts.workspace import (
    SETUP_SYSTEM_PROMPT as WORKSPACE_SETUP_SYSTEM_PROMPT,
)
from swe_af.prompts.workspace import (
    workspace_cleanup_task_prompt,
    workspace_setup_task_prompt,
)

from . import router


def _maybe_workspace_manifest(raw: dict | None):
    """Deserialize workspace_manifest dict to WorkspaceManifest, or return None."""
    if raw is None:
        return None
    from swe_af.execution.schemas import WorkspaceManifest

    return WorkspaceManifest(**raw)


# ---------------------------------------------------------------------------
# Helper for the replanner: reconstruct DAGState from dict
# ---------------------------------------------------------------------------


def _build_dag_state(dag_state_dict: dict):
    """Reconstruct a DAGState from a dict (for prompt building)."""
    from swe_af.execution.schemas import DAGState

    return DAGState(**dag_state_dict)


def _build_issue_results(failed_issues: list[dict]):
    """Reconstruct IssueResult list from dicts (for prompt building)."""
    from swe_af.execution.schemas import IssueResult

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
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Diagnose a coding agent failure and advise whether to retry.

    Returns a RetryAdvice dict. On agent failure, returns a safe default
    (should_retry=False) so the executor can proceed.
    """
    router.note(
        f"Retry advisor analyzing {issue.get('name', '?')} (attempt {attempt_number})",
        tags=["retry_advisor", "start"],
    )

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = retry_advisor_task_prompt(
        issue=issue,
        error_message=error_message,
        error_context=error_context,
        attempt_number=attempt_number,
        prd_summary=prd_summary,
        architecture_summary=architecture_summary,
        prd_path=prd_path,
        architecture_path=architecture_path,
        workspace_manifest=ws_manifest,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=RETRY_ADVISOR_SYSTEM_PROMPT,
            schema=RetryAdvice,
            model=model,
            provider=provider,
            tools=["Read", "Write", "Glob", "Grep", "Bash"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Retry advisor: should_retry={result.parsed.should_retry}, "
                f"confidence={result.parsed.confidence}",
                tags=["retry_advisor", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
async def run_issue_advisor(
    issue: dict,
    original_issue: dict,
    failure_result: dict,
    iteration_history: list[dict],
    dag_state_summary: dict,
    advisor_invocation: int = 1,
    max_advisor_invocations: int = 2,
    previous_adaptations: list[dict] | None = None,
    worktree_path: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Analyze a coding loop failure and decide how to adapt.

    Returns an IssueAdvisorDecision dict. On agent failure, falls back to
    ACCEPT_WITH_DEBT (never block the pipeline).
    """
    issue_name = issue.get("name", "?")
    router.note(
        f"Issue advisor analyzing {issue_name} (invocation {advisor_invocation}/{max_advisor_invocations})",
        tags=["issue_advisor", "start"],
    )

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = issue_advisor_task_prompt(
        issue=issue,
        original_issue=original_issue,
        failure_result=failure_result,
        iteration_history=iteration_history,
        dag_state_summary=dag_state_summary,
        advisor_invocation=advisor_invocation,
        max_advisor_invocations=max_advisor_invocations,
        previous_adaptations=previous_adaptations,
        worktree_path=worktree_path,
        workspace_manifest=ws_manifest,
    )

    cwd = worktree_path or dag_state_summary.get("repo_path", ".")
    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=ISSUE_ADVISOR_SYSTEM_PROMPT,
            schema=IssueAdvisorDecision,
            model=model,
            provider=provider,
            tools=["Read", "Write", "Glob", "Grep", "Bash"],
            cwd=cwd,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Issue advisor decision: {result.parsed.action.value} — {result.parsed.summary}",
                tags=["issue_advisor", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
    except Exception as e:
        router.note(
            f"Issue advisor agent failed: {e}",
            tags=["issue_advisor", "error"],
        )

    # Fallback: accept with debt rather than blocking the pipeline
    fallback = IssueAdvisorDecision(
        action=AdvisorAction.ACCEPT_WITH_DEBT,
        failure_diagnosis="Issue Advisor agent failed to produce a valid analysis.",
        failure_category="environment",
        rationale="Advisor failure — accepting with debt to avoid pipeline stall.",
        confidence=0.1,
        missing_functionality=[f"Full implementation of {issue_name}"],
        debt_severity="high",
        summary=f"Issue advisor failed — accepting {issue_name} with debt",
    )
    router.note(
        "Issue advisor failed — falling back to ACCEPT_WITH_DEBT",
        tags=["issue_advisor", "fallback"],
    )
    return fallback.model_dump()


@router.reasoner()
async def run_replanner(
    dag_state: dict,
    failed_issues: list[dict],
    replan_model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
    escalation_notes: list[dict] | None = None,
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

    task_prompt = replanner_task_prompt(
        state,
        failures,
        escalation_notes=escalation_notes,
        adaptation_history=state.adaptation_history
        if hasattr(state, "adaptation_history")
        else [],
    )

    log_dir = os.path.join(state.artifacts_dir, "logs") if state.artifacts_dir else None
    provider = "claude-code" if ai_provider == "claude" else ai_provider

    current_prompt = task_prompt
    for attempt in range(2):
        try:
            result = await router.harness(
                current_prompt,
                system_prompt=REPLANNER_SYSTEM_PROMPT,
                schema=ReplanDecision,
                model=replan_model,
                provider=provider,
                tools=["Read", "Write", "Glob", "Grep", "Bash"],
                cwd=state.repo_path or ".",
                max_turns=DEFAULT_AGENT_MAX_TURNS,
                permission_mode=permission_mode or None,
            )
            check_fatal_harness_error(result)
            # Log raw response for debugging (even on parse failure)
            if log_dir:
                raw_log = os.path.join(
                    log_dir, f"replanner_{state.replan_count}_raw_{attempt}.txt"
                )
                os.makedirs(log_dir, exist_ok=True)
                with open(raw_log, "w") as f:
                    f.write(getattr(result, "text", "") or "(empty)")

            if result.parsed is not None:
                router.note(
                    f"Replan decision: {result.parsed.action.value} — {result.parsed.summary}",
                    tags=["replanner", "complete"],
                )
                return result.parsed.model_dump()

            # Parse failed — retry with tighter prompt
            router.note(
                f"Replanner produced unparseable output (attempt {attempt + 1}): "
                f"{(getattr(result, 'text', '') or '')[:500]}",
                tags=["replanner", "parse_error"],
            )
            current_prompt = (
                "YOUR PREVIOUS RESPONSE COULD NOT BE PARSED. "
                "Output ONLY valid JSON conforming to the ReplanDecision schema.\n\n"
                + task_prompt
            )
        except FatalHarnessError:
            raise  # Non-retryable — propagate immediately
        except Exception as e:
            router.note(
                f"Replanner agent failed (attempt {attempt + 1}): {e}",
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
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Write a lean issue-*.md file for a new or updated issue.

    Returns {issue_name, issue_file_path, success}.
    Multiple instances can run in parallel (one per issue).
    """
    issue_name = issue.get("name", "unknown")
    router.note(
        f"Issue writer starting for {issue_name}",
        tags=["issue_writer", "start"],
    )

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = issue_writer_task_prompt(
        issue=issue,
        prd_summary=prd_summary,
        architecture_summary=architecture_summary,
        issues_dir=issues_dir,
        prd_path=prd_path,
        architecture_path=architecture_path,
        sibling_issues=sibling_issues,
        workspace_manifest=ws_manifest,
    )

    class IssueWriterOutput(BaseModel):
        issue_name: str
        issue_file_path: str
        success: bool

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=ISSUE_WRITER_SYSTEM_PROMPT,
            schema=IssueWriterOutput,
            model=model,
            provider=provider,
            tools=["Read", "Write", "Glob", "Grep"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Issue writer complete for {issue_name}: {result.parsed.issue_file_path}",
                tags=["issue_writer", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Run final acceptance verification against the PRD.

    Returns a VerificationResult dict.
    """
    router.note("Verifier starting", tags=["verifier", "start"])

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = verifier_task_prompt(
        prd=prd,
        artifacts_dir=artifacts_dir,
        completed_issues=completed_issues,
        failed_issues=failed_issues,
        skipped_issues=skipped_issues,
        workspace_manifest=ws_manifest,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=VERIFIER_SYSTEM_PROMPT,
            schema=VerificationResult,
            model=model,
            provider=provider,
            tools=["Read", "Write", "Glob", "Grep", "Bash"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Verifier complete: passed={result.parsed.passed}, summary={result.parsed.summary}",
                tags=["verifier", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    ai_provider: str = "claude",
    previous_error: str | None = None,
    build_id: str = "",
) -> dict:
    """Initialize git repo and create integration branch for feature work.

    Returns a GitInitResult dict.

    Args:
        previous_error: If provided, this is a retry attempt and the error context
            will be injected into the system prompt to help the agent learn from
            the previous failure.
    """
    router.note(
        f"Git init starting for: {goal[:80]}",
        tags=["git_init", "start"],
    )

    task_prompt = git_init_task_prompt(
        repo_path=repo_path, goal=goal, build_id=build_id
    )

    # Build system prompt with error context if retrying
    system_prompt = GIT_INIT_SYSTEM_PROMPT
    if previous_error:
        system_prompt += (
            "\n\n## IMPORTANT: Retry Context\n\n"
            f"The previous attempt failed with error: '{previous_error}'\n\n"
            "Please carefully review what went wrong and adjust your approach:\n"
            "- Ensure you provide ALL required fields in the correct format\n"
            "- Double-check your git commands are valid\n"
            "- Verify the GitInitResult JSON structure is complete\n"
            "- If the error indicates a parsing issue, ensure your output is valid JSON\n"
        )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=system_prompt,
            schema=GitInitResult,
            model=model,
            provider=provider,
            tools=["Bash", "Write"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Git init complete: mode={result.parsed.mode}, "
                f"integration_branch={result.parsed.integration_branch}",
                tags=["git_init", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    ai_provider: str = "claude",
    build_id: str = "",
) -> dict:
    """Create git worktrees for parallel issue isolation.

    Returns {workspaces: [WorkspaceInfo, ...], success: bool}.
    """
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
        build_id=build_id,
    )

    class WorkspaceSetupResult(BaseModel):
        workspaces: list[WorkspaceInfo]
        success: bool

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=WORKSPACE_SETUP_SYSTEM_PROMPT,
            schema=WorkspaceSetupResult,
            model=model,
            provider=provider,
            tools=["Bash", "Write"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Workspace setup complete: {len(result.parsed.workspaces)} worktrees created",
                tags=["workspace_setup", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    ai_provider: str = "claude",
) -> dict:
    """Merge level branches into the integration branch with AI conflict resolution.

    Returns a MergeResult dict.
    """
    branch_names = [b.get("branch_name", "?") for b in branches_to_merge]
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

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=MERGER_SYSTEM_PROMPT,
            schema=MergeResult,
            model=model,
            provider=provider,
            tools=["Bash", "Read", "Write", "Glob", "Grep"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Merger complete: merged={result.parsed.merged_branches}, "
                f"failed={result.parsed.failed_branches}, "
                f"needs_test={result.parsed.needs_integration_test}",
                tags=["merger", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Run integration tests on merged code to verify cross-feature interactions.

    Returns an IntegrationTestResult dict.
    """
    router.note(
        f"Integration tester starting: {len(merged_branches)} merged branches",
        tags=["integration_tester", "start"],
    )

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = integration_tester_task_prompt(
        repo_path=repo_path,
        integration_branch=integration_branch,
        merged_branches=merged_branches,
        prd_summary=prd_summary,
        architecture_summary=architecture_summary,
        conflict_resolutions=conflict_resolutions,
        workspace_manifest=ws_manifest,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=INTEGRATION_TESTER_SYSTEM_PROMPT,
            schema=IntegrationTestResult,
            model=model,
            provider=provider,
            tools=["Bash", "Read", "Write", "Glob", "Grep"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Integration tester complete: passed={result.parsed.passed}, "
                f"{result.parsed.tests_passed}/{result.parsed.tests_run} tests passed",
                tags=["integration_tester", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    ai_provider: str = "claude",
) -> dict:
    """Remove worktrees and optionally delete merged branches.

    Returns {success: bool, cleaned: list[str]}.
    """
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

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=WORKSPACE_CLEANUP_SYSTEM_PROMPT,
            schema=WorkspaceCleanupResult,
            model=model,
            provider=provider,
            tools=["Bash", "Write"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Workspace cleanup complete: {len(result.parsed.cleaned)} cleaned",
                tags=["workspace_cleanup", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    project_context: dict | None = None,
    memory_context: dict | None = None,
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
    target_repo: str = "",
) -> dict:
    """Implement an issue: write code, tests, and commit.

    Returns a CoderResult dict with files_changed, summary, complete,
    tests_passed, test_summary, codebase_learnings, agent_retro.
    """
    project_context = project_context or {}
    issue_name = issue.get("name", "?")
    router.note(
        f"Coder starting: {issue_name} (iteration {iteration})",
        tags=["coder", "start"],
    )

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = coder_task_prompt(
        issue=issue,
        worktree_path=worktree_path,
        feedback=feedback,
        iteration=iteration,
        project_context=project_context,
        memory_context=memory_context,
        workspace_manifest=ws_manifest,
        target_repo=target_repo,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=CODER_SYSTEM_PROMPT,
            schema=CoderResult,
            model=model,
            provider=provider,
            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            cwd=worktree_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Coder complete: {issue_name}, "
                f"files={len(result.parsed.files_changed)}, "
                f"complete={result.parsed.complete}",
                tags=["coder", "complete"],
            )
            out = result.parsed.model_dump()
            out["iteration_id"] = iteration_id
            return out
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    project_context: dict | None = None,
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
    target_repo: str = "",
) -> dict:
    """Review and augment tests, then run the test suite.

    Returns a QAResult dict with passed, summary, test_failures, coverage_gaps.
    """
    project_context = project_context or {}
    issue_name = issue.get("name", "?")
    router.note(
        f"QA starting: {issue_name}",
        tags=["qa", "start"],
    )

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = qa_task_prompt(
        worktree_path=worktree_path,
        coder_result=coder_result,
        issue=issue,
        iteration_id=iteration_id,
        project_context=project_context,
        workspace_manifest=ws_manifest,
        target_repo=target_repo,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=QA_SYSTEM_PROMPT,
            schema=QAResult,
            model=model,
            provider=provider,
            tools=["Read", "Write", "Edit", "Bash", "Glob", "Grep"],
            cwd=worktree_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"QA complete: {issue_name}, passed={result.parsed.passed}",
                tags=["qa", "complete"],
            )
            out = result.parsed.model_dump()
            out["iteration_id"] = iteration_id
            return out
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    project_context: dict | None = None,
    qa_ran: bool = False,
    memory_context: dict | None = None,
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
    target_repo: str = "",
) -> dict:
    """Review code quality, security, and requirements adherence.

    Has BASH access to independently run tests and verify the coder's work.
    On the default path (no QA), acts as sole quality gatekeeper.
    Returns a CodeReviewResult dict with approved, blocking, summary, debt_items.
    """
    project_context = project_context or {}
    issue_name = issue.get("name", "?")
    router.note(
        f"Code reviewer starting: {issue_name}",
        tags=["code_reviewer", "start"],
    )

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = code_reviewer_task_prompt(
        worktree_path=worktree_path,
        coder_result=coder_result,
        issue=issue,
        iteration_id=iteration_id,
        project_context=project_context,
        qa_ran=qa_ran,
        memory_context=memory_context,
        workspace_manifest=ws_manifest,
        target_repo=target_repo,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=CODE_REVIEWER_SYSTEM_PROMPT,
            schema=CodeReviewResult,
            model=model,
            provider=provider,
            tools=["Read", "Write", "Glob", "Grep", "Bash"],
            cwd=worktree_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Code reviewer complete: {issue_name}, "
                f"approved={result.parsed.approved}, "
                f"blocking={result.parsed.blocking}",
                tags=["code_reviewer", "complete"],
            )
            out = result.parsed.model_dump()
            out["iteration_id"] = iteration_id
            return out
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
    issue_summary: dict | None = None,
    artifacts_dir: str = "",
    model: str = "haiku",
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
    target_repo: str = "",
) -> dict:
    """Merge QA and review feedback, decide fix/approve/block.

    Returns a QASynthesisResult dict with action, summary, stuck.
    """
    issue_summary = issue_summary or {}
    router.note(
        "QA synthesizer starting",
        tags=["qa_synthesizer", "start"],
    )

    ws_manifest = _maybe_workspace_manifest(workspace_manifest)

    task_prompt = qa_synthesizer_task_prompt(
        qa_result=qa_result,
        review_result=review_result,
        iteration_history=iteration_history,
        iteration_id=iteration_id,
        worktree_path=worktree_path,
        issue_summary=issue_summary,
        workspace_manifest=ws_manifest,
    )

    try:
        result = await router.ai(
            task_prompt,
            system=QA_SYNTHESIZER_SYSTEM_PROMPT,
            schema=QASynthesisResult,
            model=model,
        )
        if result.parsed is not None:
            router.note(
                f"QA synthesizer complete: action={result.parsed.action.value}, "
                f"stuck={result.parsed.stuck}",
                tags=["qa_synthesizer", "complete"],
            )
            out = result.parsed.model_dump()
            out["iteration_id"] = iteration_id
            return out
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
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
        fallback_summary = (
            "Synthesizer failed but QA passed and review approved — approving."
        )
    elif review_blocking:
        fallback_action = "block"
        fallback_summary = (
            "Synthesizer failed and review has blocking issues — blocking."
        )
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


# ---------------------------------------------------------------------------
# Fix generator (verification fix cycles)
# ---------------------------------------------------------------------------


@router.reasoner()
async def generate_fix_issues(
    failed_criteria: list[dict],
    dag_state: dict,
    prd: dict,
    artifacts_dir: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Generate targeted fix issues from failed verification criteria.

    Returns {fix_issues: [...], debt_items: [...], summary: str}.
    """
    router.note(
        f"Fix generator starting: {len(failed_criteria)} failed criteria",
        tags=["fix_generator", "start"],
    )

    repo_path = dag_state.get("repo_path", ".")
    task_prompt = fix_generator_task_prompt(
        failed_criteria=failed_criteria,
        dag_state_summary=dag_state,
        prd=prd,
    )

    # If multi-repo, ensure generated fix issues get target_repo set
    ws_manifest = _maybe_workspace_manifest(workspace_manifest)
    if ws_manifest and len(ws_manifest.repos) > 1:
        task_prompt += (
            "\n\n## Multi-Repo Context\n"
            "This workspace spans multiple repositories. For each fix issue you generate, "
            "include a `target_repo` field specifying which repository the fix should be "
            "applied to. Available repos:\n"
        )
        for repo in ws_manifest.repos:
            task_prompt += (
                f"- **{repo.repo_name}** (role: {repo.role}): `{repo.absolute_path}`\n"
            )

    class FixGeneratorOutput(BaseModel):
        fix_issues: list[dict] = []
        debt_items: list[dict] = []
        summary: str = ""

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=FIX_GENERATOR_SYSTEM_PROMPT,
            schema=FixGeneratorOutput,
            model=model,
            provider=provider,
            tools=["Read", "Write", "Glob", "Grep", "Bash"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Fix generator complete: {len(result.parsed.fix_issues)} fix issues, "
                f"{len(result.parsed.debt_items)} debt items",
                tags=["fix_generator", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
    except Exception as e:
        router.note(
            f"Fix generator agent failed: {e}",
            tags=["fix_generator", "error"],
        )

    # Fallback: record all as debt
    return {
        "fix_issues": [],
        "debt_items": [
            {
                "criterion": c.get("criterion", ""),
                "reason": "Fix generator failed to analyze",
                "severity": "high",
            }
            for c in failed_criteria
        ],
        "summary": "Fix generator failed — all criteria recorded as debt",
    }


# ---------------------------------------------------------------------------
# Repo finalization (post-verification cleanup)
# ---------------------------------------------------------------------------


@router.reasoner()
async def run_repo_finalize(
    repo_path: str,
    artifacts_dir: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Clean up the repository after verification — remove artifacts, fortify .gitignore.

    Returns a RepoFinalizeResult dict. Non-blocking: failure does not affect
    build success.
    """
    router.note("Repo finalize starting", tags=["repo_finalize", "start"])

    task_prompt = repo_finalize_task_prompt(repo_path=repo_path)

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=REPO_FINALIZE_SYSTEM_PROMPT,
            schema=RepoFinalizeResult,
            model=model,
            provider=provider,
            tools=["Bash", "Read", "Write", "Glob", "Grep"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"Repo finalize complete: {len(result.parsed.files_removed)} files removed, "
                f"gitignore_updated={result.parsed.gitignore_updated}",
                tags=["repo_finalize", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
    except Exception as e:
        router.note(
            f"Repo finalize agent failed: {e}",
            tags=["repo_finalize", "error"],
        )

    return RepoFinalizeResult(
        success=False,
        summary="Repo finalize agent failed to produce a valid result.",
    ).model_dump()


# ---------------------------------------------------------------------------
# GitHub PR creation (Phase B)
# ---------------------------------------------------------------------------


@router.reasoner()
async def run_github_pr(
    repo_path: str,
    integration_branch: str,
    base_branch: str,
    goal: str,
    build_summary: str = "",
    completed_issues: list[dict] | None = None,
    accumulated_debt: list[dict] | None = None,
    artifacts_dir: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Push integration branch and create a draft PR on GitHub.

    Returns a GitHubPRResult dict.
    """
    router.note(
        f"GitHub PR: pushing {integration_branch} and creating draft PR",
        tags=["github_pr", "start"],
    )

    task_prompt = github_pr_task_prompt(
        repo_path=repo_path,
        integration_branch=integration_branch,
        base_branch=base_branch,
        goal=goal,
        build_summary=build_summary,
        completed_issues=completed_issues,
        accumulated_debt=accumulated_debt,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=GITHUB_PR_SYSTEM_PROMPT,
            schema=GitHubPRResult,
            model=model,
            provider=provider,
            tools=["Bash", "Write"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"GitHub PR complete: {result.parsed.pr_url}",
                tags=["github_pr", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise  # Non-retryable — propagate immediately
    except Exception as e:
        router.note(
            f"GitHub PR agent failed: {e}",
            tags=["github_pr", "error"],
        )

    return GitHubPRResult(
        success=False,
        error_message="GitHub PR agent failed to produce a valid result.",
    ).model_dump()


# ---------------------------------------------------------------------------
# Phase C: Post-PR CI gate (watcher + fixer)
# ---------------------------------------------------------------------------


@router.reasoner()
async def run_ci_watcher(
    repo_path: str,
    pr_number: int,
    wait_seconds: int = 1500,
    poll_seconds: int = 30,
    head_sha: str = "",
) -> dict:
    """Poll `gh pr checks` until conclusive, the wait cap is hit, or no checks exist.

    Deterministic — uses the `gh` CLI and does not invoke an LLM. Returns a
    ``CIWatchResult`` dict; callers decide whether to fix-and-repush or
    surface the failure.

    When ``head_sha`` is supplied, the watcher refuses to declare a verdict
    until it has seen at least one check belonging to that SHA. Used by
    ``resolve()`` to avoid the stale-state race where the previous HEAD's
    lingering conclusive checks short-circuit the verdict before the new
    push's workflow run has registered.
    """
    router.note(
        f"CI watcher: PR #{pr_number}, wait_cap={wait_seconds}s, poll={poll_seconds}s"
        + (f", anchored to {head_sha[:10]}" if head_sha else ""),
        tags=["ci_watcher", "start"],
    )

    try:
        result = await watch_pr_checks(
            repo_path=repo_path,
            pr_number=pr_number,
            wait_seconds=wait_seconds,
            poll_seconds=poll_seconds,
            head_sha=head_sha,
        )
    except Exception as e:
        router.note(
            f"CI watcher errored: {e}",
            tags=["ci_watcher", "error"],
        )
        return CIWatchResult(
            status="error",
            pr_number=pr_number,
            summary=f"CI watcher exception: {e}",
        ).model_dump()

    router.note(
        f"CI watcher: status={result.status} ({result.summary})",
        tags=["ci_watcher", "complete", result.status],
    )
    return result.model_dump()


@router.reasoner()
async def run_ci_fixer(
    repo_path: str,
    pr_number: int,
    pr_url: str,
    integration_branch: str,
    base_branch: str,
    failed_checks: list[dict],
    iteration: int = 1,
    max_iterations: int = 2,
    goal: str = "",
    completed_issues: list[dict] | None = None,
    previous_attempts: list[dict] | None = None,
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Diagnose the failing CI checks, fix the production code, and push.

    The system prompt emphatically forbids workarounds (skipping tests,
    weakening assertions, swallowing errors, disabling jobs). The agent must
    produce a legitimate fix and push it as a new commit on
    ``integration_branch`` so the PR's checks rerun.

    Returns a ``CIFixResult`` dict.
    """
    router.note(
        f"CI fixer: PR #{pr_number}, attempt {iteration}/{max_iterations}, "
        f"{len(failed_checks)} failing check(s)",
        tags=["ci_fixer", "start"],
    )

    typed_failures = [
        fc if isinstance(fc, CIFailedCheck) else CIFailedCheck(**fc)
        for fc in failed_checks
    ]

    task_prompt = ci_fixer_task_prompt(
        repo_path=repo_path,
        pr_number=pr_number,
        pr_url=pr_url,
        integration_branch=integration_branch,
        base_branch=base_branch,
        failed_checks=typed_failures,
        iteration=iteration,
        max_iterations=max_iterations,
        goal=goal,
        completed_issues=completed_issues,
        previous_attempts=previous_attempts,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=CI_FIXER_SYSTEM_PROMPT,
            schema=CIFixResult,
            model=model,
            provider=provider,
            tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"CI fixer complete: fixed={result.parsed.fixed}, "
                f"pushed={result.parsed.pushed}, "
                f"{len(result.parsed.files_changed)} file(s) changed",
                tags=["ci_fixer", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise
    except Exception as e:
        router.note(
            f"CI fixer agent failed: {e}",
            tags=["ci_fixer", "error"],
        )

    return CIFixResult(
        fixed=False,
        summary="CI fixer agent failed to produce a valid result.",
        error_message="CI fixer agent failed to produce a valid result.",
    ).model_dump()


@router.reasoner()
async def run_pr_resolver(
    repo_path: str,
    pr_number: int,
    pr_url: str,
    head_branch: str,
    base_branch: str,
    merge_state: str = "skipped",
    conflicted_files: list[str] | None = None,
    failed_checks: list[dict] | None = None,
    review_comments: list[dict] | None = None,
    goal: str = "",
    additional_context: str = "",
    model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Resolve an open PR: complete an in-progress merge, fix CI, address comments, push.

    The agent is started with the working tree already on the PR's head
    branch. ``merge_state`` tells it whether a merge from base is in progress
    ("conflict"), was already completed ("merged"), wasn't needed ("clean"),
    or was deliberately skipped ("skipped").

    Returns a ``PRResolveResult`` dict. The orchestrator (``app.resolve``)
    consumes ``addressed_comments`` to drive the post-resolve thread-reply
    pass and uses ``pushed`` to decide whether to run the CI fix loop.
    """
    failed_checks = failed_checks or []
    review_comments = review_comments or []
    conflicted_files = conflicted_files or []

    router.note(
        f"PR resolver: PR #{pr_number}, merge_state={merge_state}, "
        f"{len(failed_checks)} failing check(s), "
        f"{len(review_comments)} review comment(s)",
        tags=["pr_resolver", "start"],
    )

    typed_failures = [
        fc if isinstance(fc, CIFailedCheck) else CIFailedCheck(**fc)
        for fc in failed_checks
    ]
    typed_comments = [
        rc if isinstance(rc, ReviewCommentRef) else ReviewCommentRef(**rc)
        for rc in review_comments
    ]

    task_prompt = pr_resolver_task_prompt(
        repo_path=repo_path,
        pr_number=pr_number,
        pr_url=pr_url,
        head_branch=head_branch,
        base_branch=base_branch,
        merge_state=merge_state,
        conflicted_files=conflicted_files,
        failed_checks=typed_failures,
        review_comments=typed_comments,
        goal=goal,
        additional_context=additional_context,
    )

    provider = "claude-code" if ai_provider == "claude" else ai_provider

    try:
        result = await router.harness(
            task_prompt,
            system_prompt=PR_RESOLVER_SYSTEM_PROMPT,
            schema=PRResolveResult,
            model=model,
            provider=provider,
            tools=["Bash", "Read", "Edit", "Write", "Glob", "Grep"],
            cwd=repo_path,
            max_turns=DEFAULT_AGENT_MAX_TURNS,
            permission_mode=permission_mode or None,
        )
        check_fatal_harness_error(result)
        if result.parsed is not None:
            router.note(
                f"PR resolver complete: fixed={result.parsed.fixed}, "
                f"pushed={result.parsed.pushed}, "
                f"merge_resolved={result.parsed.merge_resolved}, "
                f"{len(result.parsed.files_changed)} file(s) changed, "
                f"{sum(1 for c in result.parsed.addressed_comments if c.addressed)}"
                f"/{len(result.parsed.addressed_comments)} comment(s) addressed",
                tags=["pr_resolver", "complete"],
            )
            return result.parsed.model_dump()
    except FatalHarnessError:
        raise
    except Exception as e:
        router.note(
            f"PR resolver agent failed: {e}",
            tags=["pr_resolver", "error"],
        )

    return PRResolveResult(
        fixed=False,
        summary="PR resolver agent failed to produce a valid result.",
        error_message="PR resolver agent failed to produce a valid result.",
    ).model_dump()
