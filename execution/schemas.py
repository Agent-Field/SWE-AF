"""Pydantic schemas for DAG execution state and replanning."""

from __future__ import annotations

from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel

# Global default for all agent max_turns. Change this one value to adjust everywhere.
DEFAULT_AGENT_MAX_TURNS: int = 150


class AdvisorAction(str, Enum):
    """What the Issue Advisor decided to do after a coding loop failure."""

    RETRY_MODIFIED = "retry_modified"          # Relax ACs, retry coding loop
    RETRY_APPROACH = "retry_approach"          # Keep ACs, different strategy
    SPLIT = "split"                            # Break into sub-issues
    ACCEPT_WITH_DEBT = "accept_with_debt"      # Close enough, record gaps
    ESCALATE_TO_REPLAN = "escalate_to_replan"  # Flag for outer loop


class IssueOutcome(str, Enum):
    """Outcome of executing a single issue."""

    COMPLETED = "completed"
    COMPLETED_WITH_DEBT = "completed_with_debt"   # Accepted via ACCEPT_WITH_DEBT
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_UNRECOVERABLE = "failed_unrecoverable"
    FAILED_NEEDS_SPLIT = "failed_needs_split"     # Advisor wants to split
    FAILED_ESCALATED = "failed_escalated"         # Advisor escalated to replanner
    SKIPPED = "skipped"


class IssueAdaptation(BaseModel):
    """Records one AC/scope modification. Accumulated as technical debt."""

    adaptation_type: AdvisorAction
    original_acceptance_criteria: list[str] = []
    modified_acceptance_criteria: list[str] = []
    dropped_criteria: list[str] = []
    failure_diagnosis: str = ""
    rationale: str = ""
    new_approach: str = ""
    missing_functionality: list[str] = []
    downstream_impact: str = ""
    severity: str = "medium"


class SplitIssueSpec(BaseModel):
    """Sub-issue spec when advisor decides to SPLIT."""

    name: str
    title: str
    description: str
    acceptance_criteria: list[str]
    depends_on: list[str] = []
    provides: list[str] = []
    files_to_create: list[str] = []
    files_to_modify: list[str] = []
    parent_issue_name: str = ""


class IssueAdvisorDecision(BaseModel):
    """Structured output from the Issue Advisor agent."""

    action: AdvisorAction
    failure_diagnosis: str
    failure_category: str = ""   # environment|logic|dependency|approach|scope
    rationale: str
    confidence: float = 0.5
    # RETRY_MODIFIED
    modified_acceptance_criteria: list[str] = []
    dropped_criteria: list[str] = []
    modification_justification: str = ""
    # RETRY_APPROACH
    new_approach: str = ""
    approach_changes: list[str] = []
    # SPLIT
    sub_issues: list[SplitIssueSpec] = []
    split_rationale: str = ""
    # ACCEPT_WITH_DEBT
    missing_functionality: list[str] = []
    debt_severity: str = "medium"
    # ESCALATE_TO_REPLAN
    escalation_reason: str = ""
    dag_impact: str = ""
    suggested_restructuring: str = ""
    # Always
    downstream_impact: str = ""
    summary: str = ""


class IssueResult(BaseModel):
    """Result of executing a single issue."""

    issue_name: str
    outcome: IssueOutcome
    result_summary: str = ""
    error_message: str = ""
    error_context: str = ""  # traceback/logs for replanner
    attempts: int = 1
    files_changed: list[str] = []
    branch_name: str = ""
    # Advisor fields
    advisor_invocations: int = 0
    adaptations: list[IssueAdaptation] = []
    debt_items: list[dict] = []
    split_request: list[SplitIssueSpec] | None = None
    escalation_context: str = ""
    final_acceptance_criteria: list[str] = []
    iteration_history: list[dict] = []


class LevelResult(BaseModel):
    """Aggregated result of executing all issues in a single level."""

    level_index: int
    completed: list[IssueResult] = []
    failed: list[IssueResult] = []
    skipped: list[IssueResult] = []


