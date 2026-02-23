"""Integration tests for per-role timeout callsite updates.

Verifies that all 6 agent callsites in dag_executor.py and coding_loop.py
use config.timeout_for_role() with the correct role strings.
"""

import re
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, call
from swe_af.execution.schemas import (
    ExecutionConfig,
    DAGState,
    IssueResult,
    IssueOutcome,
)


class TestTimeoutForRoleMethod:
    """Test the timeout_for_role method behavior."""

    def test_unknown_role_falls_back_to_default(self):
        """Unknown role should fall back to agent_timeout_seconds."""
        config = ExecutionConfig(agent_timeout_seconds=3000)
        timeout = config.timeout_for_role('unknown_role')
        assert timeout == 3000

    def test_known_role_uses_specific_timeout(self):
        """Known role should use role-specific timeout."""
        config = ExecutionConfig(
            agent_timeout_seconds=3000,
            coder_timeout=1800,
        )
        coder_timeout = config.timeout_for_role('coder')
        assert coder_timeout == 1800

    def test_issue_advisor_timeout(self):
        """Test issue_advisor role timeout."""
        config = ExecutionConfig(issue_advisor_timeout=1500)
        assert config.timeout_for_role('issue_advisor') == 1500

    def test_replan_timeout(self):
        """Test replan role timeout."""
        config = ExecutionConfig(replan_timeout=1500)
        assert config.timeout_for_role('replan') == 1500

    def test_coder_timeout(self):
        """Test coder role timeout."""
        config = ExecutionConfig(coder_timeout=1800)
        assert config.timeout_for_role('coder') == 1800

    def test_qa_timeout(self):
        """Test qa role timeout."""
        config = ExecutionConfig(qa_timeout=1500)
        assert config.timeout_for_role('qa') == 1500

    def test_code_reviewer_timeout(self):
        """Test code_reviewer role timeout."""
        config = ExecutionConfig(code_reviewer_timeout=1500)
        assert config.timeout_for_role('code_reviewer') == 1500

    def test_qa_synthesizer_timeout(self):
        """Test qa_synthesizer role timeout."""
        config = ExecutionConfig(qa_synthesizer_timeout=900)
        assert config.timeout_for_role('qa_synthesizer') == 900


