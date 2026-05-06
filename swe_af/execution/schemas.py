"""Pydantic schemas for DAG execution state and replanning."""

from __future__ import annotations

import logging
import os
import re
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PrivateAttr,
    field_validator,
    model_validator,
)

# Global default for all agent max_turns. Change this one value to adjust everywhere.
DEFAULT_AGENT_MAX_TURNS: int = 150


# ---------------------------------------------------------------------------
# Provider normalization
# ---------------------------------------------------------------------------


def _normalize_provider(ai_provider: str) -> str:
    """Map legacy provider names to AgentField native names.

    Ensures backward compatibility between old "claude" provider name
    and AgentField's native "claude-code" provider name.
    """
    return {"claude": "claude-code"}.get(ai_provider, ai_provider)


# ---------------------------------------------------------------------------
# Multi-repo helper
# ---------------------------------------------------------------------------


def _derive_repo_name(url: str) -> str:
    """Extract repo name from a git URL.

    Examples:
        'https://github.com/org/my-project.git' -> 'my-project'
        'git@github.com:org/repo.git'           -> 'repo'
        'https://github.com/org/repo'           -> 'repo'
    """
    if not url:
        return ""
    # Strip trailing .git, then take last path component
    stripped = re.sub(r"\.git$", "", url.rstrip("/"))
    # Handle both HTTPS and SSH URLs
    name = re.split(r"[/:]", stripped)[-1]
    return name


# ---------------------------------------------------------------------------
# Multi-repo models
# ---------------------------------------------------------------------------


class RepoSpec(BaseModel):
    """Specification for a single repository in a multi-repo build."""

    repo_url: str = ""  # GitHub/git URL (required if repo_path empty)
    repo_path: str = ""  # Absolute path to an existing local repo
    role: str  # 'primary' or 'dependency'
    branch: str = ""  # Branch to checkout (empty = default branch)
    sparse_paths: list[str] = []  # For sparse checkout; empty = full checkout
    mount_point: str = ""  # Workspace subdirectory override
    create_pr: bool = True  # Whether to create a PR for this repo

    @field_validator("role")
    @classmethod
    def _validate_role(cls, v: str) -> str:
        if v not in ("primary", "dependency"):
            raise ValueError(f"role must be 'primary' or 'dependency', got {v!r}")
        return v

    @field_validator("repo_url")
    @classmethod
    def _validate_repo_url(cls, v: str) -> str:
        if v and not (
            v.startswith("http://") or v.startswith("https://") or v.startswith("git@")
        ):
            raise ValueError(f"repo_url must be an HTTP(S) or SSH git URL, got {v!r}")
        return v


class WorkspaceRepo(BaseModel):
    """A repository that has been cloned into the workspace."""

    model_config = ConfigDict(
        frozen=False
    )  # Mutable: git_init_result assigned post-clone

    repo_name: str  # Derived name (from _derive_repo_name)
    repo_url: str  # Original git URL
    role: str  # 'primary' or 'dependency'
    absolute_path: str  # Path where the repo was cloned
    branch: str  # Actual checked-out branch
    sparse_paths: list[str] = []
    create_pr: bool = True
    git_init_result: dict | None = None  # Populated by _init_all_repos after cloning


class WorkspaceManifest(BaseModel):
    """Snapshot of all repositories cloned for a multi-repo build."""

    workspace_root: str  # Parent directory containing all repos
    repos: list[WorkspaceRepo]  # All cloned repos
    primary_repo_name: str  # Name of the primary repo

    @property
    def primary_repo(self) -> WorkspaceRepo | None:
        """Return the primary WorkspaceRepo, or None if not found."""
        for repo in self.repos:
            if repo.repo_name == self.primary_repo_name:
                return repo
        return None


class RepoPRResult(BaseModel):
    """Result of creating a PR for a single repository."""

    repo_name: str
    repo_url: str
    success: bool
    pr_url: str = ""
    pr_number: int = 0
    error_message: str = ""


class AdvisorAction(str, Enum):
    """What the Issue Advisor decided to do after a coding loop failure."""

    RETRY_MODIFIED = "retry_modified"  # Relax ACs, retry coding loop
    RETRY_APPROACH = "retry_approach"  # Keep ACs, different strategy
    SPLIT = "split"  # Break into sub-issues
    ACCEPT_WITH_DEBT = "accept_with_debt"  # Close enough, record gaps
    ESCALATE_TO_REPLAN = "escalate_to_replan"  # Flag for outer loop


