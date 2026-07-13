package dag

import (
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// TestSaveCheckpointHasNoNullLists is the Fix-1 checkpoint contract: the
// checkpoint.json written by saveCheckpoint (the initial write at
// executor.go:161 uses a state whose ~14 list fields are nil) serialises every
// list field as [], so a Python pydantic DAGState can re-load it. The dict|None
// workspace_manifest stays null.
func TestSaveCheckpointHasNoNullLists(t *testing.T) {
	dir := t.TempDir()
	// initDAGState mirrors the initial checkpoint's state: only AllIssues +
	// Levels are seeded, every other list field is nil.
	state := initDAGState(map[string]any{"artifacts_dir": dir}, "/repo", nil, "build-1")

	saveCheckpoint(state, nil)

	path := filepath.Join(dir, "execution", "checkpoint.json")
	b, err := os.ReadFile(path)
	if err != nil {
		t.Fatalf("read checkpoint: %v", err)
	}

	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal checkpoint: %v", err)
	}

	listKeys := []string{
		"all_issues", "levels", "completed_issues", "failed_issues", "skipped_issues",
		"in_flight_issues", "replan_history", "pending_merge_branches", "merged_branches",
		"unmerged_branches", "merge_results", "integration_test_results",
		"accumulated_debt", "adaptation_history",
	}
	for _, key := range listKeys {
		raw, ok := m[key]
		if !ok {
			t.Errorf("checkpoint missing key %q", key)
			continue
		}
		if s := strings.TrimSpace(string(raw)); s != "[]" {
			t.Errorf("checkpoint key %q = %s, want []", key, s)
		}
	}
	if got := strings.TrimSpace(string(m["workspace_manifest"])); got != "null" {
		t.Errorf("workspace_manifest = %s, want null", got)
	}

	// Round-trips back through loadCheckpoint without error (seeds max_replans=2).
	loaded := loadCheckpoint(dir)
	if loaded == nil {
		t.Fatal("loadCheckpoint returned nil")
	}
	if loaded.MaxReplans != 2 {
		t.Errorf("max_replans = %d, want 2 (seeded)", loaded.MaxReplans)
	}
	_ = schemas.DAGState{} // keep the schemas import explicit for clarity
}