class ReplanAction(str, Enum):
    """What the replanner decided to do."""

    CONTINUE = "continue"  # proceed unchanged
    MODIFY_DAG = "modify_dag"  # restructured
    REDUCE_SCOPE = "reduce_scope"  # dropped non-essential issues
    ABORT = "abort"  # cannot recover


class ReplanDecision(BaseModel):
    """Structured output from the replanner agent."""

    action: ReplanAction
    rationale: str
    updated_issues: list[dict] = []  # modified remaining issues
    removed_issue_names: list[str] = []
    skipped_issue_names: list[str] = []
    new_issues: list[dict] = []
    summary: str = ""


class DAGState(BaseModel):
    """Full execution state of the DAG — passed to replanner for context."""

    # --- Artifact paths (so any agent can read the full context) ---
    repo_path: str = ""
    artifacts_dir: str = ""
    prd_path: str = ""
    architecture_path: str = ""
    issues_dir: str = ""

    # --- Plan context (summaries for quick reference by replanner) ---
    original_plan_summary: str = ""
    prd_summary: str = ""
    architecture_summary: str = ""

    # --- Issue tracking ---
    all_issues: list[dict] = []  # full PlannedIssue dicts
    levels: list[list[str]] = []  # parallel execution levels

    # --- Execution progress ---
    completed_issues: list[IssueResult] = []
    failed_issues: list[IssueResult] = []
    skipped_issues: list[str] = []
    in_flight_issues: list[str] = []  # names of issues currently executing
    current_level: int = 0

    # --- Replan tracking ---
    replan_count: int = 0
    replan_history: list[ReplanDecision] = []
    max_replans: int = 2

    # --- Git branch tracking ---
    git_integration_branch: str = ""
    git_original_branch: str = ""
    git_initial_commit: str = ""
    git_mode: str = ""  # "fresh" or "existing"
    pending_merge_branches: list[str] = []
    merged_branches: list[str] = []
    unmerged_branches: list[str] = []  # branches that failed to merge
    worktrees_dir: str = ""  # e.g. repo_path/.worktrees

    # --- Merge/test history ---
    merge_results: list[dict] = []
    integration_test_results: list[dict] = []

    # --- Debt tracking ---
    accumulated_debt: list[dict] = []
    adaptation_history: list[dict] = []


class GitInitResult(BaseModel):
    """Result of git initialization."""

    mode: str  # "fresh" or "existing"
    original_branch: str  # "" for fresh, e.g. "main" for existing
    integration_branch: str  # branch where merged work accumulates
    initial_commit_sha: str  # commit SHA before any work
    success: bool
    error_message: str = ""
    remote_url: str = ""            # origin URL (set if repo was cloned)
    remote_default_branch: str = "" # e.g. "main" — for PR base


class WorkspaceInfo(BaseModel):
    """Info about a worktree created for an issue."""

    issue_name: str
    branch_name: str
    worktree_path: str


class MergeResult(BaseModel):
    """Structured output from the merger agent."""

    success: bool
    merged_branches: list[str]
    failed_branches: list[str]
    conflict_resolutions: list[dict] = []  # [{file, branches, resolution_strategy}]
    merge_commit_sha: str = ""
    pre_merge_sha: str = ""  # for potential rollback
    needs_integration_test: bool
    integration_test_rationale: str = ""
    summary: str


class IntegrationTestResult(BaseModel):
    """Result of integration testing after a merge."""

    passed: bool
    tests_written: list[str] = []  # test file paths
    tests_run: int
    tests_passed: int
    tests_failed: int
    failure_details: list[dict] = []  # [{test_name, error, file}]
    summary: str


class RetryAdvice(BaseModel):
    """Structured output from the retry advisor agent."""

    should_retry: bool
    diagnosis: str  # Root cause analysis
    strategy: str  # What to do differently
    modified_context: str  # Additional guidance to inject into retry
    confidence: float = 0.5  # 0.0-1.0