class IssueOutcome(str, Enum):
    """Outcome of executing a single issue."""

    COMPLETED = "completed"
    COMPLETED_WITH_DEBT = "completed_with_debt"  # Accepted via ACCEPT_WITH_DEBT
    FAILED_RETRYABLE = "failed_retryable"
    FAILED_UNRECOVERABLE = "failed_unrecoverable"
    FAILED_NEEDS_SPLIT = "failed_needs_split"  # Advisor wants to split
    FAILED_ESCALATED = "failed_escalated"  # Advisor escalated to replanner
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
    failure_category: str = ""  # environment|logic|dependency|approach|scope
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
    repo_name: str = ""  # Repo where this issue was coded (propagated from CoderResult)
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
    build_id: str = ""  # unique per build() call; namespaces git branches/worktrees

    # --- Merge/test history ---
    merge_results: list[dict] = []
    integration_test_results: list[dict] = []

    # --- Debt tracking ---
    accumulated_debt: list[dict] = []
    adaptation_history: list[dict] = []

    # --- Multi-repo workspace ---
    workspace_manifest: dict | None = (
        None  # Serialised WorkspaceManifest (dict for JSON compat)
    )


class GitInitResult(BaseModel):
    """Result of git initialization."""

    mode: str  # "fresh" or "existing"
    original_branch: str  # "" for fresh, e.g. "main" for existing
    integration_branch: str  # branch where merged work accumulates
    initial_commit_sha: str  # commit SHA before any work
    success: bool
    error_message: str = ""
    remote_url: str = ""  # origin URL (set if repo was cloned)
    remote_default_branch: str = ""  # e.g. "main" — for PR base
    repo_name: str = ""  # Repo this result belongs to (multi-repo)


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
    repo_name: str = ""  # Repo where this merge ran (multi-repo)


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
    tests_passed: bool | None = None  # Self-reported: did tests pass?
    test_summary: str = ""  # Brief test run output
    codebase_learnings: list[str] = []  # Conventions discovered (for shared memory)
    agent_retro: dict = {}  # What worked, what didn't (for shared memory)
    repo_name: str = ""  # Repo where coder ran (multi-repo)


class QAResult(BaseModel):
    """Output from the QA/tester agent."""

    passed: bool
    summary: str = ""
    test_failures: list[dict] = []  # [{test_name, file, error, expected, actual}]
    coverage_gaps: list[str] = []  # ACs without test coverage
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
# Model configuration: runtime + flat role map
# ---------------------------------------------------------------------------

RUNTIME_VALUES: tuple[str, str] = ("claude_code", "open_code")

ROLE_TO_MODEL_FIELD: dict[str, str] = {
    "pm": "pm_model",
    "architect": "architect_model",
    "tech_lead": "tech_lead_model",
    "sprint_planner": "sprint_planner_model",
    "coder": "coder_model",
    "qa": "qa_model",
    "code_reviewer": "code_reviewer_model",
    "qa_synthesizer": "qa_synthesizer_model",
    "replan": "replan_model",
    "retry_advisor": "retry_advisor_model",
    "issue_writer": "issue_writer_model",
    "issue_advisor": "issue_advisor_model",
    "verifier": "verifier_model",
    "git": "git_model",
    "merger": "merger_model",
    "integration_tester": "integration_tester_model",
    "ci_fixer": "ci_fixer_model",
}

MODEL_ROLE_KEYS: list[str] = list(ROLE_TO_MODEL_FIELD)
ALL_MODEL_FIELDS: list[str] = list(ROLE_TO_MODEL_FIELD.values())
_MODEL_FIELD_TO_ROLE: dict[str, str] = {
    model_field: role for role, model_field in ROLE_TO_MODEL_FIELD.items()
}
_ALLOWED_MODEL_KEYS: set[str] = set(MODEL_ROLE_KEYS) | {"default"}

