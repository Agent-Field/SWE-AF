package schemas

import (
	"encoding/json"
	"strings"
	"testing"
)

// dagStateListKeys enumerates every list field on DAGState by its JSON name.
// Python pydantic rejects a present-but-null value on each of these.
var dagStateListKeys = []string{
	"all_issues", "levels", "completed_issues", "failed_issues", "skipped_issues",
	"in_flight_issues", "replan_history", "pending_merge_branches", "merged_branches",
	"unmerged_branches", "merge_results", "integration_test_results",
	"accumulated_debt", "adaptation_history",
}

// TestDAGStateMarshalNoNullLists is the Fix-1 contract: a zero-value DAGState
// marshals with EVERY list field as [] (never null), while the dict|None
// workspace_manifest legitimately stays null.
func TestDAGStateMarshalNoNullLists(t *testing.T) {
	b, err := json.Marshal(DAGState{})
	if err != nil {
		t.Fatalf("marshal DAGState{}: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}

	for _, key := range dagStateListKeys {
		raw, ok := m[key]
		if !ok {
			t.Errorf("key %q missing from serialised DAGState", key)
			continue
		}
		s := strings.TrimSpace(string(raw))
		if s == "null" {
			t.Errorf("key %q serialised as null, want []", key)
		}
		if s != "[]" {
			t.Errorf("key %q = %s, want []", key, s)
		}
	}

	// The map exception: workspace_manifest is dict|None and MUST stay null.
	if got := strings.TrimSpace(string(m["workspace_manifest"])); got != "null" {
		t.Errorf("workspace_manifest = %s, want null (map exception)", got)
	}

	// Belt-and-suspenders: no list key name appears with a null value anywhere.
	for _, key := range dagStateListKeys {
		if strings.Contains(string(b), `"`+key+`":null`) {
			t.Errorf("serialised output contains %q:null", key)
		}
	}
}

// TestEmptyForNilSlicesLeavesMapsAndNilPointers guards the walker's exceptions:
// nil maps and nil pointers are untouched, nil slices become [].
func TestEmptyForNilSlicesLeavesMapsAndNilPointers(t *testing.T) {
	// IssueResult has a nil slice (files_changed), a nil map slice (debt_items),
	// and a *[]SplitIssueSpec pointer (split_request) that must stay null.
	ir := IssueResult{IssueName: "i", Outcome: IssueOutcomeCompleted}
	EmptyForNilSlices(&ir)
	if ir.FilesChanged == nil {
		t.Error("FilesChanged nil slice not normalised to empty")
	}
	if ir.DebtItems == nil {
		t.Error("DebtItems nil slice not normalised to empty")
	}
	if ir.SplitRequest != nil {
		t.Error("SplitRequest nil pointer must stay nil (Optional -> null)")
	}

	// CoderResult: tests_passed (*bool) stays nil; agent_retro (map) stays nil.
	cr := CoderResult{Summary: "x"}
	EmptyForNilSlices(&cr)
	if cr.FilesChanged == nil || cr.CodebaseLearnings == nil {
		t.Error("CoderResult nil slices not normalised")
	}
	if cr.TestsPassed != nil {
		t.Error("CoderResult.TestsPassed nil pointer must stay nil")
	}
	if cr.AgentRetro != nil {
		t.Error("CoderResult.AgentRetro nil map must stay nil (map exception)")
	}

	// Marshal check: tests_passed/agent_retro null, files_changed [].
	b, _ := json.Marshal(cr)
	js := string(b)
	if !strings.Contains(js, `"tests_passed":null`) {
		t.Errorf("tests_passed should be null: %s", js)
	}
	if !strings.Contains(js, `"files_changed":[]`) {
		t.Errorf("files_changed should be []: %s", js)
	}
}
