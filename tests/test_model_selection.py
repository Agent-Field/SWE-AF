"""Tests for model downgrade to haiku for git, merger, and issue_writer roles."""

from __future__ import annotations

import unittest

from swe_af.execution.schemas import (
    BuildConfig,
    ExecutionConfig,
    resolve_runtime_models,
)


class TestModelDowngrade(unittest.TestCase):
    """Test that git, merger, and issue_writer models default to haiku."""

    def test_git_model_defaults_to_haiku(self) -> None:
        """AC1: _RUNTIME_BASE_MODELS['claude_code'] sets git_model='haiku'."""
        resolved = resolve_runtime_models(runtime="claude_code", models=None)
        self.assertEqual(resolved["git_model"], "haiku")

    def test_merger_model_defaults_to_haiku(self) -> None:
        """AC2: _RUNTIME_BASE_MODELS['claude_code'] sets merger_model='haiku'."""
        resolved = resolve_runtime_models(runtime="claude_code", models=None)
        self.assertEqual(resolved["merger_model"], "haiku")

    def test_issue_writer_model_defaults_to_haiku(self) -> None:
        """AC3: _RUNTIME_BASE_MODELS['claude_code'] sets issue_writer_model='haiku'."""
        resolved = resolve_runtime_models(runtime="claude_code", models=None)
        self.assertEqual(resolved["issue_writer_model"], "haiku")

    def test_qa_synthesizer_model_preserved(self) -> None:
        """AC4: qa_synthesizer_model='haiku' preserved from baseline."""
        resolved = resolve_runtime_models(runtime="claude_code", models=None)
        self.assertEqual(resolved["qa_synthesizer_model"], "haiku")

    def test_git_override_to_sonnet_functional(self) -> None:
        """AC5: Config override via models={'git': 'sonnet'} still functional."""
        resolved = resolve_runtime_models(
            runtime="claude_code",
            models={"git": "sonnet"}
        )
        self.assertEqual(resolved["git_model"], "sonnet")
        # Other haiku defaults should remain
        self.assertEqual(resolved["merger_model"], "haiku")
        self.assertEqual(resolved["issue_writer_model"], "haiku")
        self.assertEqual(resolved["qa_synthesizer_model"], "haiku")

    def test_merger_override_to_sonnet_functional(self) -> None:
        """Verify merger override works."""
        resolved = resolve_runtime_models(
            runtime="claude_code",
            models={"merger": "sonnet"}
        )
        self.assertEqual(resolved["merger_model"], "sonnet")
        # Other haiku defaults should remain
        self.assertEqual(resolved["git_model"], "haiku")
        self.assertEqual(resolved["issue_writer_model"], "haiku")

    def test_issue_writer_override_to_sonnet_functional(self) -> None:
        """Verify issue_writer override works."""
        resolved = resolve_runtime_models(
            runtime="claude_code",
            models={"issue_writer": "sonnet"}
        )
        self.assertEqual(resolved["issue_writer_model"], "sonnet")
        # Other haiku defaults should remain
        self.assertEqual(resolved["git_model"], "haiku")
        self.assertEqual(resolved["merger_model"], "haiku")

    def test_all_three_roles_default_to_haiku(self) -> None:
        """Verify all three downgraded roles default to haiku."""
        resolved = resolve_runtime_models(runtime="claude_code", models=None)
        self.assertEqual(resolved["git_model"], "haiku")
        self.assertEqual(resolved["merger_model"], "haiku")
        self.assertEqual(resolved["issue_writer_model"], "haiku")

    def test_execution_config_git_model_haiku(self) -> None:
        """Verify ExecutionConfig resolves git_model to haiku."""
        cfg = ExecutionConfig(runtime="claude_code")
        self.assertEqual(cfg.git_model, "haiku")

    def test_execution_config_merger_model_haiku(self) -> None:
        """Verify ExecutionConfig resolves merger_model to haiku."""
        cfg = ExecutionConfig(runtime="claude_code")
        self.assertEqual(cfg.merger_model, "haiku")

    def test_execution_config_issue_writer_model_haiku(self) -> None:
        """Verify ExecutionConfig resolves issue_writer_model to haiku."""
        cfg = ExecutionConfig(runtime="claude_code")
        self.assertEqual(cfg.issue_writer_model, "haiku")

    def test_build_config_git_model_haiku(self) -> None:
        """Verify BuildConfig resolves git_model to haiku."""
        cfg = BuildConfig(runtime="claude_code")
        resolved = cfg.resolved_models()
        self.assertEqual(resolved["git_model"], "haiku")

    def test_build_config_merger_model_haiku(self) -> None:
        """Verify BuildConfig resolves merger_model to haiku."""
        cfg = BuildConfig(runtime="claude_code")
        resolved = cfg.resolved_models()
        self.assertEqual(resolved["merger_model"], "haiku")

    def test_build_config_issue_writer_model_haiku(self) -> None:
        """Verify BuildConfig resolves issue_writer_model to haiku."""
        cfg = BuildConfig(runtime="claude_code")
        resolved = cfg.resolved_models()
        self.assertEqual(resolved["issue_writer_model"], "haiku")

    def test_other_roles_still_sonnet(self) -> None:
        """Verify other roles (not downgraded) still default to sonnet."""
        resolved = resolve_runtime_models(runtime="claude_code", models=None)
        # Planning roles should still be sonnet (except sprint_planner which is haiku)
        self.assertEqual(resolved["pm_model"], "sonnet")
        self.assertEqual(resolved["architect_model"], "sonnet")
        self.assertEqual(resolved["tech_lead_model"], "sonnet")
        # Coding roles should still be sonnet
        self.assertEqual(resolved["coder_model"], "sonnet")
        self.assertEqual(resolved["qa_model"], "sonnet")
        self.assertEqual(resolved["code_reviewer_model"], "sonnet")

    def test_default_override_applies_to_all_including_downgraded(self) -> None:
        """Verify models.default override applies to all roles including downgraded ones."""
        resolved = resolve_runtime_models(
            runtime="claude_code",
            models={"default": "opus"}
        )
        # All models should be opus when default is set
        self.assertEqual(resolved["git_model"], "opus")
        self.assertEqual(resolved["merger_model"], "opus")
        self.assertEqual(resolved["issue_writer_model"], "opus")
        self.assertEqual(resolved["qa_synthesizer_model"], "opus")
        self.assertEqual(resolved["coder_model"], "opus")

    def test_role_override_beats_default_for_downgraded_roles(self) -> None:
        """Verify role-specific override beats default for downgraded roles."""
        resolved = resolve_runtime_models(
            runtime="claude_code",
            models={"default": "opus", "git": "haiku"}
        )
        self.assertEqual(resolved["git_model"], "haiku")
        self.assertEqual(resolved["merger_model"], "opus")
        self.assertEqual(resolved["coder_model"], "opus")


if __name__ == "__main__":
    unittest.main()