_LEGACY_GROUP_EQUIVALENTS: dict[str, str] = {
    "planning": "models.pm, models.architect, models.tech_lead, models.sprint_planner",
    "coding": "models.coder, models.qa, models.code_reviewer",
    "orchestration": "models.replan, models.retry_advisor, models.issue_writer, models.issue_advisor, models.verifier, models.git, models.merger, models.integration_tester",
    "lightweight": "models.qa_synthesizer",
}

_LEGACY_TOP_LEVEL_EQUIVALENTS: dict[str, str] = {
    "ai_provider": "runtime",
    "preset": "runtime + models",
    "model": "models.default",
    **{field: f"models.{role}" for field, role in _MODEL_FIELD_TO_ROLE.items()},
}

_RUNTIME_BASE_MODELS: dict[str, dict[str, str]] = {
    "claude_code": {
        **{field: "sonnet" for field in ALL_MODEL_FIELDS},
        "qa_synthesizer_model": "haiku",
    },
    "open_code": {
        **{field: "openrouter/minimax/minimax-m2.5" for field in ALL_MODEL_FIELDS},
    },
}


def _runtime_to_provider(runtime: str) -> Literal["claude", "opencode"]:
    if runtime == "claude_code":
        return "claude"
    if runtime == "open_code":
        return "opencode"
    raise ValueError(
        f"Unsupported runtime {runtime!r}. Valid runtimes: {', '.join(RUNTIME_VALUES)}"
    )


def _default_runtime() -> Literal["claude_code", "open_code"]:
    """Default runtime, honoring the ``SWE_DEFAULT_RUNTIME`` env var.

    Lets the deployer pick the runtime without every caller having to pass
    a config. Falls back to ``claude_code`` when unset; logs and falls back
    when the env value isn't a valid runtime.
    """
    value = os.getenv("SWE_DEFAULT_RUNTIME", "").strip()
    if not value:
        return "claude_code"
    if value in RUNTIME_VALUES:
        return value  # type: ignore[return-value]
    logging.getLogger(__name__).warning(
        "SWE_DEFAULT_RUNTIME=%r is not one of %s; falling back to claude_code",
        value,
        RUNTIME_VALUES,
    )
    return "claude_code"


_DEFAULT_MODEL_ENV_VARS: tuple[str, ...] = (
    "SWE_DEFAULT_MODEL",
    "AI_MODEL",
    "HARNESS_MODEL",
)


def _default_model_from_env() -> str | None:
    """Pick a single model id from deployer env vars.

    Cascades through the well-known env-var names this stack uses for model
    selection so the same Railway / docker-compose variable that points
    pr-af and github-buddy at a model also applies here, without needing
    a SWE-AF-specific name. First non-empty value wins:

        SWE_DEFAULT_MODEL  →  AI_MODEL  →  HARNESS_MODEL

    Caller-supplied ``models={"default": …}`` and per-role overrides still
    beat the env value (see ``resolve_runtime_models`` precedence). All
    unset / empty → ``None``, which means "use the runtime base defaults".
    """
    for var in _DEFAULT_MODEL_ENV_VARS:
        value = os.getenv(var, "").strip()
        if value:
            return value
    return None


def _legacy_hint_for_model_key(key: str) -> str:
    if key in _LEGACY_GROUP_EQUIVALENTS:
        return _LEGACY_GROUP_EQUIVALENTS[key]
    role = _MODEL_FIELD_TO_ROLE.get(key)
    if role:
        return f"models.{role}"
    if key.endswith("_model"):
        return f"models.{key[:-6]}"
    return "models.<role>"


def _reject_legacy_config_keys(data: Any) -> Any:
    if not isinstance(data, dict):
        return data

    legacy_hits: list[str] = []
    for key, equivalent in _LEGACY_TOP_LEVEL_EQUIVALENTS.items():
        if key in data:
            legacy_hits.append(f"{key!r} -> {equivalent!r}")

    models_value = data.get("models")
    if isinstance(models_value, dict):
        for model_key in models_value:
            if model_key in _LEGACY_GROUP_EQUIVALENTS:
                hint = _legacy_hint_for_model_key(model_key)
                raise ValueError(
                    f"Legacy model group key {model_key!r} is not supported in V2. "
                    f"Use flat role keys: {hint}."
                )
            if model_key in _MODEL_FIELD_TO_ROLE or model_key.endswith("_model"):
                hint = _legacy_hint_for_model_key(model_key)
                raise ValueError(
                    f"Legacy model key {model_key!r} is not supported in V2. "
                    f"Use {hint!r}."
                )

    if legacy_hits:
        raise ValueError(
            "Legacy config keys are not supported in V2: "
            + ", ".join(legacy_hits)
            + "."
        )
    return data


