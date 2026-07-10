package harnessx

import (
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// strSet turns a schema `required`/`enum` []any into a lookup set.
func strSet(v any) map[string]bool {
	out := map[string]bool{}
	if arr, ok := v.([]any); ok {
		for _, e := range arr {
			if s, ok := e.(string); ok {
				out[s] = true
			}
		}
	}
	return out
}

func props(t *testing.T, m map[string]any) map[string]any {
	t.Helper()
	p, ok := m["properties"].(map[string]any)
	if !ok {
		t.Fatalf("schema has no properties: %v", m)
	}
	return p
}

// TestSchemaForEnumsPresent maps to the Fix-3 contract: the reflected schema
// carries the enum constraint for enum-typed fields.
func TestSchemaForEnumsPresent(t *testing.T) {
	p := props(t, schemaFor[schemas.IssueAdvisorDecision]())
	action, ok := p["action"].(map[string]any)
	if !ok {
		t.Fatal("no action property")
	}
	enum := strSet(action["enum"])
	for _, want := range []string{"retry_modified", "retry_approach", "split", "accept_with_debt", "escalate_to_replan"} {
		if !enum[want] {
			t.Errorf("action enum missing %q (got %v)", want, action["enum"])
		}
	}

	// QASynthesisResult.action enum.
	qp := props(t, schemaFor[schemas.QASynthesisResult]())
	qEnum := strSet(qp["action"].(map[string]any)["enum"])
	for _, want := range []string{"fix", "approve", "block"} {
		if !qEnum[want] {
			t.Errorf("qa_synthesizer action enum missing %q", want)
		}
	}
}

// TestSchemaForRequiredIsPydanticFaithful maps to: required lists only the
// pydantic no-default fields, and does NOT list defaulted/Optional fields.
func TestSchemaForRequiredIsPydanticFaithful(t *testing.T) {
	m := schemaFor[schemas.IssueAdvisorDecision]()
	req := strSet(m["required"])
	// Required: action, failure_diagnosis, rationale.
	for _, want := range []string{"action", "failure_diagnosis", "rationale"} {
		if !req[want] {
			t.Errorf("IssueAdvisorDecision required missing %q", want)
		}
	}
	// Defaulted (defaults.go) / Optional must NOT be required.
	for _, notWant := range []string{"confidence", "debt_severity", "ask_user_form", "modified_acceptance_criteria"} {
		if req[notWant] {
			t.Errorf("IssueAdvisorDecision must NOT require defaulted/optional field %q", notWant)
		}
	}

	// CoderResult has no pydantic-required fields → no required key (or empty).
	cr := schemaFor[schemas.CoderResult]()
	if creq := strSet(cr["required"]); creq["complete"] || creq["tests_passed"] {
		t.Errorf("CoderResult must not require defaulted 'complete' / optional 'tests_passed': %v", cr["required"])
	}
}

// TestSchemaForRelaxesAdditionalProperties maps to: pydantic ignores extra keys,
// so the reflected schema must not set additionalProperties:false.
func TestSchemaForRelaxesAdditionalProperties(t *testing.T) {
	for _, m := range []map[string]any{
		schemaFor[schemas.IssueAdvisorDecision](),
		schemaFor[schemas.CoderResult](),
	} {
		if v, ok := m["additionalProperties"]; ok {
			if b, isBool := v.(bool); isBool && !b {
				t.Errorf("additionalProperties:false must be relaxed, schema=%v", m)
			}
		}
	}
	// $defs objects too (SplitIssueSpec is nested under IssueAdvisorDecision).
	m := schemaFor[schemas.IssueAdvisorDecision]()
	if defs, ok := m["$defs"].(map[string]any); ok {
		if sis, ok := defs["SplitIssueSpec"].(map[string]any); ok {
			if v, ok := sis["additionalProperties"]; ok {
				if b, isBool := v.(bool); isBool && !b {
					t.Error("nested SplitIssueSpec additionalProperties:false must be relaxed")
				}
			}
		}
	}
}

// TestSchemaForNullableAcceptsNull maps to: Optional fields (X|None) and map
// fields accept null, so a Go-serialised null does not over-reject.
func TestSchemaForNullableAcceptsNull(t *testing.T) {
	// CoderResult.tests_passed (*bool) -> type includes "null".
	cr := props(t, schemaFor[schemas.CoderResult]())
	tp := cr["tests_passed"].(map[string]any)
	if !typeAllowsNull(tp["type"]) {
		t.Errorf("tests_passed must allow null, got type=%v", tp["type"])
	}
	// CoderResult.agent_retro (map) -> type includes "null".
	ar := cr["agent_retro"].(map[string]any)
	if !typeAllowsNull(ar["type"]) {
		t.Errorf("agent_retro (map) must allow null, got type=%v", ar["type"])
	}
	// IssueAdvisorDecision.ask_user_form ($ref) -> anyOf with a null branch.
	iad := props(t, schemaFor[schemas.IssueAdvisorDecision]())
	auf := iad["ask_user_form"].(map[string]any)
	if _, ok := auf["anyOf"]; !ok {
		t.Errorf("ask_user_form must be wrapped in anyOf(null), got %v", auf)
	}
}

func typeAllowsNull(t any) bool {
	switch tv := t.(type) {
	case string:
		return tv == "null"
	case []any:
		for _, e := range tv {
			if e == "null" {
				return true
			}
		}
	}
	return false
}
