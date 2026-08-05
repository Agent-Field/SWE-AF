package dagutil

// Regression tests for LLM-emitted scalar shapes on issue data — the Go
// mirror of tests/test_llm_shape_normalization.py. See that file's docstring
// for the incident this guards against.

import (
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

func TestNormalizeIssueDictCoercesScalars(t *testing.T) {
	issue := map[string]any{
		"name":                "fix-1",
		"acceptance_criteria": "AC-1: single string",
		"depends_on":          "other-issue",
		"provides":            nil,
		"files_to_create":     "a.py",
		"files_to_modify":     []any{"b.py"},
	}
	NormalizeIssueDict(issue)

	if got, _ := issue["acceptance_criteria"].([]string); len(got) != 1 || got[0] != "AC-1: single string" {
		t.Errorf("acceptance_criteria = %v", issue["acceptance_criteria"])
	}
	if got, _ := issue["depends_on"].([]string); len(got) != 1 || got[0] != "other-issue" {
		t.Errorf("depends_on = %v", issue["depends_on"])
	}
	if got, _ := issue["provides"].([]string); got == nil || len(got) != 0 {
		t.Errorf("provides = %v", issue["provides"])
	}
	if got, _ := issue["files_to_create"].([]string); len(got) != 1 || got[0] != "a.py" {
		t.Errorf("files_to_create = %v", issue["files_to_create"])
	}
	// Already-list values keep their shape (untouched []any is fine).
	if _, ok := issue["files_to_modify"].([]any); !ok {
		t.Errorf("files_to_modify should be untouched, got %T", issue["files_to_modify"])
	}
}

func TestNormalizeIssueDictLeavesAbsentAndOddTypes(t *testing.T) {
	issue := map[string]any{"name": "fix-1", "acceptance_criteria": 42}
	NormalizeIssueDict(issue)
	if issue["acceptance_criteria"] != 42 {
		t.Errorf("non-str scalar should pass through, got %v", issue["acceptance_criteria"])
	}
	if _, ok := issue["depends_on"]; ok {
		t.Error("absent field should stay absent")
	}
}

func TestApplyReplanNormalizesLLMIssueShapes(t *testing.T) {
	state := &schemas.DAGState{
		RepoPath: "/tmp/repo",
		AllIssues: []map[string]any{
			{"name": "keep", "depends_on": []any{}, "acceptance_criteria": []any{"ok"}},
		},
		Levels: [][]string{{"keep"}},
	}
	decision := schemas.ReplanDecision{
		Action:    schemas.ReplanActionModifyDAG,
		Rationale: "r",
		UpdatedIssues: []map[string]any{
			{"name": "keep", "acceptance_criteria": "AC as string"},
		},
		NewIssues: []map[string]any{
			{"name": "new-1", "depends_on": "keep", "acceptance_criteria": "single new criterion"},
		},
	}
	state, err := ApplyReplan(state, decision)
	if err != nil {
		t.Fatalf("ApplyReplan: %v", err)
	}
	byName := map[string]map[string]any{}
	for _, i := range state.AllIssues {
		byName[asString(i["name"])] = i
	}
	if got, _ := byName["keep"]["acceptance_criteria"].([]string); len(got) != 1 || got[0] != "AC as string" {
		t.Errorf("keep.acceptance_criteria = %v", byName["keep"]["acceptance_criteria"])
	}
	if got, _ := byName["new-1"]["acceptance_criteria"].([]string); len(got) != 1 || got[0] != "single new criterion" {
		t.Errorf("new-1.acceptance_criteria = %v", byName["new-1"]["acceptance_criteria"])
	}
	if got, _ := byName["new-1"]["depends_on"].([]string); len(got) != 1 || got[0] != "keep" {
		t.Errorf("new-1.depends_on = %v", byName["new-1"]["depends_on"])
	}
}
