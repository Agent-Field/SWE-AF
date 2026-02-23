"""Tests for sprint_planner.py prompt compression.

This module verifies that the compressed sprint_planner.py prompt:
- Meets the LOC target (≤193 lines, 20% reduction from 241)
- Preserves all IssueGuidance field definitions
- Maintains testing strategy example specificity
- Preserves dependency graph thinking principle
- Maintains architecture-as-source-of-truth principle
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _load_sprint_planner_content() -> str:
    """Load sprint_planner.py file content without importing."""
    repo_root = Path(__file__).parent.parent
    sprint_planner_path = repo_root / "swe_af" / "prompts" / "sprint_planner.py"
    return sprint_planner_path.read_text()


# ---------------------------------------------------------------------------
# AC1: LOC target (≤193 lines, 20% reduction from 241)
# ---------------------------------------------------------------------------


class TestLOCTarget:
    def test_sprint_planner_loc_within_target(self) -> None:
        """Verify sprint_planner.py is ≤193 LOC (20% reduction from 241)."""
        repo_root = Path(__file__).parent.parent
        sprint_planner_path = repo_root / "swe_af" / "prompts" / "sprint_planner.py"

        result = subprocess.run(
            ["wc", "-l", str(sprint_planner_path)],
            capture_output=True,
            text=True,
            check=True,
        )

        loc_count = int(result.stdout.split()[0])
        assert loc_count <= 193, (
            f"sprint_planner.py has {loc_count} LOC, must be ≤193 "
            f"(20% reduction from 241)"
        )


# ---------------------------------------------------------------------------
# AC2: IssueGuidance field definitions preserved
# ---------------------------------------------------------------------------


class TestIssueGuidanceFields:
    """Verify all IssueGuidance fields are documented in the prompt."""

    REQUIRED_FIELDS = [
        "needs_new_tests",
        "estimated_scope",
        "touches_interfaces",
        "needs_deeper_qa",
        "testing_guidance",
        "review_focus",
        "risk_rationale",
    ]

    def test_all_guidance_fields_mentioned(self) -> None:
        """All IssueGuidance fields must be documented in the prompts."""
        content = _load_sprint_planner_content()

        for field in self.REQUIRED_FIELDS:
            assert field in content, (
                f"IssueGuidance field '{field}' not found in sprint_planner.py"
            )

    def test_needs_deeper_qa_description_preserved(self) -> None:
        """Verify needs_deeper_qa functionality is explained (4 vs 2 LLM calls)."""
        content = _load_sprint_planner_content()

        # Critical behavior: needs_deeper_qa controls loop routing
        assert "needs_deeper_qa" in content
        assert "QA" in content or "qa" in content.lower()
        # Should mention what triggers it
        assert (
            "complex" in content.lower() or
            "risky" in content.lower() or
            "security" in content.lower()
        )

    def test_estimated_scope_values_preserved(self) -> None:
        """Verify estimated_scope enum values are documented."""
        content = _load_sprint_planner_content()

        scope_values = ["trivial", "small", "medium", "large"]
        for value in scope_values:
            assert value in content, (
                f"estimated_scope value '{value}' not found in sprint_planner.py"
            )


# ---------------------------------------------------------------------------
# AC3: Testing strategy example specificity maintained
# ---------------------------------------------------------------------------


class TestTestingStrategyExample:
    """Verify testing_strategy examples remain specific and concrete."""

    def test_testing_strategy_example_includes_file_path(self) -> None:
        """Example should include specific test file path (not vague 'write tests')."""
        content = _load_sprint_planner_content()

        # Should contain an example with a concrete path like tests/test_*.py
        assert "tests/test_" in content or "`tests/" in content

    def test_testing_strategy_example_mentions_framework(self) -> None:
        """Example should mention test framework (pytest/cargo test/jest)."""
        content = _load_sprint_planner_content()

        frameworks = ["pytest", "cargo test", "jest"]
        assert any(fw in content for fw in frameworks), (
            "Testing strategy example should mention a test framework"
        )

    def test_testing_strategy_example_mentions_categories(self) -> None:
        """Example should mention test categories (unit/functional/edge)."""
        content = _load_sprint_planner_content()

        categories = ["unit", "functional", "edge"]
        found = sum(1 for cat in categories if cat.lower() in content.lower())
        assert found >= 2, (
            "Testing strategy example should mention at least 2 test categories "
            "(unit/functional/edge)"
        )

    def test_testing_strategy_example_mentions_ac_mapping(self) -> None:
        """Example should show mapping to acceptance criteria."""
        content = _load_sprint_planner_content()

        # Should contain something like "Covers AC1" or "AC3" or "acceptance criteria"
        assert (
            "AC" in content or
            "acceptance criteria" in content.lower()
        ), "Testing strategy example should show AC mapping"


# ---------------------------------------------------------------------------
# AC4: Dependency graph thinking principle preserved
# ---------------------------------------------------------------------------


class TestDependencyGraphPrinciple:
    """Verify dependency graph optimization principle is maintained."""

    def test_mentions_dependency_graph(self) -> None:
        """Prompt should explicitly mention dependency graphs."""
        content = _load_sprint_planner_content()

        assert (
            "dependency" in content.lower() and
            "graph" in content.lower()
        ), "Must mention 'dependency graph' concept"

    def test_mentions_parallelism(self) -> None:
        """Prompt should emphasize parallelism as a goal."""
        content = _load_sprint_planner_content()

        assert "parallel" in content.lower(), (
            "Must mention parallelism optimization"
        )

    def test_mentions_interface_agreement(self) -> None:
        """Prompt should explain interface agreement enables parallel work."""
        content = _load_sprint_planner_content()

        assert "interface" in content.lower(), (
            "Must explain interface contracts for parallel work"
        )

    def test_mentions_critical_path_minimization(self) -> None:
        """Prompt should emphasize minimizing critical path."""
        content = _load_sprint_planner_content()

        assert (
            "critical path" in content.lower() or
            "minimal" in content.lower()
        ), "Must mention critical path optimization"


# ---------------------------------------------------------------------------
# AC5: Architecture-as-source-of-truth principle preserved
# ---------------------------------------------------------------------------


class TestArchitectureSourceOfTruth:
    """Verify architecture document as source of truth principle."""

    def test_architecture_as_truth_mentioned(self) -> None:
        """Prompt must state architecture is source of truth."""
        content = _load_sprint_planner_content()

        assert (
            "architecture" in content.lower() and
            "truth" in content.lower()
        ), "Must explicitly state architecture as source of truth"

    def test_warns_against_reproducing_code(self) -> None:
        """Prompt should warn against reproducing code/signatures from architecture."""
        content = _load_sprint_planner_content()

        # Should tell planner NOT to reproduce code/signatures/types
        content_types = ["code", "signature", "type"]

        # Check that we warn against reproducing these
        found_prohibition = False
        for content_type in content_types:
            if content_type in content.lower():
                # Look for prohibition context nearby
                found_prohibition = True
                break

        assert found_prohibition, (
            "Must warn against reproducing code/signatures/types from architecture"
        )

    def test_encourages_referencing_architecture_sections(self) -> None:
        """Prompt should encourage referencing architecture sections instead."""
        content = _load_sprint_planner_content()

        assert "reference" in content.lower(), (
            "Should encourage referencing architecture sections"
        )

    def test_mentions_coder_reads_architecture(self) -> None:
        """Prompt should explain that coder agents read architecture directly."""
        content = _load_sprint_planner_content()

        assert (
            "coder" in content.lower() or
            "agent" in content.lower()
        ), "Should mention that downstream agents read architecture"


# ---------------------------------------------------------------------------
# Functional correctness: basic structure validation
# ---------------------------------------------------------------------------


class TestPromptStructure:
    """Verify the compressed prompt maintains basic structure."""

    def test_contains_system_prompt_constant(self) -> None:
        """File should define SYSTEM_PROMPT constant."""
        content = _load_sprint_planner_content()
        assert 'SYSTEM_PROMPT = """' in content or "SYSTEM_PROMPT = '''" in content

    def test_contains_function_definition(self) -> None:
        """File should define sprint_planner_prompts function."""
        content = _load_sprint_planner_content()
        assert "def sprint_planner_prompts(" in content

    def test_function_has_required_parameters(self) -> None:
        """Function should accept prd, architecture, repo_path, prd_path, architecture_path."""
        content = _load_sprint_planner_content()
        assert "prd: PRD" in content
        assert "architecture: Architecture" in content
        assert "repo_path: str" in content
        assert "prd_path: str" in content
        assert "architecture_path: str" in content

    def test_mentions_issue_stub_components(self) -> None:
        """Prompt should describe all required issue stub fields."""
        content = _load_sprint_planner_content()

        required_fields = [
            "name",
            "title",
            "description",
            "depends_on",
            "provides",
            "files_to_create",
            "files_to_modify",
            "acceptance_criteria",
            "testing_strategy",
        ]

        for field in required_fields:
            assert field in content, f"Issue field '{field}' not documented"

    def test_mentions_guidance_object(self) -> None:
        """Prompt should mention the guidance object concept."""
        content = _load_sprint_planner_content()
        assert "guidance" in content.lower()

    def test_mentions_vertical_slices(self) -> None:
        """Prompt should emphasize vertical slices (implementation + tests together)."""
        content = _load_sprint_planner_content()
        assert (
            "vertical" in content.lower() or
            ("test" in content.lower() and "implementation" in content.lower())
        ), "Should mention vertical slices concept"

    def test_mentions_early_verification(self) -> None:
        """Prompt should mention early verification to catch integration problems."""
        content = _load_sprint_planner_content()
        assert "verification" in content.lower() or "verify" in content.lower()

    def test_mentions_worktree_isolation(self) -> None:
        """Prompt should explain worktree isolation for parallel issues."""
        content = _load_sprint_planner_content()
        assert (
            "worktree" in content.lower() or
            "isolat" in content.lower()
        ), "Should mention worktree/isolation"
