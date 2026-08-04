//go:build functional

package functional

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"os"
	"os/exec"
	"testing"
	"time"
)

// buildResultKeys is the exact top-level key set of Python
// BuildResult.model_dump() (schemas/execution.go BuildResult + the injected
// pr_url). The end-to-end API byte-compat assertion (design §11(b)).
var buildResultKeys = []string{
	"plan_result", "dag_state", "verification", "success",
	"summary", "pr_results", "ci_gate_results", "pr_url",
}

// expectedPlanDAGReasoners are the role reasoners a swe-planner.build MUST fan
// out to during planning, as child executions in the control-plane DAG (design
// constraint #2, §11(b) DAG parity). Issue-writer runs per issue.
var expectedPlanDAGReasoners = []string{
	"run_product_manager",
	"run_architect",
	"run_tech_lead",
	"run_sprint_planner",
	"run_issue_writer",
}

// TestBuildLLMAndDAGParity runs a real, minimal swe-planner.build end to end and
// asserts (a) the BuildResult key set and (b) that the control-plane execution
// DAG contains child executions for the expected planning role reasoners.
//
// It is DEFAULT-SKIPPED: it costs money and time. It runs only when
// SWE_FUNCTIONAL_LLM=1 AND an Anthropic credential is present (the trivial goal
// is pinned to the haiku model to minimise cost). The temp repo is created
// INSIDE the swe-agent container's /workspaces volume because the node operates
// on container-local paths, not host paths.
func TestBuildLLMAndDAGParity(t *testing.T) {
	requireStack(t)

	if os.Getenv("SWE_FUNCTIONAL_LLM") != "1" {
		t.Skip("LLM build test disabled; set SWE_FUNCTIONAL_LLM=1 to enable (costs money/time)")
	}
	if os.Getenv("ANTHROPIC_API_KEY") == "" && os.Getenv("CLAUDE_CODE_OAUTH_TOKEN") == "" {
		t.Skip("no Anthropic credential (ANTHROPIC_API_KEY / CLAUDE_CODE_OAUTH_TOKEN) in env; cannot run the haiku build")
	}

	root, err := repoRoot()
	if err != nil {
		t.Fatalf("repo root: %v", err)
	}

	// Create a fresh git repo inside the container's /workspaces volume.
	repoPath := fmt.Sprintf("/workspaces/functional-build-%d", time.Now().UnixNano())
	if err := containerExec(root, "swe-agent",
		fmt.Sprintf("set -e; mkdir -p %s && cd %s && git init -q && "+
			"git config user.email swe@functional.test && git config user.name 'SWE Functional' && "+
			"echo '# functional' > README.md && git add README.md && git commit -q -m init",
			repoPath, repoPath)); err != nil {
		t.Fatalf("create container temp repo: %v", err)
	}
	defer func() { _ = containerExec(root, "swe-agent", "rm -rf "+repoPath) }()

	// Kick off an async build with the trivial goal and the cheap model.
	buildInput := map[string]any{
		"goal":      "Create a file hello.txt containing the word hello",
		"repo_path": repoPath,
		"config":    map[string]any{"models": map[string]any{"default": "haiku"}},
	}
	url := fmt.Sprintf("%s/api/v1/execute/async/%s.build", cpBaseURL, plannerNodeID)
	body, status, err := httpPostJSON(url, map[string]any{"input": buildInput})
	if err != nil {
		t.Fatalf("async build POST: %v", err)
	}
	if status != http.StatusOK && status != http.StatusAccepted {
		t.Fatalf("async build POST -> %d (body: %s)", status, string(body))
	}
	var async struct {
		ExecutionID string `json:"execution_id"`
		WorkflowID  string `json:"workflow_id"`
	}
	if err := json.Unmarshal(body, &async); err != nil {
		t.Fatalf("decode async response: %v (body: %s)", err, string(body))
	}
	if async.ExecutionID == "" {
		t.Fatalf("async build had no execution_id (body: %s)", string(body))
	}

	// Poll until the build execution reaches a terminal state (hard 15m cap).
	deadline := time.Now().Add(15 * time.Minute)
	var final executionRecord
	for time.Now().Before(deadline) {
		final = getExecution(t, async.ExecutionID)
		if isTerminal(final.Status) {
			break
		}
		time.Sleep(10 * time.Second)
	}
	if !isTerminal(final.Status) {
		t.Fatalf("build did not finish within 15m (last status=%q)", final.Status)
	}

	// (a) BuildResult key-set parity. The result is present regardless of
	// success (even a failed/empty build carries the BuildResult via the
	// carrier).
	if final.Result == nil {
		t.Fatalf("terminal build (status=%q) had a null result", final.Status)
	}
	assertExactKeys(t, "build result", final.Result, buildResultKeys)

	// (b) DAG parity: the CP workflow DAG must contain child executions for the
	// expected planning role reasoners.
	if async.WorkflowID == "" {
		t.Fatalf("async build had no workflow_id; cannot assert DAG parity")
	}
	reasoners := fetchWorkflowReasoners(t, async.WorkflowID)
	for _, want := range expectedPlanDAGReasoners {
		if !reasoners[want] {
			t.Errorf("DAG parity: expected child execution for %q not found in workflow %s (found: %v)",
				want, async.WorkflowID, mapKeys(reasoners))
		}
	}
}

func isTerminal(status string) bool {
	switch status {
	case "succeeded", "failed", "cancelled", "timeout", "error":
		return true
	}
	return false
}

// containerExec runs a shell command inside a compose service container.
func containerExec(root, service, script string) error {
	ctx, cancel := context.WithTimeout(context.Background(), 90*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "docker", composeArgs("exec", "-T", service, "sh", "-c", script)...)
	cmd.Dir = root
	out, err := cmd.CombinedOutput()
	if err != nil {
		return fmt.Errorf("%v: %s", err, string(out))
	}
	return nil
}

// fetchWorkflowReasoners collects the set of reasoner_ids from every node in the
// control-plane workflow DAG (root tree + timeline).
func fetchWorkflowReasoners(t *testing.T, workflowID string) map[string]bool {
	t.Helper()
	url := fmt.Sprintf("%s/api/ui/v1/workflows/%s/dag", cpBaseURL, workflowID)
	body, status, err := httpGet(url)
	if err != nil {
		t.Fatalf("GET workflow DAG %s: %v", url, err)
	}
	if status != http.StatusOK {
		t.Fatalf("GET workflow DAG %s -> %d (body: %s)", url, status, string(body))
	}

	type dagNode struct {
		ReasonerID string    `json:"reasoner_id"`
		Children   []dagNode `json:"children"`
	}
	var resp struct {
		DAG      dagNode   `json:"dag"`
		Timeline []dagNode `json:"timeline"`
	}
	if err := json.Unmarshal(body, &resp); err != nil {
		t.Fatalf("decode workflow DAG: %v (body: %s)", err, string(body))
	}

	found := map[string]bool{}
	var walk func(n dagNode)
	walk = func(n dagNode) {
		if n.ReasonerID != "" {
			found[n.ReasonerID] = true
		}
		for _, c := range n.Children {
			walk(c)
		}
	}
	walk(resp.DAG)
	for _, n := range resp.Timeline {
		walk(n)
	}
	return found
}

func mapKeys(m map[string]bool) []string {
	ks := make([]string, 0, len(m))
	for k := range m {
		ks = append(ks, k)
	}
	return ks
}
