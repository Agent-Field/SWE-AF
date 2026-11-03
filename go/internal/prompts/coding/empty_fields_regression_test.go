package coding

import (
	"strings"
	"testing"
)

func TestEmptyPipelinePromptFieldsHaveNoSections(t *testing.T) {
	for _, value := range []any{nil, ""} {
		issue := map[string]any{
			"description": value, "retry_context": value, "retry_diagnosis": value,
			"approach_changes": value, "debt_notes": value,
			"guidance": map[string]any{"estimated_scope": value, "needs_new_tests": nil, "testing_guidance": value},
		}
		// testing_guidance already existed in the coder before this PR; its
		// null behavior is a separate baseline discrepancy. QA adds that field.
		coderIssue := map[string]any{}
		for key, v := range issue {
			coderIssue[key] = v
		}
		coderIssue["guidance"] = map[string]any{"estimated_scope": value, "needs_new_tests": nil}
		coder := CoderTaskPrompt(CoderTaskPromptOpts{Issue: coderIssue, WorktreePath: "/wt"})
		guidanceIssue := map[string]any{"guidance": issue["guidance"]}
		reviewer := CodeReviewerTaskPrompt(CodeReviewerTaskPromptOpts{Issue: guidanceIssue, WorktreePath: "/wt", CoderResult: map[string]any{}})
		qa := QATaskPrompt(QATaskPromptOpts{Issue: guidanceIssue, WorktreePath: "/wt", CoderResult: map[string]any{}})
		for _, forbidden := range []string{"**Description**", "## Retry Context", "## Upstream Debt Notes"} {
			if strings.Contains(coder, forbidden) {
				t.Errorf("empty value %v renders %s", value, forbidden)
			}
		}
		if strings.Contains(reviewer, "## Scope and Test Expectations") {
			t.Errorf("empty scope header for %v", value)
		}
		if strings.Contains(qa, "Testing Guidance (from sprint planner)") {
			t.Errorf("empty testing guidance for %v", value)
		}
		for _, prompt := range []string{coder, reviewer, qa} {
			if strings.Contains(prompt, ": None") {
				t.Errorf("null rendered as None: %s", prompt)
			}
		}
	}
}
