package schemas

import (
	"encoding/json"
	"testing"
)

func TestStrListAcceptsBareString(t *testing.T) {
	var result IssueResult
	// The incident shape: a pre-fix checkpoint serialized a bare-string
	// final_acceptance_criteria. Decoding must tolerate it.
	data := `{"issue_name":"fix-ac1","outcome":"completed","final_acceptance_criteria":"AC-1: exits 0"}`
	if err := json.Unmarshal([]byte(data), &result); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(result.FinalAcceptanceCriteria) != 1 || result.FinalAcceptanceCriteria[0] != "AC-1: exits 0" {
		t.Errorf("FinalAcceptanceCriteria = %v", result.FinalAcceptanceCriteria)
	}
}

func TestStrListAcceptsListAndBlank(t *testing.T) {
	var s StrList
	if err := json.Unmarshal([]byte(`["a","b"]`), &s); err != nil || len(s) != 2 {
		t.Fatalf("list decode: %v %v", s, err)
	}
	if err := json.Unmarshal([]byte(`"  "`), &s); err != nil || len(s) != 0 {
		t.Fatalf("blank decode: %v %v", s, err)
	}
	// Marshals as a plain array (checkpoint format unchanged).
	out, err := json.Marshal(StrList{"x"})
	if err != nil || string(out) != `["x"]` {
		t.Fatalf("marshal: %s %v", out, err)
	}
}

func TestSplitIssueSpecToleratesBareStrings(t *testing.T) {
	var spec SplitIssueSpec
	data := `{"name":"s","title":"t","description":"d","acceptance_criteria":"only criterion","files_to_modify":"one.py"}`
	if err := json.Unmarshal([]byte(data), &spec); err != nil {
		t.Fatalf("unmarshal: %v", err)
	}
	if len(spec.AcceptanceCriteria) != 1 || spec.AcceptanceCriteria[0] != "only criterion" {
		t.Errorf("AcceptanceCriteria = %v", spec.AcceptanceCriteria)
	}
	if len(spec.FilesToModify) != 1 || spec.FilesToModify[0] != "one.py" {
		t.Errorf("FilesToModify = %v", spec.FilesToModify)
	}
}
