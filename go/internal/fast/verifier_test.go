package fast

import (
	"context"
	"errors"
	"strings"
	"testing"
)

func verifyDeps(fn func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error)) (*Deps, *callScripter) {
	s := &callScripter{fn: fn}
	return &Deps{Call: s.call, Note: &noteRecorder{}, NodeID: "swe-fast-go"}, s
}

var verifyInput = map[string]any{
	"prd":             map[string]any{"validated_description": "Build a CLI tool"},
	"repo_path":       "/tmp/repo",
	"task_results":    []any{map[string]any{"task_name": "init", "outcome": "completed"}},
	"verifier_model":  "haiku",
	"permission_mode": "default",
	"ai_provider":     "claude",
	"artifacts_dir":   "/tmp/artifacts",
}

// Contract: a successful run_verifier result propagates passed/summary and the
// exact FastVerificationResult key set.
func TestFastVerify_Success(t *testing.T) {
	deps, s := verifyDeps(func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
		return map[string]any{
			"passed":           true,
			"summary":          "All checks passed",
			"criteria_results": []any{map[string]any{"criterion": "Tests pass", "passed": true}},
			"suggested_fixes":  []any{},
		}, nil
	})
	out, err := FastVerify(context.Background(), deps, verifyInput)
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	m := asMap(t, out)
	if m["passed"] != true {
		t.Errorf("passed = %v, want true", m["passed"])
	}
	if m["summary"] != "All checks passed" {
		t.Errorf("summary = %v", m["summary"])
	}
	wantKeys := map[string]bool{"passed": true, "summary": true, "criteria_results": true, "suggested_fixes": true}
	if len(m) != len(wantKeys) {
		t.Errorf("key set = %v, want exactly %v", keysOf(m), keysOf(mapFromSet(wantKeys)))
	}
	for k := range wantKeys {
		if _, ok := m[k]; !ok {
			t.Errorf("missing key %q", k)
		}
	}
	// task_results split into completed/failed before forwarding.
	rec := s.snapshot()
	if got := rec[0].target; got != "swe-fast-go.run_verifier" {
		t.Errorf("target = %q, want swe-fast.run_verifier", got)
	}
	ci := rec[0].kwargs["completed_issues"].([]map[string]any)
	if len(ci) != 1 || ci[0]["issue_name"] != "init" {
		t.Errorf("completed_issues = %v, want one 'init'", ci)
	}
}

// Contract: an error from the verification agent yields a safe fallback
// (passed=false) whose summary contains 'Verification agent failed' and the
// underlying error text, with empty criteria/fixes.
func TestFastVerify_ExceptionFallback(t *testing.T) {
	deps, _ := verifyDeps(func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
		return nil, errors.New("Connection refused")
	})
	out, err := FastVerify(context.Background(), deps, verifyInput)
	if err != nil {
		t.Fatalf("FastVerify propagated error: %v", err)
	}
	m := asMap(t, out)
	if m["passed"] != false {
		t.Errorf("passed = %v, want false", m["passed"])
	}
	summary := m["summary"].(string)
	if !strings.Contains(summary, "Verification agent failed") || !strings.Contains(summary, "Connection refused") {
		t.Errorf("summary = %q, want to contain both markers", summary)
	}
	if cr := m["criteria_results"].([]any); len(cr) != 0 {
		t.Errorf("criteria_results = %v, want []", cr)
	}
	if sf := m["suggested_fixes"].([]any); len(sf) != 0 {
		t.Errorf("suggested_fixes = %v, want []", sf)
	}
}

// Contract: empty task_results still forwards (completed_issues=[], failed=[]).
func TestFastVerify_EmptyTaskResults(t *testing.T) {
	deps, s := verifyDeps(func(_ context.Context, _ string, _ map[string]any) (map[string]any, error) {
		return map[string]any{"passed": true, "summary": "Nothing to verify",
			"criteria_results": []any{}, "suggested_fixes": []any{}}, nil
	})
	in := map[string]any{}
	for k, v := range verifyInput {
		in[k] = v
	}
	in["task_results"] = []any{}

	out, err := FastVerify(context.Background(), deps, in)
	if err != nil {
		t.Fatalf("error: %v", err)
	}
	if asMap(t, out)["passed"] != true {
		t.Error("passed = false, want true")
	}
	if n := s.count(); n != 1 {
		t.Fatalf("run_verifier called %d times, want 1", n)
	}
	vrec := s.snapshot()
	ci := vrec[0].kwargs["completed_issues"].([]map[string]any)
	fi := vrec[0].kwargs["failed_issues"].([]map[string]any)
	if len(ci) != 0 || len(fi) != 0 {
		t.Errorf("completed/failed = %v/%v, want empty", ci, fi)
	}
}

func keysOf(m map[string]any) []string {
	out := make([]string, 0, len(m))
	for k := range m {
		out = append(out, k)
	}
	return out
}

func mapFromSet(s map[string]bool) map[string]any {
	m := map[string]any{}
	for k := range s {
		m[k] = nil
	}
	return m
}
