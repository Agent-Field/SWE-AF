package afx

import (
	"encoding/json"
	"testing"
)

// coderResultLike mimics the schema-package pattern (design §2.2): a type whose
// UnmarshalJSON seeds a non-zero default (Complete=true) so that a key absent
// from the input keeps the Pydantic default rather than the Go zero value.
type coderResultLike struct {
	Complete    bool   `json:"complete"`
	Summary     string `json:"summary"`
	TestsPassed *bool  `json:"tests_passed"`
}

func defaultCoderResultLike() coderResultLike { return coderResultLike{Complete: true} }

func (c *coderResultLike) UnmarshalJSON(b []byte) error {
	*c = defaultCoderResultLike()
	type alias coderResultLike
	return json.Unmarshal(b, (*alias)(c))
}

type nested struct {
	Name  string          `json:"name"`
	Inner coderResultLike `json:"inner"`
}

// TestBind_DefaultSeeding covers the contract:
//   - absent key keeps the type's UnmarshalJSON default (Bind must route through it)
//   - present key overrides the default, even with the zero value (false)
//   - matching keys populate fields; extra input keys are ignored
func TestBind_DefaultSeeding(t *testing.T) {
	tests := []struct {
		name         string
		input        map[string]any
		wantComplete bool
		wantSummary  string
	}{
		{
			name:         "absent complete keeps seeded default true",
			input:        map[string]any{"summary": "did the thing"},
			wantComplete: true,
			wantSummary:  "did the thing",
		},
		{
			name:         "present complete=false overrides default",
			input:        map[string]any{"complete": false, "summary": "wip"},
			wantComplete: false,
			wantSummary:  "wip",
		},
		{
			name:         "present complete=true stays true",
			input:        map[string]any{"complete": true},
			wantComplete: true,
			wantSummary:  "",
		},
		{
			name:         "empty input keeps all defaults",
			input:        map[string]any{},
			wantComplete: true,
			wantSummary:  "",
		},
		{
			name:         "nil input keeps all defaults",
			input:        nil,
			wantComplete: true,
			wantSummary:  "",
		},
		{
			name:         "unknown keys are ignored",
			input:        map[string]any{"summary": "s", "not_a_field": 42},
			wantComplete: true,
			wantSummary:  "s",
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			got, err := Bind[coderResultLike](tt.input)
			if err != nil {
				t.Fatalf("Bind returned error: %v", err)
			}
			if got.Complete != tt.wantComplete {
				t.Errorf("Complete = %v, want %v", got.Complete, tt.wantComplete)
			}
			if got.Summary != tt.wantSummary {
				t.Errorf("Summary = %q, want %q", got.Summary, tt.wantSummary)
			}
		})
	}
}

// TestBind_PointerTriState covers *bool tri-state fields: absent -> nil,
// present -> non-nil pointing at the given value (design §2.1).
func TestBind_PointerTriState(t *testing.T) {
	absent, err := Bind[coderResultLike](map[string]any{})
	if err != nil {
		t.Fatalf("Bind(absent) error: %v", err)
	}
	if absent.TestsPassed != nil {
		t.Errorf("TestsPassed = %v, want nil when absent", *absent.TestsPassed)
	}

	present, err := Bind[coderResultLike](map[string]any{"tests_passed": false})
	if err != nil {
		t.Fatalf("Bind(present) error: %v", err)
	}
	if present.TestsPassed == nil {
		t.Fatalf("TestsPassed = nil, want non-nil pointer to false")
	}
	if *present.TestsPassed != false {
		t.Errorf("*TestsPassed = %v, want false", *present.TestsPassed)
	}
}

// TestBind_Nested covers a nested struct field binding, including the nested
// type's own default-seeding.
func TestBind_Nested(t *testing.T) {
	got, err := Bind[nested](map[string]any{
		"name":  "issue-1",
		"inner": map[string]any{"summary": "nested summary"},
	})
	if err != nil {
		t.Fatalf("Bind error: %v", err)
	}
	if got.Name != "issue-1" {
		t.Errorf("Name = %q, want %q", got.Name, "issue-1")
	}
	if !got.Inner.Complete {
		t.Errorf("Inner.Complete = false, want true (nested default seeding)")
	}
	if got.Inner.Summary != "nested summary" {
		t.Errorf("Inner.Summary = %q, want %q", got.Inner.Summary, "nested summary")
	}
}

// TestBind_IntoMap covers binding into map[string]any (identity round-trip of
// the request body — used where a handler wants the raw shape back).
func TestBind_IntoMap(t *testing.T) {
	in := map[string]any{"a": "x", "b": float64(2)}
	got, err := Bind[map[string]any](in)
	if err != nil {
		t.Fatalf("Bind error: %v", err)
	}
	if got["a"] != "x" {
		t.Errorf("got[a] = %v, want x", got["a"])
	}
	if got["b"] != float64(2) {
		t.Errorf("got[b] = %v (%T), want float64(2)", got["b"], got["b"])
	}
}

// TestBind_TypeMismatch covers the error path: a value whose JSON type cannot
// unmarshal into the target field type returns an error, not a silent zero.
func TestBind_TypeMismatch(t *testing.T) {
	type numeric struct {
		Count int `json:"count"`
	}
	_, err := Bind[numeric](map[string]any{"count": "not-a-number"})
	if err == nil {
		t.Fatalf("Bind = nil error, want unmarshal error for string into int")
	}
}
