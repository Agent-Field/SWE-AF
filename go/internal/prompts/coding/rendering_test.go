package coding

import (
	"strings"
	"testing"
)

// These tests mirror tests/test_prompt_field_rendering.py: the coder renders
// description, retry context, approach changes, retry diagnosis, upstream debt
// and needs_new_tests; the reviewer renders estimated_scope and
// needs_new_tests; QA renders testing_guidance.

func TestCoderPromptRendersPipelineFields(t *testing.T) {
	issue := map[string]any{
		"name":                "cache-read",
		"title":               "Retry the cache read",
		"description":         "DESCRIPTION-SENTINEL: read through the cache, then the store.",
		"acceptance_criteria": []any{"Reads fall back to the store"},
		"retry_context":       "RETRY-CONTEXT-SENTINEL: look up the store before the index.",
		"approach_changes":    []any{"APPROACH-SENTINEL: drop the custom index"},
		"retry_diagnosis":     "DIAGNOSIS-SENTINEL: the index was never populated.",
		"debt_notes":          []any{"DEBT-SENTINEL: upstream left batch mode unimplemented."},
		"guidance":            map[string]any{"needs_new_tests": true},
	}

	prompt := CoderTaskPrompt(CoderTaskPromptOpts{Issue: issue, WorktreePath: "/tmp/wt"})

	for _, sentinel := range []string{
		"DESCRIPTION-SENTINEL",
		"RETRY-CONTEXT-SENTINEL",
		"APPROACH-SENTINEL",
		"DIAGNOSIS-SENTINEL",
		"DEBT-SENTINEL",
	} {
		if !strings.Contains(prompt, sentinel) {
			t.Errorf("%s did not reach the coder prompt", sentinel)
		}
	}
}

func TestCoderRetryBlockNamesNewApproach(t *testing.T) {
	prompt := CoderTaskPrompt(CoderTaskPromptOpts{
		Issue:        map[string]any{"retry_context": "use a plain list"},
		WorktreePath: "/tmp/wt",
	})

	for _, want := range []string{
		"Retry Context (from Issue Advisor)",
		"New approach",
		"use a plain list",
	} {
		if !strings.Contains(prompt, want) {
			t.Errorf("coder prompt missing %q", want)
		}
	}
}

func TestCoderNeedsNewTestsFalseLimitsToBuildAndExistingSuite(t *testing.T) {
	prompt := CoderTaskPrompt(CoderTaskPromptOpts{
		Issue:        map[string]any{"guidance": map[string]any{"needs_new_tests": false}},
		WorktreePath: "/tmp/wt",
	})

	for _, want := range []string{
		"New tests needed",
		"do NOT create or extend test files",
		"Run the build and the existing test suite only",
		"unless the issue says no new tests are needed",
	} {
		if !strings.Contains(prompt, want) {
			t.Errorf("coder prompt missing %q", want)
		}
	}
}

func TestCoderNeedsNewTestsTrueDoesNotCarrySkipInstruction(t *testing.T) {
	prompt := CoderTaskPrompt(CoderTaskPromptOpts{
		Issue:        map[string]any{"guidance": map[string]any{"needs_new_tests": true}},
		WorktreePath: "/tmp/wt",
	})

	if !strings.Contains(prompt, "New tests needed") {
		t.Error("coder prompt missing the New tests needed block")
	}
	if strings.Contains(prompt, "do NOT create or extend test files") {
		t.Error("needs_new_tests=true must not carry the skip instruction")
	}
}

func TestReviewerPromptRendersEstimatedScope(t *testing.T) {
	prompt := CodeReviewerTaskPrompt(CodeReviewerTaskPromptOpts{
		WorktreePath: "/tmp/wt",
		CoderResult:  map[string]any{"summary": "done", "files_changed": []any{"app.py"}},
		Issue:        map[string]any{"guidance": map[string]any{"estimated_scope": "SCOPE-SENTINEL-small"}},
	})

	if !strings.Contains(prompt, "Scope and Test Expectations (from sprint planner)") {
		t.Error("reviewer prompt missing the scope section")
	}
	if !strings.Contains(prompt, "SCOPE-SENTINEL-small") {
		t.Error("estimated_scope did not reach the reviewer prompt")
	}
}

func TestReviewerNeedsNewTestsFalseForbidsDemandingTests(t *testing.T) {
	prompt := CodeReviewerTaskPrompt(CodeReviewerTaskPromptOpts{
		WorktreePath: "/tmp/wt",
		CoderResult:  map[string]any{"summary": "done", "files_changed": []any{"app.py"}},
		Issue:        map[string]any{"guidance": map[string]any{"needs_new_tests": false}},
	})

	for _, want := range []string{
		"New tests needed**: no",
		"not expected to add tests",
		"do not require new tests",
		"do not block on missing test coverage",
	} {
		if !strings.Contains(prompt, want) {
			t.Errorf("reviewer prompt missing %q", want)
		}
	}
}

func TestReviewerNeedsNewTestsTrueAsksForTestCoverage(t *testing.T) {
	prompt := CodeReviewerTaskPrompt(CodeReviewerTaskPromptOpts{
		WorktreePath: "/tmp/wt",
		CoderResult:  map[string]any{"summary": "done", "files_changed": []any{"app.py"}},
		Issue:        map[string]any{"guidance": map[string]any{"needs_new_tests": true}},
	})

	if !strings.Contains(prompt, "New tests needed**: yes") {
		t.Error("reviewer prompt missing the yes expectation")
	}
	if strings.Contains(prompt, "do not require new tests") {
		t.Error("needs_new_tests=true must not forbid requiring tests")
	}
}

func TestQAPromptRendersTestingGuidance(t *testing.T) {
	guidance := "QA-GUIDANCE-SENTINEL: extend the existing CLI test; no new files."
	prompt := QATaskPrompt(QATaskPromptOpts{
		WorktreePath: "/tmp/wt",
		CoderResult:  map[string]any{"summary": "done", "files_changed": []any{"app.py"}},
		Issue:        map[string]any{"guidance": map[string]any{"testing_guidance": guidance}},
	})

	if !strings.Contains(prompt, "Testing Guidance (from sprint planner)") {
		t.Error("QA prompt missing the testing guidance section")
	}
	if !strings.Contains(prompt, guidance) {
		t.Error("testing_guidance did not reach the QA prompt")
	}
}