def _validate_flat_models(models: dict[str, str] | None) -> dict[str, str]:
    if models is None:
        return {}
    if not isinstance(models, dict):
        raise ValueError("models must be an object mapping role keys to model strings")

    unknown = sorted(k for k in models if k not in _ALLOWED_MODEL_KEYS)
    if unknown:
        raise ValueError(
            f"Unknown model keys: {', '.join(repr(k) for k in unknown)}. "
            f"Valid keys: {', '.join(sorted(_ALLOWED_MODEL_KEYS))}"
        )
    return models


def resolve_runtime_models(
    *,
    runtime: str,
    models: dict[str, str] | None,
    field_names: list[str] | None = None,
) -> dict[str, str]:
    """Resolve internal ``*_model`` fields from runtime + flat role overrides.

    Resolution order (lowest → highest precedence):
        1. runtime base defaults (``_RUNTIME_BASE_MODELS[runtime]``)
        2. env-var cascade: ``SWE_DEFAULT_MODEL`` → ``AI_MODEL`` →
           ``HARNESS_MODEL`` (first non-empty wins, applies to all roles)
        3. caller's ``models["default"]``
        4. caller's ``models["<role>"]``
    """
    if field_names is None:
        field_names = ALL_MODEL_FIELDS

    if runtime not in _RUNTIME_BASE_MODELS:
        raise ValueError(
            f"Unsupported runtime {runtime!r}. Valid runtimes: {', '.join(RUNTIME_VALUES)}"
        )

    flat_models = _validate_flat_models(models)

    base = _RUNTIME_BASE_MODELS[runtime]
    resolved: dict[str, str] = {field: base[field] for field in field_names}

    env_default = _default_model_from_env()
    if env_default:
        for field in field_names:
            resolved[field] = env_default

    default_model = flat_models.get("default")
    if default_model:
        for field in field_names:
            resolved[field] = default_model

    for role, model_name in flat_models.items():
        if role == "default":
            continue
        field = ROLE_TO_MODEL_FIELD[role]
        if field in resolved:
            resolved[field] = model_name

    return resolved


