package schemas

import "testing"

// defaultedFields mirrors defaults.go: the fields that carry a NON-ZERO pydantic
// default (seeded by a defaultXxx constructor + UnmarshalJSON). A field with a
// default is NOT pydantic-required, so none of these may appear in
// pydanticRequired for its type. This is the cross-check the Fix-3 spec asks for.
var defaultedFields = map[string][]string{
	"ReviewResult":         {"complexity_assessment"},
	"IssueGuidance":        {"needs_new_tests", "estimated_scope"},
	"PlannedIssue":         {"estimated_complexity"},
	"RepoSpec":             {"create_pr"},
	"WorkspaceRepo":        {"create_pr"},
	"IssueAdaptation":      {"severity"},
	"IssueAdvisorDecision": {"confidence", "debt_severity"},
	"IssueResult":          {"attempts"},
	"DAGState":             {"max_replans"},
	"RetryAdvice":          {"confidence"},
	"CoderResult":          {"complete"},
	"FastTask":             {"estimated_minutes"},
	"AskUserForm":          {"submit_label"},
}

// TestPydanticRequiredExcludesDefaultedFields cross-checks pydanticRequired
// against defaults.go: a field seeded with a non-zero default must never be in
// the required set (pydantic requires only no-default fields).
func TestPydanticRequiredExcludesDefaultedFields(t *testing.T) {
	for typeName, fields := range defaultedFields {
		req := pydanticRequired[typeName]
		reqSet := map[string]bool{}
		for _, f := range req {
			reqSet[f] = true
		}
		for _, f := range fields {
			if reqSet[f] {
				t.Errorf("%s: defaulted field %q must NOT be in pydanticRequired (has a default)", typeName, f)
			}
		}
	}
}

// TestMakePydanticFaithfulTransforms exercises the transformer directly on a
// hand-built invopop-shaped schema: required is replaced, additionalProperties
// relaxed, a map field and a $ref field made nullable.
func TestMakePydanticFaithfulTransforms(t *testing.T) {
	schema := map[string]any{
		"type":                 "object",
		"additionalProperties": false,
		"required":             []any{"action", "confidence", "ask_user_form", "sub_issues"},
		"properties": map[string]any{
			"action":        map[string]any{"type": "string"},
			"confidence":    map[string]any{"type": "number"},
			"ask_user_form": map[string]any{"$ref": "#/$defs/AskUserForm"},
			"agent_map":     map[string]any{"type": "object"},
			"sub_issues":    map[string]any{"type": "array"},
		},
	}
	// Pretend this is IssueAdvisorDecision (ask_user_form is its nullable field).
	MakePydanticFaithful(schema, "IssueAdvisorDecision")

	if v, ok := schema["additionalProperties"]; ok {
		if b, _ := v.(bool); b == false {
			t.Error("additionalProperties:false not relaxed")
		}
	}
	req := map[string]bool{}
	if arr, ok := schema["required"].([]any); ok {
		for _, e := range arr {
			req[e.(string)] = true
		}
	}
	if !req["action"] || req["confidence"] || req["ask_user_form"] {
		t.Errorf("required not faithful: %v", schema["required"])
	}
	props := schema["properties"].(map[string]any)
	if _, ok := props["ask_user_form"].(map[string]any)["anyOf"]; !ok {
		t.Error("ask_user_form ($ref) not made nullable via anyOf")
	}
	// A bare-object map field is made nullable by the general map rule.
	if !hasNullType(props["agent_map"].(map[string]any)["type"]) {
		t.Errorf("agent_map (map) not made nullable: %v", props["agent_map"])
	}
}

func hasNullType(t any) bool {
	if arr, ok := t.([]any); ok {
		for _, e := range arr {
			if e == "null" {
				return true
			}
		}
	}
	return false
}
