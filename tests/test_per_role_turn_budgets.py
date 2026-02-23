"""Unit tests for per-role turn budgets and timeout configuration.

Tests verify that ExecutionConfig properly exposes per-role turn limits and
timeouts through accessor methods, with fallback to global defaults for
unknown roles.
"""

from __future__ import annotations

import pytest

from swe_af.execution.schemas import DEFAULT_AGENT_MAX_TURNS, ExecutionConfig


class TestPerRoleTurnBudgets:
    """Test max_turns_for_role() accessor method."""

    def test_all_15_roles_return_correct_turn_limits(self):
        """Verify max_turns_for_role() returns correct values for all 15 roles."""
        config = ExecutionConfig()

        # Planning roles (50 turns)
        assert config.max_turns_for_role("pm") == 50
        assert config.max_turns_for_role("architect") == 50
        assert config.max_turns_for_role("tech_lead") == 50
        assert config.max_turns_for_role("sprint_planner") == 50

        # Lightweight roles (30 turns)
        assert config.max_turns_for_role("issue_writer") == 30
        assert config.max_turns_for_role("qa_synthesizer") == 30
        assert config.max_turns_for_role("git") == 30

        # Orchestration roles (75 turns)
        assert config.max_turns_for_role("qa") == 75
        assert config.max_turns_for_role("code_reviewer") == 75
        assert config.max_turns_for_role("issue_advisor") == 75
        assert config.max_turns_for_role("replan") == 75
        assert config.max_turns_for_role("verifier") == 75
        assert config.max_turns_for_role("integration_tester") == 75

        # Coding role (100 turns)
        assert config.max_turns_for_role("coder") == 100

        # Other roles
        assert config.max_turns_for_role("retry_advisor") == 50
        assert config.max_turns_for_role("merger") == 50

    def test_unknown_role_returns_fallback(self):
        """Verify unknown role returns agent_max_turns fallback."""
        config = ExecutionConfig()
        assert config.max_turns_for_role("unknown_role") == DEFAULT_AGENT_MAX_TURNS
        assert config.max_turns_for_role("nonexistent") == 150

    def test_role_with_typo_returns_fallback(self):
        """Verify role with typo returns fallback."""
        config = ExecutionConfig()
        assert config.max_turns_for_role("codder") == DEFAULT_AGENT_MAX_TURNS  # typo
        assert config.max_turns_for_role("PM") == DEFAULT_AGENT_MAX_TURNS  # wrong case

    def test_backward_compatibility_with_agent_max_turns(self):
        """Verify backward compatibility: unknown roles use agent_max_turns override."""
        config = ExecutionConfig(agent_max_turns=200)
        assert config.max_turns_for_role("unknown_role") == 200
        assert config.max_turns_for_role("coder") == 100  # Per-role override still works

    def test_per_role_override_via_constructor(self):
        """Verify per-role values can be overridden at construction."""
        config = ExecutionConfig(coder_turns=120, pm_turns=60)
        assert config.max_turns_for_role("coder") == 120
        assert config.max_turns_for_role("pm") == 60
        assert config.max_turns_for_role("qa") == 75  # Default unchanged


