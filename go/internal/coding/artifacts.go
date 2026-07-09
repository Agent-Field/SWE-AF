package coding

import (
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"time"
)

// This file ports the iteration-level checkpoint helpers and per-iteration
// artifact writers from swe_af/execution/coding_loop.py (lines 42-85). Paths and
// JSON shapes are reproduced exactly so a build's on-disk layout is identical to
// the Python node's (and mid-issue resume reads the same files).

// iterationStatePath ports _iteration_state_path.
//
// With a build_id, iteration checkpoints are scoped by build_id so parallel /
// sequential builds against the same repo do not resume stale state from prior
// runs: <artifacts>/execution/iterations/<build_id>/<issue>.json. Without one:
// <artifacts>/execution/iterations/<issue>.json. Empty artifactsDir -> "".
func iterationStatePath(artifactsDir, issueName, buildID string) string {
	if artifactsDir == "" {
		return ""
	}
	if buildID != "" {
		return filepath.Join(artifactsDir, "execution", "iterations", buildID, issueName+".json")
	}
	return filepath.Join(artifactsDir, "execution", "iterations", issueName+".json")
}

// saveIterationState ports _save_iteration_state. Writes state as indent=2 JSON.
func saveIterationState(artifactsDir, issueName string, state map[string]any, buildID string) {
	path := iterationStatePath(artifactsDir, issueName, buildID)
	if path == "" {
		return
	}
	if err := os.MkdirAll(filepath.Dir(path), 0o755); err != nil {
		return
	}
	data, err := json.MarshalIndent(state, "", "  ")
	if err != nil {
		return
	}
	_ = os.WriteFile(path, data, 0o644)
}

// loadIterationState ports _load_iteration_state. Returns nil when the file is
// absent, when artifactsDir is empty, or on read/parse error.
func loadIterationState(artifactsDir, issueName, buildID string) map[string]any {
	path := iterationStatePath(artifactsDir, issueName, buildID)
	if path == "" {
		return nil
	}
	data, err := os.ReadFile(path)
	if err != nil {
		return nil
	}
	var state map[string]any
	if err := json.Unmarshal(data, &state); err != nil {
		return nil
	}
	return state
}

// saveArtifact ports _save_artifact. Saves a structured result as a JSON
// artifact under <artifacts>/coding-loop/<iteration_id>/<name>.json and returns
// the file path (or "" when artifactsDir is empty or on error).
func saveArtifact(artifactsDir, iterationID, name string, data map[string]any) string {
	if artifactsDir == "" {
		return ""
	}
	artifactDir := filepath.Join(artifactsDir, "coding-loop", iterationID)
	if err := os.MkdirAll(artifactDir, 0o755); err != nil {
		return ""
	}
	path := filepath.Join(artifactDir, name+".json")
	b, err := json.MarshalIndent(data, "", "  ")
	if err != nil {
		return ""
	}
	if err := os.WriteFile(path, b, 0o644); err != nil {
		return ""
	}
	return path
}

// newIterationID reproduces str(uuid.uuid4())[:8] — the first 8 hex characters
// of a random UUID, which is the artifact subdirectory name / agent-visible
// iteration id. crypto/rand keeps it dependency-free.
func newIterationID() string {
	b := make([]byte, 4)
	if _, err := rand.Read(b); err != nil {
		return fmt.Sprintf("%08x", time.Now().UnixNano()&0xffffffff)
	}
	return hex.EncodeToString(b)
}
