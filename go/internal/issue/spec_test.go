package issue

import (
	"strings"
	"testing"
)

func TestSlugify(t *testing.T) {
	cases := []struct{ in, want string }{
		{"Add retry helper!", "add-retry-helper"},
		{"  --Weird__ Name--  ", "weird-name"},
		{"!!!", "issue"},
		{strings.Repeat("x", 100), strings.Repeat("x", 40)},
	}
	for _, c := range cases {
		if got := Slugify(c.in); got != c.want {
			t.Errorf("Slugify(%q) = %q, want %q", c.in, got, c.want)
		}
	}
}

func TestParseSpecValidation(t *testing.T) {
	if _, err := ParseSpec(map[string]any{"title": "", "description": "d"}); err == nil {
		t.Error("empty title accepted")
	}
	if _, err := ParseSpec(map[string]any{"title": "t", "description": "   "}); err == nil {
		t.Error("blank description accepted")
	}
	if _, err := ParseSpec(map[string]any{"description": "d"}); err == nil {
		t.Error("missing title accepted")
	}
	if _, err := ParseSpec(map[string]any{"title": "t", "description": "d", "surprise": 1}); err == nil {
		t.Error("unknown field accepted (extra=forbid)")
	}
	spec, err := ParseSpec(map[string]any{"title": "t", "description": "d"})
	if err != nil {
		t.Fatalf("valid spec rejected: %v", err)
	}
	if spec.EstimatedComplexity != "small" {
		t.Errorf("EstimatedComplexity default = %q, want small", spec.EstimatedComplexity)
	}
}

func TestToPlannedIssue(t *testing.T) {
	spec, err := ParseSpec(map[string]any{
		"title":               "Add retry helper",
		"description":         "Backoff retry in utils.",
		"acceptance_criteria": []any{"retries 3 times"},
		"testing_strategy":    "pytest tests/test_utils.py",
		"needs_deeper_qa":     true,
	})
	if err != nil {
		t.Fatalf("ParseSpec: %v", err)
	}
	planned := spec.ToPlannedIssue("Use httpx, not requests.")

	if planned["name"] != "add-retry-helper" {
		t.Errorf("name = %v", planned["name"])
	}
	if planned["sequence_number"] != 1 {
		t.Errorf("sequence_number = %v", planned["sequence_number"])
	}
	desc, _ := planned["description"].(string)
	if !strings.Contains(desc, "Backoff retry") || !strings.Contains(desc, "Use httpx") {
		t.Errorf("description missing base or additional context: %q", desc)
	}
	guidance, _ := planned["guidance"].(map[string]any)
	if guidance == nil || guidance["needs_deeper_qa"] != true {
		t.Errorf("guidance.needs_deeper_qa = %v", guidance)
	}
	if guidance["testing_guidance"] != "pytest tests/test_utils.py" {
		t.Errorf("guidance.testing_guidance = %v", guidance["testing_guidance"])
	}
}

func TestToPlannedIssueSlugifiesExplicitName(t *testing.T) {
	spec, err := ParseSpec(map[string]any{
		"title": "T", "description": "d", "name": "My Fancy Name!",
	})
	if err != nil {
		t.Fatalf("ParseSpec: %v", err)
	}
	if got := spec.ToPlannedIssue("")["name"]; got != "my-fancy-name" {
		t.Errorf("name = %v, want my-fancy-name", got)
	}
}