class CriterionResult(BaseModel):
    """Verification result for a single acceptance criterion."""

    criterion: str
    passed: bool
    evidence: str  # What the verifier found
    issue_name: str = ""  # Which issue was responsible


class VerificationResult(BaseModel):
    """Structured output from the verifier agent."""

    passed: bool
    criteria_results: list[CriterionResult]
    summary: str
    suggested_fixes: list[str] = []


# ---------------------------------------------------------------------------
# Phase 4: Coding loop schemas
# ---------------------------------------------------------------------------


class CoderResult(BaseModel):
    """Output from the coder agent."""

    files_changed: list[str] = []
    summary: str = ""
    complete: bool = True
    iteration_id: str = ""
    tests_passed: bool | None = None       # Self-reported: did tests pass?
    test_summary: str = ""                 # Brief test run output
    codebase_learnings: list[str] = []     # Conventions discovered (for shared memory)
    agent_retro: dict = {}                 # What worked, what didn't (for shared memory)


class QAResult(BaseModel):
    """Output from the QA/tester agent."""

    passed: bool
    summary: str = ""
    test_failures: list[dict] = []  # [{test_name, file, error, expected, actual}]
    coverage_gaps: list[str] = []   # ACs without test coverage
    iteration_id: str = ""


class CodeReviewResult(BaseModel):
    """Output from the code reviewer agent."""

    approved: bool
    summary: str = ""
    blocking: bool = False  # True ONLY for security/crash/data-loss
    debt_items: list[dict[str, Any]] = []  # [{severity, title, file_path, description}]
    iteration_id: str = ""


class QASynthesisAction(str, Enum):
    """Decision from the feedback synthesizer."""

    FIX = "fix"
    APPROVE = "approve"
    BLOCK = "block"


class QASynthesisResult(BaseModel):
    """Output from the feedback synthesizer agent."""

    action: QASynthesisAction
    summary: str = ""
    stuck: bool = False
    iteration_id: str = ""


# ---------------------------------------------------------------------------
# Model configuration: role groups, presets, layered resolution
# ---------------------------------------------------------------------------

ROLE_GROUPS: dict[str, list[str]] = {
    "planning": ["pm_model", "architect_model", "tech_lead_model", "sprint_planner_model"],
    "coding": ["coder_model", "qa_model", "code_reviewer_model"],
    "orchestration": [
        "replan_model", "retry_advisor_model", "issue_writer_model",
        "verifier_model", "git_model", "merger_model",
        "integration_tester_model", "issue_advisor_model",
    ],
    "lightweight": ["qa_synthesizer_model"],
}

ALL_MODEL_FIELDS: list[str] = [f for fields in ROLE_GROUPS.values() for f in fields]

MODEL_PRESETS: dict[str, dict[str, str]] = {
    "turbo": {"planning": "haiku", "coding": "haiku", "orchestration": "haiku", "lightweight": "haiku"},
    "fast": {"planning": "sonnet", "coding": "sonnet", "orchestration": "haiku", "lightweight": "haiku"},
    "balanced": {"planning": "sonnet", "coding": "sonnet", "orchestration": "sonnet", "lightweight": "haiku"},
    "thorough": {"planning": "sonnet", "coding": "sonnet", "orchestration": "sonnet", "lightweight": "sonnet"},
    "quality": {"planning": "opus", "coding": "opus", "orchestration": "sonnet", "lightweight": "haiku"},
}

# Reverse lookup: field name → group name
_FIELD_TO_GROUP: dict[str, str] = {
    field: group for group, fields in ROLE_GROUPS.items() for field in fields
}


