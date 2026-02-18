"""Tests for swe_af.fast.schemas — FastBuildConfig, FastTask, result types,
and fast_resolve_models()."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swe_af.fast.schemas import (
    FastBuildConfig,
    FastBuildResult,
    FastExecutionResult,
    FastPlanResult,
    FastTask,
    FastTaskResult,
    FastVerificationResult,
    fast_resolve_models,
)

# ---------------------------------------------------------------------------
# FastBuildConfig defaults (AC-3)
# ---------------------------------------------------------------------------

_ALL_FOUR_ROLES = ("pm_model", "coder_model", "verifier_model", "git_model")


class TestFastBuildConfigDefaults:
    def test_runtime_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.runtime == "claude_code"

    def test_max_tasks_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.max_tasks == 10

    def test_task_timeout_seconds_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.task_timeout_seconds == 300

    def test_build_timeout_seconds_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.build_timeout_seconds == 600

    def test_enable_github_pr_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.enable_github_pr is True

    def test_agent_max_turns_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.agent_max_turns == 50

    def test_models_default_is_none(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.models is None

    def test_github_pr_base_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.github_pr_base == ""

    def test_permission_mode_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.permission_mode == ""

    def test_repo_url_default(self) -> None:
        cfg = FastBuildConfig()
        assert cfg.repo_url == ""


# ---------------------------------------------------------------------------
# FastBuildConfig extra='forbid' (AC-2)
# ---------------------------------------------------------------------------


class TestFastBuildConfigForbidExtra:
    def test_unknown_field_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            FastBuildConfig(unknown_field="x")  # type: ignore[call-arg]

    def test_valid_fields_do_not_raise(self) -> None:
        # Should not raise
        cfg = FastBuildConfig(runtime="claude_code", max_tasks=5)
        assert cfg.max_tasks == 5


# ---------------------------------------------------------------------------
# fast_resolve_models — claude_code runtime (AC-4)
# ---------------------------------------------------------------------------


class TestFastResolveModelsCludeCode:
    def test_all_roles_are_haiku(self) -> None:
        cfg = FastBuildConfig(runtime="claude_code")
        resolved = fast_resolve_models(cfg)
        for role in _ALL_FOUR_ROLES:
            assert resolved[role] == "haiku", f"{role} should be 'haiku'"

    def test_returns_all_four_roles(self) -> None:
        cfg = FastBuildConfig(runtime="claude_code")
        resolved = fast_resolve_models(cfg)
        assert set(resolved.keys()) == set(_ALL_FOUR_ROLES)


# ---------------------------------------------------------------------------
# fast_resolve_models — open_code runtime (AC-5)
# ---------------------------------------------------------------------------

_QWEN_MODEL = "qwen/qwen-2.5-coder-32b-instruct"


class TestFastResolveModelsOpenCode:
    def test_all_roles_are_qwen(self) -> None:
        cfg = FastBuildConfig(runtime="open_code")
        resolved = fast_resolve_models(cfg)
        for role in _ALL_FOUR_ROLES:
            assert resolved[role] == _QWEN_MODEL, f"{role} should be qwen model"

    def test_returns_all_four_roles(self) -> None:
        cfg = FastBuildConfig(runtime="open_code")
        resolved = fast_resolve_models(cfg)
        assert set(resolved.keys()) == set(_ALL_FOUR_ROLES)


# ---------------------------------------------------------------------------
# fast_resolve_models — model overrides (AC-6)
# ---------------------------------------------------------------------------


class TestFastResolveModelsOverrides:
    def test_coder_override_with_default(self) -> None:
        cfg = FastBuildConfig(
            runtime="claude_code",
            models={"coder": "sonnet", "default": "haiku"},
        )
        resolved = fast_resolve_models(cfg)
        assert resolved["coder_model"] == "sonnet"
        assert resolved["pm_model"] == "haiku"
        assert resolved["verifier_model"] == "haiku"
        assert resolved["git_model"] == "haiku"

    def test_default_key_overrides_all_roles(self) -> None:
        cfg = FastBuildConfig(runtime="claude_code", models={"default": "opus"})
        resolved = fast_resolve_models(cfg)
        for role in _ALL_FOUR_ROLES:
            assert resolved[role] == "opus"

    def test_per_role_override_wins_over_default(self) -> None:
        cfg = FastBuildConfig(
            runtime="open_code",
            models={"default": "haiku", "verifier": "sonnet"},
        )
        resolved = fast_resolve_models(cfg)
        assert resolved["verifier_model"] == "sonnet"
        assert resolved["pm_model"] == "haiku"
        assert resolved["coder_model"] == "haiku"
        assert resolved["git_model"] == "haiku"

    def test_all_role_overrides(self) -> None:
        cfg = FastBuildConfig(
            runtime="claude_code",
            models={
                "pm": "opus",
                "coder": "sonnet",
                "verifier": "haiku",
                "git": "haiku",
            },
        )
        resolved = fast_resolve_models(cfg)
        assert resolved["pm_model"] == "opus"
        assert resolved["coder_model"] == "sonnet"
        assert resolved["verifier_model"] == "haiku"
        assert resolved["git_model"] == "haiku"


# ---------------------------------------------------------------------------
# fast_resolve_models — unknown role key raises ValueError (edge case)
# ---------------------------------------------------------------------------


class TestFastResolveModelsUnknownRole:
    def test_unknown_role_key_raises_value_error(self) -> None:
        cfg = FastBuildConfig(runtime="claude_code", models={"unknown_role": "haiku"})
        with pytest.raises(ValueError, match="Unknown role key"):
            fast_resolve_models(cfg)

    def test_typo_in_role_key_raises_value_error(self) -> None:
        cfg = FastBuildConfig(runtime="claude_code", models={"coderr": "sonnet"})
        with pytest.raises(ValueError):
            fast_resolve_models(cfg)


# ---------------------------------------------------------------------------
# FastTask defaults (AC-7)
# ---------------------------------------------------------------------------


class TestFastTaskDefaults:
    def test_files_to_create_default_is_empty_list(self) -> None:
        task = FastTask(
            name="x",
            title="t",
            description="d",
            acceptance_criteria=["c"],
        )
        assert task.files_to_create == []

    def test_files_to_modify_default_is_empty_list(self) -> None:
        task = FastTask(
            name="x",
            title="t",
            description="d",
            acceptance_criteria=["c"],
        )
        assert task.files_to_modify == []

    def test_estimated_minutes_default(self) -> None:
        task = FastTask(
            name="x",
            title="t",
            description="d",
            acceptance_criteria=["c"],
        )
        assert task.estimated_minutes == 5

    def test_extra_field_raises(self) -> None:
        with pytest.raises(ValidationError):
            FastTask(
                name="x",
                title="t",
                description="d",
                acceptance_criteria=["c"],
                unexpected="value",  # type: ignore[call-arg]
            )


# ---------------------------------------------------------------------------
# FastBuildResult defaults (AC-19)
# ---------------------------------------------------------------------------


class TestFastBuildResultDefaults:
    def test_pr_url_default_is_empty_string(self) -> None:
        result = FastBuildResult(
            plan_result={},
            execution_result={},
            success=True,
            summary="ok",
        )
        assert result.pr_url == ""

    def test_verification_default_is_none(self) -> None:
        result = FastBuildResult(
            plan_result={},
            execution_result={},
            success=True,
            summary="ok",
        )
        assert result.verification is None

    def test_all_required_fields_accepted(self) -> None:
        result = FastBuildResult(
            plan_result={"tasks": []},
            execution_result={"completed": 1},
            success=False,
            summary="failed",
            pr_url="https://github.com/org/repo/pull/1",
        )
        assert result.pr_url == "https://github.com/org/repo/pull/1"


# ---------------------------------------------------------------------------
# FastTaskResult defaults (AC-20)
# ---------------------------------------------------------------------------


class TestFastTaskResultDefaults:
    def test_files_changed_default_is_empty_list(self) -> None:
        result = FastTaskResult(task_name="t1", outcome="completed")
        assert result.files_changed == []

    def test_outcome_roundtrip(self) -> None:
        for outcome in ("completed", "failed", "timeout"):
            result = FastTaskResult(task_name="t1", outcome=outcome)
            assert result.outcome == outcome

    def test_summary_default_is_empty_string(self) -> None:
        result = FastTaskResult(task_name="t1", outcome="completed")
        assert result.summary == ""

    def test_error_default_is_empty_string(self) -> None:
        result = FastTaskResult(task_name="t1", outcome="failed")
        assert result.error == ""


# ---------------------------------------------------------------------------
# FastPlanResult defaults
# ---------------------------------------------------------------------------


class TestFastPlanResultDefaults:
    def test_rationale_default(self) -> None:
        result = FastPlanResult(tasks=[])
        assert result.rationale == ""

    def test_fallback_used_default(self) -> None:
        result = FastPlanResult(tasks=[])
        assert result.fallback_used is False


# ---------------------------------------------------------------------------
# FastExecutionResult defaults
# ---------------------------------------------------------------------------


class TestFastExecutionResultDefaults:
    def test_timed_out_default(self) -> None:
        result = FastExecutionResult(task_results=[], completed_count=0, failed_count=0)
        assert result.timed_out is False


# ---------------------------------------------------------------------------
# FastVerificationResult defaults
# ---------------------------------------------------------------------------


class TestFastVerificationResultDefaults:
    def test_summary_default(self) -> None:
        result = FastVerificationResult(passed=True)
        assert result.summary == ""

    def test_criteria_results_default(self) -> None:
        result = FastVerificationResult(passed=False)
        assert result.criteria_results == []

    def test_suggested_fixes_default(self) -> None:
        result = FastVerificationResult(passed=False)
        assert result.suggested_fixes == []


# ---------------------------------------------------------------------------
# Module importability (AC-9)
# ---------------------------------------------------------------------------


class TestModuleImportability:
    def test_import_swe_af_fast(self) -> None:
        import swe_af.fast  # noqa: F401

    def test_import_swe_af_fast_app(self) -> None:
        import swe_af.fast.app  # noqa: F401

    def test_import_swe_af_fast_schemas(self) -> None:
        import swe_af.fast.schemas  # noqa: F401

    def test_import_swe_af_fast_planner(self) -> None:
        import swe_af.fast.planner  # noqa: F401

    def test_import_swe_af_fast_executor(self) -> None:
        import swe_af.fast.executor  # noqa: F401

    def test_import_swe_af_fast_verifier(self) -> None:
        import swe_af.fast.verifier  # noqa: F401
