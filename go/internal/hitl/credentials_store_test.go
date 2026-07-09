package hitl

import (
	"fmt"
	"sync"
	"testing"
)

// Contract: stored creds override base env on inject.
func TestInjectScopedCredsOverrideBase(t *testing.T) {
	runID := "run-override"
	t.Cleanup(func() { ClearScopedCredentials(runID) })

	StoreScopedCredentials(runID, map[string]string{
		"RAILWAY_TOKEN": "fresh",
		"NEW_TOKEN":     "added",
	})

	base := map[string]string{
		"RAILWAY_TOKEN": "stale",
		"PATH":          "/usr/bin",
	}
	merged := InjectCredentialsIntoEnv(base, runID)

	if merged["RAILWAY_TOKEN"] != "fresh" {
		t.Errorf("scoped cred should override base: got %q, want %q", merged["RAILWAY_TOKEN"], "fresh")
	}
	if merged["NEW_TOKEN"] != "added" {
		t.Errorf("scoped-only cred missing: got %q, want %q", merged["NEW_TOKEN"], "added")
	}
	if merged["PATH"] != "/usr/bin" {
		t.Errorf("base-only key should pass through: got %q, want %q", merged["PATH"], "/usr/bin")
	}
	// A new map must be returned; base must not be mutated.
	if base["RAILWAY_TOKEN"] != "stale" {
		t.Errorf("base env was mutated: got %q, want %q", base["RAILWAY_TOKEN"], "stale")
	}
}

// Contract: clear removes stored creds; inject then falls back to base.
func TestClearRemovesScopedCreds(t *testing.T) {
	runID := "run-clear"
	StoreScopedCredentials(runID, map[string]string{"RAILWAY_TOKEN": "fresh"})

	if got := GetScopedCredentials(runID); got["RAILWAY_TOKEN"] != "fresh" {
		t.Fatalf("precondition: expected stored cred, got %v", got)
	}

	ClearScopedCredentials(runID)

	if got := GetScopedCredentials(runID); len(got) != 0 {
		t.Errorf("after clear, expected empty creds, got %v", got)
	}
	base := map[string]string{"RAILWAY_TOKEN": "stale"}
	merged := InjectCredentialsIntoEnv(base, runID)
	if merged["RAILWAY_TOKEN"] != "stale" {
		t.Errorf("after clear, base should be unchanged: got %q, want %q", merged["RAILWAY_TOKEN"], "stale")
	}
}

// Contract: unknown run_id → base unchanged.
func TestInjectUnknownRunIDReturnsBaseCopy(t *testing.T) {
	base := map[string]string{"PATH": "/usr/bin", "HOME": "/root"}
	merged := InjectCredentialsIntoEnv(base, "run-never-stored")

	if len(merged) != len(base) {
		t.Fatalf("unknown run_id changed key count: got %d, want %d", len(merged), len(base))
	}
	for k, v := range base {
		if merged[k] != v {
			t.Errorf("unknown run_id altered %q: got %q, want %q", k, merged[k], v)
		}
	}
	// Returned map must be independent of base.
	merged["PATH"] = "/mutated"
	if base["PATH"] != "/usr/bin" {
		t.Errorf("mutating merged leaked into base: got %q", base["PATH"])
	}
}

// Store must filter empty/whitespace-only values (parity with Python filter).
func TestStoreFiltersEmptyValues(t *testing.T) {
	runID := "run-filter"
	t.Cleanup(func() { ClearScopedCredentials(runID) })

	StoreScopedCredentials(runID, map[string]string{
		"GOOD":  "value",
		"EMPTY": "",
		"BLANK": "   ",
	})
	got := GetScopedCredentials(runID)
	if got["GOOD"] != "value" {
		t.Errorf("non-empty value dropped: got %q", got["GOOD"])
	}
	if _, ok := got["EMPTY"]; ok {
		t.Errorf("empty value should be filtered out")
	}
	if _, ok := got["BLANK"]; ok {
		t.Errorf("whitespace-only value should be filtered out")
	}
}

// Store with no surviving values removes an existing entry.
func TestStoreAllEmptyRemovesEntry(t *testing.T) {
	runID := "run-allempty"
	t.Cleanup(func() { ClearScopedCredentials(runID) })

	StoreScopedCredentials(runID, map[string]string{"TOKEN": "real"})
	StoreScopedCredentials(runID, map[string]string{"TOKEN": "  "})

	if got := GetScopedCredentials(runID); len(got) != 0 {
		t.Errorf("all-empty store should remove entry, got %v", got)
	}
}

// Get returns a copy — mutating it must not corrupt the store.
func TestGetReturnsCopy(t *testing.T) {
	runID := "run-copy"
	t.Cleanup(func() { ClearScopedCredentials(runID) })

	StoreScopedCredentials(runID, map[string]string{"TOKEN": "orig"})
	got := GetScopedCredentials(runID)
	got["TOKEN"] = "tampered"
	got["EXTRA"] = "x"

	fresh := GetScopedCredentials(runID)
	if fresh["TOKEN"] != "orig" {
		t.Errorf("store mutated via returned copy: got %q, want %q", fresh["TOKEN"], "orig")
	}
	if _, ok := fresh["EXTRA"]; ok {
		t.Errorf("added key leaked into store")
	}
}

// Empty runID is a no-op for every operation.
func TestEmptyRunIDIsNoOp(t *testing.T) {
	StoreScopedCredentials("", map[string]string{"TOKEN": "x"})
	if got := GetScopedCredentials(""); len(got) != 0 {
		t.Errorf("empty runID Get should be empty, got %v", got)
	}
	// Should not panic.
	ClearScopedCredentials("")
	base := map[string]string{"A": "b"}
	if merged := InjectCredentialsIntoEnv(base, ""); merged["A"] != "b" {
		t.Errorf("empty runID inject should pass base through, got %v", merged)
	}
}

// Contract: concurrent access is safe (run with -race).
func TestConcurrentAccessSafe(t *testing.T) {
	var wg sync.WaitGroup
	for i := 0; i < 50; i++ {
		wg.Add(1)
		go func(n int) {
			defer wg.Done()
			runID := fmt.Sprintf("run-%d", n%8)
			StoreScopedCredentials(runID, map[string]string{"TOKEN": fmt.Sprintf("v%d", n)})
			_ = GetScopedCredentials(runID)
			_ = InjectCredentialsIntoEnv(map[string]string{"BASE": "b"}, runID)
			if n%3 == 0 {
				ClearScopedCredentials(runID)
			}
		}(i)
	}
	wg.Wait()
	for i := 0; i < 8; i++ {
		ClearScopedCredentials(fmt.Sprintf("run-%d", i))
	}
}
