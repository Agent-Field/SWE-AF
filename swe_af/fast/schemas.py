"""Pydantic schemas for the swe-fast single-pass build node."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

# ---------------------------------------------------------------------------
# Runtime default model strings
# ---------------------------------------------------------------------------

_CLAUDE_CODE_DEFAULT = "haiku"
_OPEN_CODE_DEFAULT = "qwen/qwen-2.5-coder-32b-instruct"

_RUNTIME_DEFAULTS: dict[str, str] = {
    "claude_code": _CLAUDE_CODE_DEFAULT,
    "open_code": _OPEN_CODE_DEFAULT,
}

# All four roles resolved by fast_resolve_models()
_FAST_ROLES: tuple[str, ...] = ("pm_model", "coder_model", "verifier_model", "git_model")

# Mapping from role name (in models dict) to resolved key
_ROLE_KEY_MAP: dict[str, str] = {
    "pm": "pm_model",
    "coder": "coder_model",
    "verifier": "verifier_model",
    "git": "git_model",
}


# ---------------------------------------------------------------------------
# Task-level schemas
# ---------------------------------------------------------------------------


class FastTask(BaseModel):
    """A single task in the flat fast-build decomposition."""

    model_config = ConfigDict(extra="forbid")

    name: str                           # kebab-case slug
    title: str                          # human-readable title
    description: str                    # self-contained description for the coder
    acceptance_criteria: list[str]      # task-specific acceptance criteria
    files_to_create: list[str] = []
    files_to_modify: list[str] = []
    estimated_minutes: int = 5


class FastPlanResult(BaseModel):
    """Output of the fast planner reasoner."""

    tasks: list[FastTask]
    rationale: str = ""
    fallback_used: bool = False


class FastTaskResult(BaseModel):
    """Result of executing a single FastTask."""

    task_name: str
    outcome: str                # "completed" | "failed" | "timeout"
    files_changed: list[str] = []
    summary: str = ""
    error: str = ""


class FastExecutionResult(BaseModel):
    """Aggregate result of executing all tasks."""

    task_results: list[FastTaskResult]
    completed_count: int
    failed_count: int
    timed_out: bool = False


class FastVerificationResult(BaseModel):
    """Result of the single verification pass."""

    passed: bool
    summary: str = ""
    criteria_results: list[dict] = []
    suggested_fixes: list[str] = []


# ---------------------------------------------------------------------------
# Build-level schemas
# ---------------------------------------------------------------------------


class FastBuildConfig(BaseModel):
    """Configuration for a fast single-pass build run."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["claude_code", "open_code"] = "claude_code"
    models: dict[str, str] | None = None
    max_tasks: int = 10
    task_timeout_seconds: int = 300
    build_timeout_seconds: int = 600
    enable_github_pr: bool = True
    github_pr_base: str = ""
    permission_mode: str = ""
    repo_url: str = ""
    agent_max_turns: int = 50


class FastBuildResult(BaseModel):
    """Top-level result returned by the fast build reasoner."""

    plan_result: dict
    execution_result: dict
    verification: dict | None = None
    success: bool
    summary: str
    pr_url: str = ""


# ---------------------------------------------------------------------------
# Model resolution helper
# ---------------------------------------------------------------------------


def fast_resolve_models(config: FastBuildConfig) -> dict[str, str]:
    """Resolve the four role model strings for a fast build run.

    Resolution order (last wins):
      1. Runtime default (haiku or qwen depending on runtime)
      2. ``models["default"]`` — overrides all roles
      3. ``models["<role>"]`` — overrides a specific role (pm, coder, verifier, git)

    Args:
        config: A :class:`FastBuildConfig` instance.

    Returns:
        A dict with keys ``pm_model``, ``coder_model``, ``verifier_model``,
        ``git_model`` mapping to model name strings.

    Raises:
        ValueError: If ``config.models`` contains a key that is not ``"default"``
            and not one of the four known role names.
    """
    runtime_default = _RUNTIME_DEFAULTS[config.runtime]

    resolved: dict[str, str] = {role: runtime_default for role in _FAST_ROLES}

    if config.models:
        # Validate all keys first
        valid_keys = {"default"} | set(_ROLE_KEY_MAP.keys())
        for key in config.models:
            if key not in valid_keys:
                raise ValueError(
                    f"Unknown role key {key!r} in models dict. "
                    f"Valid keys are: {sorted(valid_keys)}"
                )

        # Apply "default" override first
        if "default" in config.models:
            for role in _FAST_ROLES:
                resolved[role] = config.models["default"]

        # Apply per-role overrides
        for role_key, resolved_key in _ROLE_KEY_MAP.items():
            if role_key in config.models:
                resolved[resolved_key] = config.models[role_key]

    return resolved