class TestPerRoleTimeouts:
    """Test timeout_for_role() accessor method."""

    def test_all_15_roles_return_correct_timeouts(self):
        """Verify timeout_for_role() returns correct values for all 15 roles."""
        config = ExecutionConfig()

        # Planning roles (1200s = 20 min)
        assert config.timeout_for_role("pm") == 1200
        assert config.timeout_for_role("architect") == 1200
        assert config.timeout_for_role("tech_lead") == 1200
        assert config.timeout_for_role("sprint_planner") == 1200
        assert config.timeout_for_role("retry_advisor") == 1200
        assert config.timeout_for_role("merger") == 1200

        # Lightweight roles (900s = 15 min)
        assert config.timeout_for_role("issue_writer") == 900
        assert config.timeout_for_role("qa_synthesizer") == 900
        assert config.timeout_for_role("git") == 900

        # Coding role (1800s = 30 min)
        assert config.timeout_for_role("coder") == 1800

        # Orchestration roles (1500s = 25 min)
        assert config.timeout_for_role("qa") == 1500
        assert config.timeout_for_role("code_reviewer") == 1500
        assert config.timeout_for_role("issue_advisor") == 1500
        assert config.timeout_for_role("replan") == 1500
        assert config.timeout_for_role("verifier") == 1500
        assert config.timeout_for_role("integration_tester") == 1500

    def test_unknown_role_returns_timeout_fallback(self):
        """Verify unknown role returns agent_timeout_seconds fallback."""
        config = ExecutionConfig()
        assert config.timeout_for_role("unknown_role") == 2700
        assert config.timeout_for_role("nonexistent") == 2700

    def test_role_with_typo_returns_timeout_fallback(self):
        """Verify role with typo returns timeout fallback."""
        config = ExecutionConfig()
        assert config.timeout_for_role("codder") == 2700  # typo
        assert config.timeout_for_role("PM") == 2700  # wrong case

    def test_backward_compatibility_with_agent_timeout_seconds(self):
        """Verify backward compatibility: unknown roles use agent_timeout_seconds override."""
        config = ExecutionConfig(agent_timeout_seconds=3600)
        assert config.timeout_for_role("unknown_role") == 3600
        assert config.timeout_for_role("coder") == 1800  # Per-role override still works

    def test_per_role_timeout_override_via_constructor(self):
        """Verify per-role timeout values can be overridden at construction."""
        config = ExecutionConfig(coder_timeout=2400, pm_timeout=1500)
        assert config.timeout_for_role("coder") == 2400
        assert config.timeout_for_role("pm") == 1500
        assert config.timeout_for_role("qa") == 1500  # Default unchanged


class TestExecutionConfigDefaults:
    """Test ExecutionConfig instantiation with defaults."""

    def test_default_instantiation(self):
        """Verify ExecutionConfig can be instantiated with defaults."""
        config = ExecutionConfig()
        assert config.agent_max_turns == DEFAULT_AGENT_MAX_TURNS
        assert config.agent_timeout_seconds == 2700
        assert config.coder_turns == 100
        assert config.coder_timeout == 1800

    def test_defaults_preserved_as_fallbacks(self):
        """Verify DEFAULT_AGENT_MAX_TURNS and agent_timeout_seconds are preserved."""
        config = ExecutionConfig()
        assert config.agent_max_turns == 150
        assert config.agent_timeout_seconds == 2700


class TestEdgeCases:
    """Test edge cases and error handling."""

    def test_empty_role_string_returns_fallback(self):
        """Verify empty string role returns fallback."""
        config = ExecutionConfig()
        assert config.max_turns_for_role("") == DEFAULT_AGENT_MAX_TURNS
        assert config.timeout_for_role("") == 2700

    def test_all_16_turn_fields_exist(self):
        """Verify all 16 per-role turn fields exist on ExecutionConfig."""
        config = ExecutionConfig()
        expected_fields = [
            "pm_turns",
            "architect_turns",
            "tech_lead_turns",
            "sprint_planner_turns",
            "issue_writer_turns",
            "coder_turns",
            "qa_turns",
            "code_reviewer_turns",
            "qa_synthesizer_turns",
            "issue_advisor_turns",
            "replan_turns",
            "verifier_turns",
            "retry_advisor_turns",
            "git_turns",
            "merger_turns",
            "integration_tester_turns",
        ]
        for field in expected_fields:
            assert hasattr(config, field), f"Missing field: {field}"

    def test_all_16_timeout_fields_exist(self):
        """Verify all 16 per-role timeout fields exist on ExecutionConfig."""
        config = ExecutionConfig()
        expected_fields = [
            "pm_timeout",
            "architect_timeout",
            "tech_lead_timeout",
            "sprint_planner_timeout",
            "issue_writer_timeout",
            "coder_timeout",
            "qa_timeout",
            "code_reviewer_timeout",
            "qa_synthesizer_timeout",
            "issue_advisor_timeout",
            "replan_timeout",
            "verifier_timeout",
            "retry_advisor_timeout",
            "git_timeout",
            "merger_timeout",
            "integration_tester_timeout",
        ]
        for field in expected_fields:
            assert hasattr(config, field), f"Missing field: {field}"

    def test_accessor_methods_are_callable(self):
        """Verify accessor methods exist and are callable."""
        config = ExecutionConfig()
        assert callable(config.max_turns_for_role)
        assert callable(config.timeout_for_role)

    def test_multiple_configs_are_independent(self):
        """Verify multiple ExecutionConfig instances don't interfere."""
        config1 = ExecutionConfig(coder_turns=100)
        config2 = ExecutionConfig(coder_turns=200)
        assert config1.max_turns_for_role("coder") == 100
        assert config2.max_turns_for_role("coder") == 200