def resolve_models(
    *,
    preset: str | None,
    models: dict[str, str] | None,
    explicit_fields: dict[str, str],
    field_names: list[str] | None = None,
) -> dict[str, str]:
    """Layered model resolution: defaults < preset < role groups < individual fields.

    Args:
        preset: Named preset (e.g. "quality", "turbo"). Applied first.
        models: Role-group overrides (e.g. {"planning": "opus"}). Applied second.
        explicit_fields: Individual *_model field overrides the user actually set.
            Only keys present here are treated as explicit overrides.
        field_names: Which model fields to resolve. Defaults to ALL_MODEL_FIELDS.

    Returns:
        Dict mapping each field name to its resolved model string.

    Raises:
        ValueError: If preset name or group name is unknown.
    """
    if field_names is None:
        field_names = ALL_MODEL_FIELDS

    # 1. Start from "balanced" defaults
    balanced = MODEL_PRESETS["balanced"]
    result: dict[str, str] = {}
    for field in field_names:
        group = _FIELD_TO_GROUP.get(field)
        result[field] = balanced[group] if group else "sonnet"

    # 2. Apply preset
    if preset is not None:
        if preset not in MODEL_PRESETS:
            raise ValueError(
                f"Unknown preset {preset!r}. Valid presets: {', '.join(MODEL_PRESETS)}"
            )
        preset_map = MODEL_PRESETS[preset]
        for field in field_names:
            group = _FIELD_TO_GROUP.get(field)
            if group and group in preset_map:
                result[field] = preset_map[group]

    # 3. Apply role-group overrides
    if models:
        for group_name, model_value in models.items():
            if group_name not in ROLE_GROUPS:
                raise ValueError(
                    f"Unknown model group {group_name!r}. "
                    f"Valid groups: {', '.join(ROLE_GROUPS)}"
                )
            for field in ROLE_GROUPS[group_name]:
                if field in result:
                    result[field] = model_value

    # 4. Apply individual field overrides (highest priority)
    for field, value in explicit_fields.items():
        if field in result:
            result[field] = value

    return result


class BuildConfig(BaseModel):
    """Configuration for the end-to-end build pipeline."""

    # --- Model presets & groups ---
    preset: str | None = None
    models: dict[str, str] | None = None

    # Planning
    pm_model: str = "sonnet"
    architect_model: str = "sonnet"
    tech_lead_model: str = "sonnet"
    sprint_planner_model: str = "sonnet"
    max_review_iterations: int = 2
    # Execution
    max_retries_per_issue: int = 2
    max_replans: int = 2
    enable_replanning: bool = True
    replan_model: str = "sonnet"
    retry_advisor_model: str = "sonnet"
    issue_writer_model: str = "sonnet"
    # Verification
    verifier_model: str = "sonnet"
    max_verify_fix_cycles: int = 1
    # Git / Merge
    git_model: str = "sonnet"
    merger_model: str = "sonnet"
    integration_tester_model: str = "sonnet"
    max_integration_test_retries: int = 1
    enable_integration_testing: bool = True
    # Coding loop
    max_coding_iterations: int = 5
    coder_model: str = "sonnet"
    qa_model: str = "sonnet"
    code_reviewer_model: str = "sonnet"
    qa_synthesizer_model: str = "haiku"
    # Agent limits
    agent_max_turns: int = DEFAULT_AGENT_MAX_TURNS
    # Target
    execute_fn_target: str = ""
    ai_provider: Literal["claude", "codex"] = "claude"
    permission_mode: str = ""
    # GitHub workflow
    repo_url: str = ""                # GitHub URL to clone
    enable_github_pr: bool = True     # Create draft PR after build
    github_pr_base: str = ""          # PR base branch (default: repo's default branch)
    # Issue Advisor
    agent_timeout_seconds: int = 2700
    issue_advisor_model: str = "sonnet"
    max_advisor_invocations: int = 2
    enable_issue_advisor: bool = True

    def resolved_models(self) -> dict[str, str]:
        """Resolve all model fields using layered precedence.

        Returns a dict of {field_name: resolved_model} for all 16 model fields.
        Individual ``*_model`` fields only act as overrides if explicitly set by the
        caller (detected via Pydantic's ``model_fields_set``).
        """
        explicit = {
            f: getattr(self, f)
            for f in ALL_MODEL_FIELDS
            if f in self.model_fields_set
        }
        return resolve_models(
            preset=self.preset,
            models=self.models,
            explicit_fields=explicit,
        )

    def to_execution_config_dict(self) -> dict:
        """Build the dict that gets passed to ``ExecutionConfig`` via ``execute()``.

        Resolves models through the layered system and merges with non-model config.
        """
        resolved = self.resolved_models()
        # Only include model fields that ExecutionConfig actually has
        _EXEC_ONLY = {
            "replan_model", "retry_advisor_model", "issue_writer_model",
            "merger_model", "integration_tester_model",
            "coder_model", "qa_model", "code_reviewer_model",
            "qa_synthesizer_model", "issue_advisor_model",
        }
        return {
            **{f: resolved[f] for f in ALL_MODEL_FIELDS if f in _EXEC_ONLY},
            "max_retries_per_issue": self.max_retries_per_issue,
            "max_replans": self.max_replans,
            "enable_replanning": self.enable_replanning,
            "max_integration_test_retries": self.max_integration_test_retries,
            "enable_integration_testing": self.enable_integration_testing,
            "max_coding_iterations": self.max_coding_iterations,
            "agent_max_turns": self.agent_max_turns,
            "agent_timeout_seconds": self.agent_timeout_seconds,
            "max_advisor_invocations": self.max_advisor_invocations,
            "enable_issue_advisor": self.enable_issue_advisor,
        }