class BuildConfig(BaseModel):
    """Configuration for the end-to-end build pipeline."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["claude_code", "open_code"] = Field(default_factory=_default_runtime)
    models: dict[str, str] | None = None

    max_review_iterations: int = 2
    max_retries_per_issue: int = 2
    max_replans: int = 2
    enable_replanning: bool = True
    max_verify_fix_cycles: int = 1
    git_init_max_retries: int = 3  # Number of retry attempts for git_init
    git_init_retry_delay: float = 1.0  # Seconds to wait between retries
    max_integration_test_retries: int = 1
    enable_integration_testing: bool = True
    max_coding_iterations: int = 5
    agent_max_turns: int = DEFAULT_AGENT_MAX_TURNS
    execute_fn_target: str = ""
    permission_mode: str = ""
    repo_url: str = ""  # GitHub URL to clone (single-repo shorthand)
    repos: list[RepoSpec] = []  # Multi-repo list; normalised by _normalize_repos
    enable_github_pr: bool = True  # Create PR (ready for review) after build
    github_pr_base: str = ""  # PR base branch (default: repo's default branch)
    # Post-PR CI gate. When True, after the PR is opened SWE-AF waits for CI
    # to be conclusive and runs a bounded fix-and-repush loop on failure.
    # PRs are opened ready for review (no draft phase), so this gate does
    # NOT toggle a draft → ready promotion — passing CI is success, failing
    # CI leaves the PR open with visible failing checks. When False, the
    # build returns immediately after creating the PR.
    check_ci: bool = True
    max_ci_fix_cycles: int = 2  # number of fix → repush → re-watch iterations
    ci_wait_seconds: int = 1500  # wall-clock cap per watch (25 min)
    ci_poll_seconds: int = 30  # poll interval for `gh pr checks --json`
    # Wall-clock grace period to wait between `git push` and the first CI
    # poll. GitHub Actions takes a few seconds to register a new workflow
    # run after a push lands; without this grace, the first poll can race
    # the registration and either return empty or — worse — return the
    # PREVIOUS HEAD's lingering conclusive check states, causing the watcher
    # to short-circuit with the wrong verdict. 30s comfortably covers the
    # observed registration lag (~5–25s on this stack) without meaningfully
    # extending overall gate runtime. Used by `resolve()`; `build()` doesn't
    # need this because it creates a fresh PR with no prior check history.
    ci_startup_grace_seconds: int = 30
    agent_timeout_seconds: int = 2700
    max_advisor_invocations: int = 2
    enable_issue_advisor: bool = True
    enable_learning: bool = (
        False  # Cross-issue shared memory (conventions, failure patterns, bug patterns)
    )
    max_concurrent_issues: int = 3  # max parallel issues per level (0 = unlimited)
    level_failure_abort_threshold: float = (
        0.8  # abort DAG when >= this fraction of a level fails
    )

    @model_validator(mode="before")
    @classmethod
    def _validate_v2_keys(cls, data: Any) -> Any:
        return _reject_legacy_config_keys(data)

    @model_validator(mode="after")
    def _normalize_repos(self) -> "BuildConfig":
        """Normalise the repos list and enforce invariants.

        Steps:
        1. Mutual exclusion: repo_url + repos simultaneously → error.
        2. If only repo_url given, synthesise a single primary RepoSpec.
        3. If repos is empty and repo_url is empty, pass through (deferred).
        4. Exactly one primary repo required.
        5. No duplicate repo_url values.
        6. Backfill self.repo_url from primary if it was empty.
        """
        repo_url = self.repo_url
        repos = self.repos

        # Step 1: Mutual exclusion
        if repo_url and repos:
            raise ValueError(
                "Specify either 'repo_url' (single-repo shorthand) or 'repos' "
                "(multi-repo list), not both."
            )

        # Step 2: Synthesise from repo_url
        if repo_url and not repos:
            self.repos = [RepoSpec(repo_url=repo_url, role="primary")]
            return self

        # Step 3: Empty passthrough
        if not repos:
            return self

        # Step 4: Exactly one primary
        primaries = [r for r in repos if r.role == "primary"]
        if len(primaries) != 1:
            raise ValueError(
                f"Exactly one RepoSpec with role='primary' is required; "
                f"found {len(primaries)}."
            )

        # Step 5: No duplicate repo_url values
        urls = [r.repo_url for r in repos if r.repo_url]
        if len(urls) != len(set(urls)):
            raise ValueError("Duplicate repo_url values are not allowed in 'repos'.")

        # Step 6: Backfill repo_url from primary
        if not self.repo_url:
            self.repo_url = primaries[0].repo_url

        return self

    def model_post_init(self, __context: Any) -> None:
        _validate_flat_models(self.models)

    @property
    def ai_provider(self) -> Literal["claude", "opencode"]:
        return _runtime_to_provider(self.runtime)

    @property
    def primary_repo(self) -> RepoSpec | None:
        """Return the primary RepoSpec, or None if repos is empty."""
        for r in self.repos:
            if r.role == "primary":
                return r
        return None

    def resolved_models(self) -> dict[str, str]:
        """Resolve all internal ``*_model`` fields from V2 runtime config."""
        return resolve_runtime_models(
            runtime=self.runtime,
            models=self.models,
        )

    def to_execution_config_dict(self) -> dict:
        """Build the dict that gets passed to ``ExecutionConfig`` via ``execute()``.

        Carries forward runtime model selection plus non-model execution settings.
        """
        return {
            "runtime": self.runtime,
            "models": self.models,
            "permission_mode": self.permission_mode,
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
            "enable_learning": self.enable_learning,
            "max_concurrent_issues": self.max_concurrent_issues,
            "level_failure_abort_threshold": self.level_failure_abort_threshold,
            "check_ci": self.check_ci,
            "max_ci_fix_cycles": self.max_ci_fix_cycles,
            "ci_wait_seconds": self.ci_wait_seconds,
            "ci_poll_seconds": self.ci_poll_seconds,
        }


class BuildResult(BaseModel):
    """Final output of the end-to-end build pipeline."""

    plan_result: dict
    dag_state: dict
    verification: dict | None = None
    success: bool
    summary: str
    pr_results: list[RepoPRResult] = []  # Per-repo PR creation results
    # Per-repo result of the post-PR CI gate (watch → fix → repush → ready).
    # Empty when ``BuildConfig.check_ci`` is False or no PR was opened.
    ci_gate_results: list[dict] = []

    @property
    def pr_url(self) -> str:
        """Backward-compat: return the first successful PR URL, or empty string."""
        for r in self.pr_results:
            if r.success and r.pr_url:
                return r.pr_url
        return ""

    def model_dump(self, **kwargs: Any) -> dict[str, Any]:
        """Override to inject computed pr_url into serialisation output."""
        data = super().model_dump(**kwargs)
        data["pr_url"] = self.pr_url
        return data


class RepoFinalizeResult(BaseModel):
    """Result of the repo finalization (cleanup) step."""

    success: bool
    files_removed: list[str] = []
    gitignore_updated: bool = False
    summary: str = ""


class GitHubPRResult(BaseModel):
    """Result of pushing and creating a PR on GitHub."""

    success: bool
    pr_url: str = ""
    pr_number: int = 0
    error_message: str = ""


class CIFailedCheck(BaseModel):
    """One failing GitHub check on a PR."""

    name: str
    workflow: str = ""
    conclusion: str = ""  # FAILURE, CANCELLED, TIMED_OUT, ACTION_REQUIRED, etc.
    details_url: str = ""
    logs_excerpt: str = ""  # tail of the failed job's log, truncated


class CIWatchResult(BaseModel):
    """Outcome of waiting for CI checks on a PR."""

    status: Literal["passed", "failed", "timed_out", "no_checks", "error"]
    pr_number: int
    elapsed_seconds: int = 0
    failed_checks: list[CIFailedCheck] = []
    summary: str = ""


class CIFixResult(BaseModel):
    """Output from one iteration of the CI fixer agent."""

    fixed: bool  # True if the agent believes it has resolved all failures
    files_changed: list[str] = []
    commit_sha: str = ""  # SHA of the fix commit, if pushed
    pushed: bool = False  # True if the agent pushed the fix to origin
    summary: str = ""
    rejected_workarounds: list[str] = []  # legitimate-fix self-checks the agent ran
    error_message: str = ""


class ReviewCommentRef(BaseModel):
    """One review comment on an existing PR that the resolver should consider.

    Carries enough context for the resolver to find the comment thread, decide
    whether the change addresses it, and (back in github-buddy) reply +
    resolveReviewThread via the GraphQL API.

    `thread_id` is the GraphQL node id of the PR review thread (used to call
    `resolveReviewThread`). `comment_id` is the REST id of the comment (used
    to post the inline reply via `/repos/{o}/{r}/pulls/{n}/comments/{id}/replies`).
    Either may be empty when the source comment is a plain issue-comment on
    the PR conversation (not anchored to a code line) — in that case the
    resolver still addresses it but no thread can be resolved.
    """

    comment_id: int = 0  # 0 when not a review comment (e.g. PR conversation comment)
    thread_id: str = ""  # GraphQL node id of the review thread; empty for non-review
    path: str = ""  # File path the comment is anchored to ("" for non-review)
    line: int = 0  # Line number the comment is anchored to (0 for non-review)
    author: str = ""  # GitHub login of the commenter
    body: str = ""  # The comment body (markdown)
    url: str = ""  # html_url for the comment


class AddressedComment(BaseModel):
    """The resolver agent's record of one comment it claims to have addressed.

    Used to drive the post-resolve "reply + resolveReviewThread" pass. The
    agent decides which comments it actually addressed (it may judge some
    irrelevant or out-of-scope and explain in `note`); only entries with
    `addressed=True` get a reply posted and the thread resolved.
    """

    comment_id: int = 0
    thread_id: str = ""
    addressed: bool
    note: str = ""  # one-line: "fixed by ...", "skipped because ..."


class PRResolveResult(BaseModel):
    """Output from one run of the PR-resolver agent.

    The agent both resolves any in-progress merge from base AND addresses CI
    failures + review comments in a single harness session, then commits and
    pushes. Caller (the `resolve` entry reasoner) then runs the existing CI
    watch+fix loop and posts replies on every `addressed=True` comment.
    """

    fixed: bool  # True if the agent believes it produced a correct, complete fix
    merge_resolved: bool = False  # True iff a merge from base was completed (with or without prior conflicts)
    files_changed: list[str] = []
    commit_shas: list[str] = []  # All new commits the agent created (merge + fixes)
    pushed: bool = False  # True if `git push` succeeded
    addressed_comments: list[AddressedComment] = []
    summary: str = ""
    rejected_workarounds: list[str] = []  # legitimate-fix self-checks the agent ran
    error_message: str = ""


class ExecutionConfig(BaseModel):
    """Configuration for the DAG executor."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["claude_code", "open_code"] = Field(default_factory=_default_runtime)
    models: dict[str, str] | None = None
    _resolved_models: dict[str, str] = PrivateAttr(default_factory=dict)

    max_retries_per_issue: int = 1
    max_replans: int = 2
    enable_replanning: bool = True
    max_integration_test_retries: int = 1
    enable_integration_testing: bool = True
    max_coding_iterations: int = 5
    agent_max_turns: int = DEFAULT_AGENT_MAX_TURNS
    permission_mode: str = ""
    agent_timeout_seconds: int = 2700  # 45 min
    max_advisor_invocations: int = 2
    enable_issue_advisor: bool = True
    enable_learning: bool = False
    max_concurrent_issues: int = 3  # max parallel issues per level (0 = unlimited)
    level_failure_abort_threshold: float = (
        0.8  # abort DAG when >= this fraction of a level fails
    )
    # Mirrored from BuildConfig so the post-PR CI gate sees the same caps when
    # invoked from the build pipeline.
    check_ci: bool = True
    max_ci_fix_cycles: int = 2
    ci_wait_seconds: int = 1500
    ci_poll_seconds: int = 30

    @model_validator(mode="before")
    @classmethod
    def _validate_v2_keys(cls, data: Any) -> Any:
        return _reject_legacy_config_keys(data)

    @model_validator(mode="after")
    def _normalize_provider_field(self) -> "ExecutionConfig":
        # Normalize legacy provider names at config boundary for defense-in-depth
        # (inline mappings in execution_agents.py/pipeline.py provide first layer)
        self.runtime = "claude_code" if self.runtime == "claude" else self.runtime
        return self

    def model_post_init(self, __context: Any) -> None:
        """Resolve runtime model selection once at construction time."""
        self._resolved_models = resolve_runtime_models(
            runtime=self.runtime,
            models=self.models,
        )

    def _model_for(self, field_name: str) -> str:
        return self._resolved_models[field_name]

    @property
    def ai_provider(self) -> Literal["claude", "opencode"]:
        return _runtime_to_provider(self.runtime)

    @property
    def pm_model(self) -> str:
        return self._model_for("pm_model")

    @property
    def architect_model(self) -> str:
        return self._model_for("architect_model")

    @property
    def tech_lead_model(self) -> str:
        return self._model_for("tech_lead_model")

    @property
    def sprint_planner_model(self) -> str:
        return self._model_for("sprint_planner_model")

    @property
    def coder_model(self) -> str:
        return self._model_for("coder_model")

    @property
    def qa_model(self) -> str:
        return self._model_for("qa_model")

    @property
    def code_reviewer_model(self) -> str:
        return self._model_for("code_reviewer_model")

    @property
    def qa_synthesizer_model(self) -> str:
        return self._model_for("qa_synthesizer_model")

    @property
    def replan_model(self) -> str:
        return self._model_for("replan_model")

    @property
    def retry_advisor_model(self) -> str:
        return self._model_for("retry_advisor_model")

    @property
    def issue_writer_model(self) -> str:
        return self._model_for("issue_writer_model")

    @property
    def issue_advisor_model(self) -> str:
        return self._model_for("issue_advisor_model")

    @property
    def verifier_model(self) -> str:
        return self._model_for("verifier_model")

    @property
    def git_model(self) -> str:
        return self._model_for("git_model")

    @property
    def merger_model(self) -> str:
        return self._model_for("merger_model")

    @property
    def integration_tester_model(self) -> str:
        return self._model_for("integration_tester_model")

    @property
    def ci_fixer_model(self) -> str:
        return self._model_for("ci_fixer_model")
