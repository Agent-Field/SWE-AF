#!/usr/bin/env python3
"""Verification script for model default assignments.

This script validates that _RUNTIME_BASE_MODELS['claude_code'] assigns:
- 'haiku' to exactly 4 models: qa_synthesizer_model, git_model, merger_model, retry_advisor_model
- 'sonnet' to all other 12 models
"""

from swe_af.execution.schemas import _RUNTIME_BASE_MODELS


def verify_model_defaults() -> None:
    """Verify model assignments in _RUNTIME_BASE_MODELS['claude_code']."""
    claude_models = _RUNTIME_BASE_MODELS["claude_code"]

    # Expected assignments
    expected_haiku = {"qa_synthesizer_model", "git_model", "merger_model", "retry_advisor_model"}
    expected_sonnet = {
        "pm_model", "architect_model", "tech_lead_model", "sprint_planner_model",
        "coder_model", "qa_model", "code_reviewer_model", "replan_model",
        "issue_writer_model", "issue_advisor_model", "verifier_model",
        "integration_tester_model"
    }

    # Verify haiku assignments
    print("Verifying haiku model assignments...")
    for role in expected_haiku:
        assert role in claude_models, f"Missing role: {role}"
        actual = claude_models[role]
        assert actual == "haiku", f"{role} should be 'haiku', got {actual!r}"
        print(f"  ✓ {role}: {actual}")

    # Verify sonnet assignments
    print("\nVerifying sonnet model assignments...")
    for role in expected_sonnet:
        assert role in claude_models, f"Missing role: {role}"
        actual = claude_models[role]
        assert actual == "sonnet", f"{role} should be 'sonnet', got {actual!r}"
        print(f"  ✓ {role}: {actual}")

    # Verify total count
    all_expected = expected_haiku | expected_sonnet
    assert len(all_expected) == 16, f"Expected 16 total models, got {len(all_expected)}"
    print(f"\n✓ All 16 models verified: 4 haiku + 12 sonnet")


if __name__ == "__main__":
    verify_model_defaults()
    print("\n✓ Verification passed!")