class BuildResult(BaseModel):
    """Final output of the end-to-end build pipeline."""

    plan_result: dict
    dag_state: dict
    verification: dict | None = None
    success: bool
    summary: str
    pr_url: str = ""


class RepoFinalizeResult(BaseModel):
    """Result of the repo finalization (cleanup) step."""

    success: bool
    files_removed: list[str] = []
    gitignore_updated: bool = False
    summary: str = ""


class GitHubPRResult(BaseModel):
    """Result of pushing and creating a draft PR on GitHub."""

    success: bool
    pr_url: str = ""
    pr_number: int = 0
    error_message: str = ""


class ExecutionConfig(BaseModel):
    """Configuration for the DAG executor."""

    # --- Model presets & groups ---
    preset: str | None = None
    models: dict[str, str] | None = None

    max_retries_per_issue: int = 1
    max_replans: int = 2
    replan_model: str = "sonnet"
    enable_replanning: bool = True
    retry_advisor_model: str = "sonnet"
    issue_writer_model: str = "sonnet"
    merger_model: str = "sonnet"
    integration_tester_model: str = "sonnet"
    max_integration_test_retries: int = 1
    enable_integration_testing: bool = True
    # Coding loop
    max_coding_iterations: int = 5
    coder_model: str = "sonnet"
    qa_model: str = "sonnet"
    code_reviewer_model: str = "sonnet"
    qa_synthesizer_model: str = "haiku"
    # Agent limits
    agent_max_turns: int = DEFAULT_AGENT_MAX_TURNS
    ai_provider: Literal["claude", "codex"] = "claude"
    # Issue Advisor
    agent_timeout_seconds: int = 2700       # 45 min
    issue_advisor_model: str = "sonnet"
    max_advisor_invocations: int = 2
    enable_issue_advisor: bool = True

    def model_post_init(self, __context: Any) -> None:
        """Apply layered model resolution after construction.

        If ``preset`` or ``models`` is set, resolve and write back to the
        individual ``*_model`` fields so downstream code (dag_executor,
        coding_loop) can keep reading ``config.coder_model`` etc. unchanged.
        """
        if self.preset is None and self.models is None:
            return
        # Fields present on ExecutionConfig (use class-level access)
        exec_model_fields = [f for f in ALL_MODEL_FIELDS if f in type(self).model_fields]
        explicit = {
            f: getattr(self, f)
            for f in exec_model_fields
            if f in self.model_fields_set and f not in ("preset", "models")
        }
        resolved = resolve_models(
            preset=self.preset,
            models=self.models,
            explicit_fields=explicit,
            field_names=exec_model_fields,
        )
        for field, value in resolved.items():
            object.__setattr__(self, field, value)
