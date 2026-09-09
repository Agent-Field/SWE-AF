//go:build functional

package functional

import (
	"encoding/json"
	"fmt"
	"net/http"
	"sort"
	"testing"
)

// ciWatchResultKeys is the exact top-level key set of the Python
// CIWatchResult.model_dump() (schemas/execution.go CIWatchResult). The API
// byte-compat contract (design constraint #3, §11(b)) requires the result JSON
// carry exactly these keys — key-set parity, not values.
var ciWatchResultKeys = []string{
	"status", "pr_number", "elapsed_seconds", "failed_checks", "summary",
}

// TestDeterministicReasonerKeySets calls the only two NO-LLM (deterministic)
// reasoners — run_ci_watcher on both nodes — through the synchronous execute
// API against a nonexistent repo path (so `gh pr checks` fails immediately and
// the poller returns status="error" without any network/LLM), then asserts the
// returned result carries exactly the CIWatchResult key set.
//
// This exercises the full HTTP surface end to end: CP sync execute -> node
// dispatch -> reasoner -> result serialization -> CP envelope. Both swe-planner
// and swe-fast register run_ci_watcher (it is one of the 25 shared roles), so
// hitting both is the "one more deterministic reasoner" coverage.
func TestDeterministicReasonerKeySets(t *testing.T) {
	requireStack(t)

	// A path that is guaranteed not to be a git repo inside the container.
	input := map[string]any{
		"repo_path":    "/nonexistent/swe-af-functional-probe",
		"pr_number":    1,
		"wait_seconds": 5,
		"poll_seconds": 1,
	}

	targets := []string{
		plannerNodeID + ".run_ci_watcher",
		fastNodeID + ".run_ci_watcher",
	}
	for _, target := range targets {
		t.Run(target, func(t *testing.T) {
			result := execSyncResult(t, target, input)
			assertExactKeys(t, target, result, ciWatchResultKeys)

			// Sanity: with a bad repo path the deterministic poller reports
			// an error status (value check is secondary to key-set parity but
			// confirms we exercised the real code path, not a stub).
			if status, _ := result["status"].(string); status != "error" {
				t.Logf("%s: status=%q (expected \"error\" for a nonexistent repo; key-set parity still asserted)", target, status)
			}
		})
	}
}

// execSyncResult POSTs a synchronous execute request and returns the decoded
// `result` object from the CP ExecuteResponse. It fails the test on transport
// errors, non-200 status, or a non-object result.
func execSyncResult(t *testing.T, target string, input map[string]any) map[string]any {
	t.Helper()
	url := fmt.Sprintf("%s/api/v1/execute/%s", cpBaseURL, target)
	body, status, err := httpPostJSON(url, map[string]any{"input": input})
	if err != nil {
		t.Fatalf("POST %s: %v", url, err)
	}
	if status != http.StatusOK {
		t.Fatalf("POST %s -> %d, want 200 (body: %s)", url, status, string(body))
	}

	var envelope struct {
		Status       string          `json:"status"`
		Result       json.RawMessage `json:"result"`
		ErrorMessage *string         `json:"error_message"`
	}
	if err := json.Unmarshal(body, &envelope); err != nil {
		t.Fatalf("decode execute response: %v (body: %s)", err, string(body))
	}
	if envelope.Status != "succeeded" {
		msg := ""
		if envelope.ErrorMessage != nil {
			msg = *envelope.ErrorMessage
		}
		t.Fatalf("%s execution status=%q, want succeeded (error: %s; body: %s)",
			target, envelope.Status, msg, string(body))
	}

	var result map[string]any
	if err := json.Unmarshal(envelope.Result, &result); err != nil {
		t.Fatalf("%s result is not a JSON object: %v (raw: %s)", target, err, string(envelope.Result))
	}
	return result
}

// assertExactKeys fails the test unless obj's top-level key set is exactly want.
func assertExactKeys(t *testing.T, label string, obj map[string]any, want []string) {
	t.Helper()
	wantSet := make(map[string]bool, len(want))
	for _, k := range want {
		wantSet[k] = true
	}
	missing, extra := diffSets(wantSet, keySet(obj))
	sort.Strings(missing)
	sort.Strings(extra)
	if len(missing) > 0 || len(extra) > 0 {
		t.Errorf("%s result key-set MISMATCH:\n  missing: %v\n  extra: %v\n  got keys: %v",
			label, missing, extra, sortedKeys(obj))
	}
}

func sortedKeys(obj map[string]any) []string {
	ks := make([]string, 0, len(obj))
	for k := range obj {
		ks = append(ks, k)
	}
	sort.Strings(ks)
	return ks
}
