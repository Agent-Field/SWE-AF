"""Tests for the trivial field in IssueGuidance schema.

Covers:
- AC1: IssueGuidance.trivial field exists with type bool and default False
- AC2: Field documented with triviality criteria
- AC3: PlannedIssue schema validation passes with trivial field
- AC4: Backward compatibility - existing plans without trivial field default to False
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from swe_af.reasoners.schemas import IssueGuidance, PlannedIssue


class TestIssueGuidanceTrivialField:
    """Tests for IssueGuidance.trivial field (AC1, AC2)."""

    def test_trivial_field_exists(self) -> None:
        """IssueGuidance should have a trivial field."""
        guidance = IssueGuidance()
        assert hasattr(guidance, "trivial"), "IssueGuidance must have a 'trivial' field"

    def test_trivial_default_is_false(self) -> None:
        """IssueGuidance.trivial should default to False (AC1)."""
        guidance = IssueGuidance()
        assert guidance.trivial is False, "trivial field must default to False"

    def test_trivial_field_is_bool(self) -> None:
        """IssueGuidance.trivial should be a bool type (AC1)."""
        guidance = IssueGuidance(trivial=True)
        assert isinstance(guidance.trivial, bool), "trivial field must be bool type"
        assert guidance.trivial is True

    def test_trivial_accepts_false(self) -> None:
        """IssueGuidance.trivial should accept False explicitly."""
        guidance = IssueGuidance(trivial=False)
        assert guidance.trivial is False

    def test_trivial_rejects_non_bool(self) -> None:
        """IssueGuidance.trivial should reject non-bool values."""
        with pytest.raises(ValidationError):
            IssueGuidance(trivial="not_a_bool")  # type: ignore[arg-type]

    def test_trivial_field_documented(self) -> None:
        """Verify that triviality criteria are documented in the schema file (AC2).

        This test reads the schema file source to verify documentation exists.
        The documentation should mention key criteria:
        - acceptance criteria count
        - dependencies
        - file count
        - keywords (config, README, comment, doc, rename)
        """
        import inspect
        import swe_af.reasoners.schemas as schemas_module

        # Get the source file path
        source_file = inspect.getsourcefile(schemas_module)
        assert source_file is not None, "Could not find schemas.py source file"

        # Read the source
        with open(source_file, "r", encoding="utf-8") as f:
            source = f.read()

        # Verify documentation mentions triviality criteria
        # Look for comments or docstrings near the trivial field
        assert "trivial" in source.lower(), "Schema should mention 'trivial' field"

        # Check for key criteria terms in proximity to trivial field
        # (within the IssueGuidance class definition)
        guidance_class_start = source.find("class IssueGuidance")
        guidance_class_section = source[guidance_class_start:guidance_class_start + 2000]

        # Verify criteria are documented
        assert "acceptance criteria" in guidance_class_section.lower(), \
            "Documentation should mention acceptance criteria"
        assert "dependencies" in guidance_class_section.lower() or "depends_on" in guidance_class_section.lower(), \
            "Documentation should mention dependencies"
        assert "files" in guidance_class_section.lower(), \
            "Documentation should mention file count"
        assert any(kw in guidance_class_section.lower() for kw in ["config", "readme", "comment", "doc"]), \
            "Documentation should mention keywords (config, README, comment, doc)"


class TestPlannedIssueTrivialField:
    """Tests for PlannedIssue with trivial field in guidance (AC3)."""

    def test_planned_issue_with_trivial_true(self) -> None:
        """PlannedIssue should validate with guidance.trivial=True (AC3)."""
        issue = PlannedIssue(
            name="test-issue",
            title="Test Issue",
            description="A test issue",
            acceptance_criteria=["AC1"],
            guidance=IssueGuidance(trivial=True),
        )
        assert issue.guidance is not None
        assert issue.guidance.trivial is True

    def test_planned_issue_with_trivial_false(self) -> None:
        """PlannedIssue should validate with guidance.trivial=False (AC3)."""
        issue = PlannedIssue(
            name="test-issue",
            title="Test Issue",
            description="A test issue",
            acceptance_criteria=["AC1"],
            guidance=IssueGuidance(trivial=False),
        )
        assert issue.guidance is not None
        assert issue.guidance.trivial is False

    def test_planned_issue_with_default_trivial(self) -> None:
        """PlannedIssue with guidance but no trivial specified should default to False (AC3)."""
        issue = PlannedIssue(
            name="test-issue",
            title="Test Issue",
            description="A test issue",
            acceptance_criteria=["AC1"],
            guidance=IssueGuidance(),  # No trivial specified
        )
        assert issue.guidance is not None
        assert issue.guidance.trivial is False

    def test_planned_issue_serialization_with_trivial(self) -> None:
        """PlannedIssue should serialize and deserialize with trivial field (AC3)."""
        original_issue = PlannedIssue(
            name="test-issue",
            title="Test Issue",
            description="A test issue",
            acceptance_criteria=["AC1"],
            guidance=IssueGuidance(trivial=True, needs_deeper_qa=True),
        )

        # Serialize to dict
        issue_dict = original_issue.model_dump()
        assert issue_dict["guidance"]["trivial"] is True

        # Deserialize back
        restored_issue = PlannedIssue(**issue_dict)
        assert restored_issue.guidance is not None
        assert restored_issue.guidance.trivial is True
        assert restored_issue.guidance.needs_deeper_qa is True


class TestBackwardCompatibility:
    """Tests for backward compatibility with existing plans (AC4)."""

    def test_issue_guidance_without_trivial_field(self) -> None:
        """IssueGuidance created from dict without 'trivial' key should default to False (AC4)."""
        # Simulate an old plan that doesn't have the trivial field
        old_guidance_dict = {
            "needs_new_tests": True,
            "estimated_scope": "medium",
            "touches_interfaces": False,
            "needs_deeper_qa": False,
            "testing_guidance": "Run pytest",
            "review_focus": "",
            "risk_rationale": "",
        }

        # Pydantic should fill in the default
        guidance = IssueGuidance(**old_guidance_dict)
        assert guidance.trivial is False, "Missing trivial field should default to False"

    def test_planned_issue_without_trivial_in_guidance(self) -> None:
        """PlannedIssue from old plan without trivial field should work (AC4)."""
        old_issue_dict = {
            "name": "old-issue",
            "title": "Old Issue",
            "description": "An issue from an old plan",
            "acceptance_criteria": ["AC1", "AC2"],
            "depends_on": [],
            "provides": ["feature-x"],
            "estimated_complexity": "medium",
            "files_to_create": ["file.py"],
            "files_to_modify": [],
            "testing_strategy": "pytest",
            "sequence_number": 1,
            "guidance": {
                "needs_new_tests": True,
                "estimated_scope": "medium",
                "touches_interfaces": False,
                "needs_deeper_qa": False,
                # NOTE: no 'trivial' field here - simulating old plan
            },
        }

        # Should deserialize successfully with trivial defaulting to False
        issue = PlannedIssue(**old_issue_dict)
        assert issue.guidance is not None
        assert issue.guidance.trivial is False, "Old plans should default trivial to False"

    def test_planned_issue_without_guidance(self) -> None:
        """PlannedIssue without guidance field should still work (AC4)."""
        issue = PlannedIssue(
            name="no-guidance-issue",
            title="Issue Without Guidance",
            description="An issue without guidance",
            acceptance_criteria=["AC1"],
        )
        assert issue.guidance is None, "guidance can be None for backward compatibility"

    def test_guidance_serialization_includes_trivial(self) -> None:
        """When serializing IssueGuidance, trivial field should be included (AC4)."""
        guidance = IssueGuidance(
            needs_new_tests=False,
            estimated_scope="small",
            trivial=True,
        )

        guidance_dict = guidance.model_dump()
        assert "trivial" in guidance_dict, "Serialized guidance must include trivial field"
        assert guidance_dict["trivial"] is True

    def test_guidance_serialization_includes_trivial_when_false(self) -> None:
        """Trivial field should be serialized even when False (AC4)."""
        guidance = IssueGuidance()  # All defaults

        guidance_dict = guidance.model_dump()
        assert "trivial" in guidance_dict, "Serialized guidance must include trivial field"
        assert guidance_dict["trivial"] is False


class TestTrivialFieldIntegration:
    """Integration tests for trivial field in realistic scenarios."""

    def test_trivial_issue_example(self) -> None:
        """Example of a trivial issue (config change)."""
        issue = PlannedIssue(
            name="update-config",
            title="Update configuration timeout",
            description="Change timeout from 30s to 60s in config.yaml",
            acceptance_criteria=["AC1: timeout value is 60s"],
            depends_on=[],
            files_to_modify=["config.yaml"],
            guidance=IssueGuidance(
                trivial=True,
                estimated_scope="trivial",
                needs_new_tests=False,
                testing_guidance="Verify config value after change",
            ),
        )

        assert issue.guidance.trivial is True
        assert len(issue.acceptance_criteria) == 1
        assert len(issue.depends_on) == 0
        assert len(issue.files_to_create) + len(issue.files_to_modify) == 1

    def test_non_trivial_issue_example(self) -> None:
        """Example of a non-trivial issue (feature implementation)."""
        issue = PlannedIssue(
            name="add-authentication",
            title="Implement user authentication",
            description="Add JWT-based authentication to the API",
            acceptance_criteria=[
                "AC1: Users can log in with email/password",
                "AC2: JWT tokens are issued on successful login",
                "AC3: Protected endpoints validate tokens",
            ],
            depends_on=["database-schema"],
            files_to_create=["auth.py", "middleware.py"],
            files_to_modify=["api.py", "config.py"],
            guidance=IssueGuidance(
                trivial=False,  # Not trivial - complex feature
                estimated_scope="large",
                needs_deeper_qa=True,
                touches_interfaces=True,
                testing_guidance="Unit tests for auth logic, integration tests for endpoints",
            ),
        )

        assert issue.guidance.trivial is False
        assert len(issue.acceptance_criteria) > 2
        assert len(issue.depends_on) > 0
        assert len(issue.files_to_create) + len(issue.files_to_modify) > 2
