package fast

import (
	"sort"
	"testing"
)

// Contract: Wrappers exposes exactly the seven fast/__init__.py wrapper names,
// each delegating (identity) to the same-named full-pipeline role handler.
func TestWrappers_ExactNamesAndIdentityDelegation(t *testing.T) {
	want := []string{
		"run_git_init", "run_coder", "run_verifier", "run_repo_finalize",
		"run_github_pr", "run_ci_watcher", "run_ci_fixer",
	}
	w := Wrappers()
	if len(w) != len(want) {
		t.Fatalf("Wrappers has %d entries, want %d: %v", len(w), len(want), w)
	}
	for _, name := range want {
		delegate, ok := w[name]
		if !ok {
			t.Errorf("missing wrapper %q", name)
			continue
		}
		if delegate != name {
			t.Errorf("wrapper %q delegates to %q, want identity (%q)", name, delegate, name)
		}
	}
}

// Contract: WrapperNames preserves the declaration order from fast/__init__.py.
func TestWrapperNames_Order(t *testing.T) {
	want := []string{
		"run_git_init", "run_coder", "run_verifier", "run_repo_finalize",
		"run_github_pr", "run_ci_watcher", "run_ci_fixer",
	}
	got := WrapperNames()
	if len(got) != len(want) {
		t.Fatalf("WrapperNames len = %d, want %d", len(got), len(want))
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("WrapperNames[%d] = %q, want %q", i, got[i], want[i])
		}
	}
}

// Contract: Handlers exposes exactly the four first-class fast reasoner names.
func TestHandlers_ExactNames(t *testing.T) {
	want := []string{"build", "fast_execute_tasks", "fast_plan_tasks", "fast_verify"}
	h := Handlers()
	got := make([]string, 0, len(h))
	for name, fn := range h {
		if fn == nil {
			t.Errorf("handler %q is nil", name)
		}
		got = append(got, name)
	}
	sort.Strings(got)
	if len(got) != len(want) {
		t.Fatalf("Handlers names = %v, want %v", got, want)
	}
	for i := range want {
		if got[i] != want[i] {
			t.Errorf("Handlers[%d] = %q, want %q (full: %v)", i, got[i], want[i], got)
		}
	}
}