class TestAllCallsitesUpdated:
    """Integration tests to verify all callsites are updated."""

    def test_no_hardcoded_agent_timeout_seconds_in_callsites(self):
        """Verify no hardcoded config.agent_timeout_seconds in agent call wrappers."""
        # Read dag_executor.py
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/dag_executor.py', 'r') as f:
            dag_executor_content = f.read()

        # Read coding_loop.py
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/coding_loop.py', 'r') as f:
            coding_loop_content = f.read()

        # Pattern to find timeout=config.agent_timeout_seconds
        # We allow it in the _call_with_timeout function signature default but not in calls
        pattern = r'timeout\s*=\s*config\.agent_timeout_seconds'

        # Check dag_executor.py - exclude _call_with_timeout function definition
        dag_lines = dag_executor_content.split('\n')
        for i, line in enumerate(dag_lines):
            if re.search(pattern, line):
                # Allow it only in function signature default values
                if 'def _call_with_timeout' not in '\n'.join(dag_lines[max(0, i-5):i+1]):
                    pytest.fail(f"Found hardcoded timeout in dag_executor.py line {i+1}: {line}")

        # Check coding_loop.py - should have NO hardcoded timeouts now
        # (we removed the timeout variable assignment)
        coding_lines = coding_loop_content.split('\n')
        for i, line in enumerate(coding_lines):
            if re.search(pattern, line):
                # Allow it only in function signature defaults
                if 'def _call_with_timeout' not in '\n'.join(coding_lines[max(0, i-5):i+1]):
                    pytest.fail(f"Found hardcoded timeout in coding_loop.py line {i+1}: {line}")

    def test_all_timeout_for_role_calls_present(self):
        """Verify all expected timeout_for_role calls are present."""
        # Read dag_executor.py
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/dag_executor.py', 'r') as f:
            dag_executor_content = f.read()

        # Read coding_loop.py
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/coding_loop.py', 'r') as f:
            coding_loop_content = f.read()

        # Check for expected timeout_for_role calls
        expected_calls = [
            ("issue_advisor", dag_executor_content),
            ("replan", dag_executor_content),
            ("coder", coding_loop_content),
            ("qa", coding_loop_content),
            ("code_reviewer", coding_loop_content),
            ("qa_synthesizer", coding_loop_content),
        ]

        missing_roles = []
        for role, content in expected_calls:
            pattern = f"timeout_for_role\\(['\"]({role})['\"]\\)"
            if not re.search(pattern, content):
                missing_roles.append(role)

        if missing_roles:
            pytest.fail(f"Missing timeout_for_role calls for roles: {', '.join(missing_roles)}")

    def test_issue_advisor_callsite_location(self):
        """Verify Issue Advisor callsite is in dag_executor.py."""
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/dag_executor.py', 'r') as f:
            content = f.read()

        # Look for issue_advisor callsite with timeout
        pattern = r'timeout\s*=\s*config\.timeout_for_role\(["\']issue_advisor["\']\)'
        assert re.search(pattern, content), "Issue Advisor callsite not found with timeout_for_role"

    def test_replanner_callsite_location(self):
        """Verify Replanner callsite is in dag_executor.py with _call_with_timeout wrapper."""
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/dag_executor.py', 'r') as f:
            content = f.read()

        # Look for replanner callsite with timeout
        pattern = r'timeout\s*=\s*config\.timeout_for_role\(["\']replan["\']\)'
        assert re.search(pattern, content), "Replanner callsite not found with timeout_for_role"

        # Verify it's wrapped in _call_with_timeout
        # Look for pattern: _call_with_timeout(...run_replanner...)
        wrapper_pattern = r'_call_with_timeout\([^)]*run_replanner'
        assert re.search(wrapper_pattern, content), "Replanner callsite not wrapped in _call_with_timeout"

    def test_coder_callsite_location(self):
        """Verify Coder callsite is in coding_loop.py."""
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/coding_loop.py', 'r') as f:
            content = f.read()

        # Look for coder callsite with timeout
        pattern = r'timeout\s*=\s*config\.timeout_for_role\(["\']coder["\']\)'
        assert re.search(pattern, content), "Coder callsite not found with timeout_for_role"

    def test_qa_callsite_location(self):
        """Verify QA callsite is in coding_loop.py."""
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/coding_loop.py', 'r') as f:
            content = f.read()

        # Look for qa callsite with timeout
        pattern = r'timeout\s*=\s*config\.timeout_for_role\(["\']qa["\']\)'
        assert re.search(pattern, content), "QA callsite not found with timeout_for_role"

    def test_code_reviewer_callsite_location(self):
        """Verify Code Reviewer callsite is in coding_loop.py."""
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/coding_loop.py', 'r') as f:
            content = f.read()

        # Look for code_reviewer callsite with timeout (should appear at least twice - default and flagged paths)
        pattern = r'timeout\s*=\s*config\.timeout_for_role\(["\']code_reviewer["\']\)'
        matches = re.findall(pattern, content)
        assert len(matches) >= 2, f"Code Reviewer callsite should appear at least twice (found {len(matches)})"

    def test_qa_synthesizer_callsite_location(self):
        """Verify QA Synthesizer callsite is in coding_loop.py."""
        with open('/workspaces/SWE-AF/workspace/SWE-AF/swe_af/execution/coding_loop.py', 'r') as f:
            content = f.read()

        # Look for qa_synthesizer callsite with timeout
        pattern = r'timeout\s*=\s*config\.timeout_for_role\(["\']qa_synthesizer["\']\)'
        assert re.search(pattern, content), "QA Synthesizer callsite not found with timeout_for_role"


class TestCallsiteRoleStrings:
    """Test that all callsites use the correct role strings."""

    def test_all_role_strings_match_config_fields(self):
        """Verify all role strings have corresponding timeout fields in ExecutionConfig."""
        role_strings = [
            "issue_advisor",
            "replan",
            "coder",
            "qa",
            "code_reviewer",
            "qa_synthesizer",
        ]

        config = ExecutionConfig()

        for role in role_strings:
            # Each role should have a corresponding timeout field
            field_name = f"{role}_timeout"
            assert hasattr(config, field_name), f"ExecutionConfig missing {field_name} field"

            # timeout_for_role should return the field value
            expected_timeout = getattr(config, field_name)
            actual_timeout = config.timeout_for_role(role)
            assert actual_timeout == expected_timeout, \
                f"timeout_for_role('{role}') returned {actual_timeout}, expected {expected_timeout}"
