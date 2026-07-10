//go:build functional

package functional

import (
	"encoding/json"
	"fmt"
	"net/http"
	"testing"
	"time"
)

// TestReasonerFailedStatusContract verifies the control-plane persistence
// contract that the ReasonerFailed carrier (design §4.5) is built on, WITHOUT
// spending an LLM call. The carrier replicates Python's
// `ReasonerFailed(message, result=...)` by POSTing status=failed + result to
// the CP status endpoint and then returning a plain error; the SDK's own
// resultless failed re-post must NOT clobber the carried result.
//
// The real empty-build trigger (orch/build.go `_is_empty_build`) needs a full
// plan → execute cycle and therefore an LLM; that path is covered by the
// env-gated TestBuildLLMAndDAGParity and skipped here (see below). This test
// instead asserts the underlying CP contract directly, which is what the design
// says to do when the guard cannot be triggered cheaply:
//
//  1. status=failed + result + error persist TOGETHER (the §11(b) assertion:
//     a failed record carries a non-null result AND an error simultaneously);
//  2. a subsequent resultless status=failed (the SDK's re-post) leaves the
//     result and error intact — the non-clobber behaviour §4.5 relies on.
func TestReasonerFailedStatusContract(t *testing.T) {
	requireStack(t)

	// Mint a real execution by running a fast, deterministic reasoner. It
	// completes `succeeded` (terminal); a terminal→terminal transition to
	// failed is permitted by the CP status guard, which lets us drive the
	// carrier's write shape against a real execution record.
	execID := mintExecution(t)

	// A BuildResult-shaped payload — the exact object the empty-build carrier
	// would attach (schemas/execution.go BuildResult keys).
	carriedResult := map[string]any{
		"plan_result":     map[string]any{},
		"dag_state":       map[string]any{},
		"verification":    map[string]any{},
		"success":         false,
		"summary":         "empty build: no issues completed and nothing merged",
		"pr_results":      []any{},
		"ci_gate_results": []any{},
		"pr_url":          "",
	}
	const carriedError = "empty build: no issues completed and nothing merged"

	// Step 1 — carrier write: status=failed + result + error together.
	postStatus(t, execID, map[string]any{
		"status":       "failed",
		"result":       carriedResult,
		"error":        carriedError,
		"completed_at": time.Now().UTC().Format(time.RFC3339Nano),
	})

	rec := getExecution(t, execID)
	if rec.Status != "failed" {
		t.Fatalf("after carrier write: status=%q, want failed", rec.Status)
	}
	if rec.Error == nil || *rec.Error != carriedError {
		t.Fatalf("after carrier write: error=%v, want %q", rec.Error, carriedError)
	}
	if rec.Result == nil {
		t.Fatalf("after carrier write: result is null, want the BuildResult object")
	}
	assertExactKeys(t, "reasoner-failed result", rec.Result, []string{
		"plan_result", "dag_state", "verification", "success",
		"summary", "pr_results", "ci_gate_results", "pr_url",
	})

	// Step 2 — the SDK's resultless failed re-post must not clobber the result
	// or error (§4.5). failed→failed is idempotent per the CP terminal guard.
	postStatus(t, execID, map[string]any{
		"status": "failed",
		"error":  "", // resultless + errorless re-post, as the SDK sends
	})

	rec2 := getExecution(t, execID)
	if rec2.Status != "failed" {
		t.Fatalf("after resultless re-post: status=%q, want failed", rec2.Status)
	}
	if rec2.Result == nil {
		t.Fatalf("after resultless re-post: result was CLOBBERED to null — §4.5 non-clobber contract violated")
	}
	if rec2.Error == nil || *rec2.Error != carriedError {
		t.Fatalf("after resultless re-post: error=%v, want preserved %q", rec2.Error, carriedError)
	}
}

// TestEmptyBuildGuardViaBuild documents the real-build empty-guard path and
// skips it: triggering orch/build.go's empty-build guard requires a full
// plan→execute cycle (an LLM plan), which is neither cheap nor deterministic.
// The CP contract it depends on is asserted by TestReasonerFailedStatusContract
// above; an end-to-end build (including the failed+result carrier record) runs
// under the env-gated TestBuildLLMAndDAGParity.
func TestEmptyBuildGuardViaBuild(t *testing.T) {
	t.Skip("empty-build guard needs an LLM plan/execute cycle; CP carrier contract covered by TestReasonerFailedStatusContract, end-to-end by TestBuildLLMAndDAGParity (SWE_FUNCTIONAL_LLM=1)")
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

// executionRecord is the subset of the CP ExecutionStatusResponse we assert on.
type executionRecord struct {
	ExecutionID string         `json:"execution_id"`
	Status      string         `json:"status"`
	Result      map[string]any `json:"result"`
	Error       *string        `json:"error"`
}

// mintExecution runs run_ci_watcher synchronously (deterministic, no LLM) and
// returns the resulting execution_id.
func mintExecution(t *testing.T) string {
	t.Helper()
	url := fmt.Sprintf("%s/api/v1/execute/%s.run_ci_watcher", cpBaseURL, plannerNodeID)
	input := map[string]any{
		"repo_path":    "/nonexistent/swe-af-functional-probe",
		"pr_number":    1,
		"wait_seconds": 5,
		"poll_seconds": 1,
	}
	body, status, err := httpPostJSON(url, map[string]any{"input": input})
	if err != nil {
		t.Fatalf("mint execution POST %s: %v", url, err)
	}
	if status != http.StatusOK {
		t.Fatalf("mint execution POST %s -> %d (body: %s)", url, status, string(body))
	}
	var resp struct {
		ExecutionID string `json:"execution_id"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		t.Fatalf("decode mint response: %v (body: %s)", err, string(body))
	}
	if resp.ExecutionID == "" {
		t.Fatalf("mint response had no execution_id (body: %s)", string(body))
	}
	return resp.ExecutionID
}

func postStatus(t *testing.T, execID string, payload map[string]any) {
	t.Helper()
	url := fmt.Sprintf("%s/api/v1/executions/%s/status", cpBaseURL, execID)
	body, status, err := httpPostJSON(url, payload)
	if err != nil {
		t.Fatalf("POST status %s: %v", url, err)
	}
	if status != http.StatusOK {
		t.Fatalf("POST status %s -> %d, want 200 (body: %s)", url, status, string(body))
	}
}

func getExecution(t *testing.T, execID string) executionRecord {
	t.Helper()
	url := fmt.Sprintf("%s/api/v1/executions/%s", cpBaseURL, execID)
	body, status, err := httpGet(url)
	if err != nil {
		t.Fatalf("GET execution %s: %v", url, err)
	}
	if status != http.StatusOK {
		t.Fatalf("GET execution %s -> %d (body: %s)", url, status, string(body))
	}
	var rec executionRecord
	if err := json.Unmarshal(body, &rec); err != nil {
		t.Fatalf("decode execution record: %v (body: %s)", err, string(body))
	}
	return rec
}
