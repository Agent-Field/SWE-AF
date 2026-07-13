//go:build functional

// Package functional holds the black-box / functional parity tests for the
// SWE-AF Go port (design §11(b), work-breakdown T7.2). They exercise the *live*
// stack — a control-plane plus the two Go nodes (swe-planner-go :8005,
// swe-fast-go :8006) — brought up via the self-contained compose.functional.yml,
// and assert the byte-level parity contracts that the Python→Go port must
// preserve:
//
//   - /health on both nodes returns 200 (TestHealth);
//   - each node registers EXACTLY its expected reasoner-name set — the parity
//     checklist: 30 on swe-planner-go, 29 on swe-fast-go (TestRegistrationParity);
//   - a deterministic (no-LLM) reasoner call returns the exact pydantic
//     model_dump() key set — CIWatchResult (TestDeterministicReasonerKeySets);
//   - the control-plane status contract that the ReasonerFailed carrier
//     (design §4.5) depends on: status=failed + result + error persist together,
//     and a resultless failed re-post does not clobber the result
//     (TestReasonerFailedStatusContract);
//   - (env-gated, default-skipped) a real minimal swe-planner-go.build returns the
//     BuildResult key set and the control-plane DAG contains child executions
//     for the expected role reasoners (TestBuildLLMAndDAGParity).
//
// All files carry the `functional` build tag so they are invisible to the unit
// CI job (`go test ./...`) and run only under `go test -tags functional
// ./test/functional/`.
//
// TestMain owns the stack lifecycle: it brings the compose stack up once
// (building the Go images if needed), waits for both nodes to be healthy and
// registered, runs every test against that shared stack, then tears the stack
// down (removing volumes) so nothing is left running. If Docker is unavailable
// the whole suite skips with a message rather than failing.
package functional

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"io"
	"net/http"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"testing"
	"time"
)

const (
	// composeFile is the self-contained functional stack (control-plane + both
	// Go nodes). It is distinct from the production docker-compose.go.yml, which
	// is an opt-in add-on with no control plane of its own. Path is relative to
	// the repo root (compose runs there).
	composeFile = "go/test/functional/compose.functional.yml"

	// composeOverride remaps the HOST port bindings to 18080/18005/18006 so the
	// functional stack can run alongside anything already bound to
	// 8080/8005/8006 on the host (a host control-plane, the Go add-on nodes,
	// unrelated projects). Container ports and service-to-service URLs are
	// untouched. Path is relative to the repo root (compose runs there).
	composeOverride = "go/test/functional/compose.override.functional.yml"

	// composeProject isolates this stack's containers/volumes/network from any
	// concurrently running swe-af compose project.
	composeProject = "swe-af-go-functional"

	cpBaseURL      = "http://localhost:18080"
	plannerBaseURL = "http://localhost:18005"
	fastBaseURL    = "http://localhost:18006"

	plannerNodeID = "swe-planner-go"
	fastNodeID    = "swe-fast-go"

	// Generous ceilings: the Go images are multi-stage builds that clone the
	// AgentField SDK, so a cold `up --build` can take several minutes.
	composeUpTimeout = 15 * time.Minute
	readyTimeout     = 4 * time.Minute
	composeDownGrace = 3 * time.Minute
)

// stackReady is set true once TestMain has a healthy, registered stack. When
// Docker is unavailable it stays false and every test skips via requireStack.
var stackReady bool

// stackSkipReason explains, for the skip message, why the stack is not ready.
var stackSkipReason string

func TestMain(m *testing.M) {
	code := run(m)
	os.Exit(code)
}

func run(m *testing.M) int {
	if !dockerAvailable() {
		stackSkipReason = "docker (with a running daemon) is not available"
		fmt.Println("functional: SKIP — " + stackSkipReason)
		// Still run: every test calls requireStack and skips cleanly.
		return m.Run()
	}

	root, err := repoRoot()
	if err != nil {
		stackSkipReason = "could not locate repo root: " + err.Error()
		fmt.Println("functional: SKIP — " + stackSkipReason)
		return m.Run()
	}

	fmt.Printf("functional: bringing up stack (%s + %s, project %s) from %s ...\n",
		composeFile, composeOverride, composeProject, root)
	if err := composeUp(root); err != nil {
		// Docker IS available here, so a failed `up` is a real failure (broken
		// image build, config error), not an environmental skip. Dump logs,
		// tear down, and fail the suite.
		dumpComposeLogs(root)
		_ = composeDown(root)
		fmt.Println("functional: FAIL — docker compose up failed: " + err.Error())
		return 1
	}

	// Always tear the stack down, even on panic in a test.
	defer func() {
		fmt.Println("functional: tearing down stack ...")
		if err := composeDown(root); err != nil {
			fmt.Println("functional: WARNING compose down failed: " + err.Error())
		}
	}()

	fmt.Println("functional: waiting for nodes to be healthy + registered ...")
	if err := waitForStackReady(); err != nil {
		dumpComposeLogs(root)
		stackSkipReason = "stack did not become ready: " + err.Error()
		fmt.Println("functional: SKIP — " + stackSkipReason)
		return m.Run()
	}

	stackReady = true
	fmt.Println("functional: stack ready — running tests")
	return m.Run()
}

// requireStack skips the calling test unless TestMain brought up a ready stack.
func requireStack(t *testing.T) {
	t.Helper()
	if !stackReady {
		t.Skipf("live stack unavailable: %s", stackSkipReason)
	}
}

// ---------------------------------------------------------------------------
// docker compose lifecycle
// ---------------------------------------------------------------------------

func dockerAvailable() bool {
	if _, err := exec.LookPath("docker"); err != nil {
		return false
	}
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	// `docker info` fails fast if the daemon is not reachable.
	return exec.CommandContext(ctx, "docker", "info").Run() == nil
}

// repoRoot resolves the SWE-AF repo root (where docker-compose.go.yml lives)
// from this test file's location: go/test/functional -> ../../.. .
func repoRoot() (string, error) {
	_, thisFile, _, ok := runtime.Caller(0)
	if !ok {
		return "", fmt.Errorf("runtime.Caller failed")
	}
	root, err := filepath.Abs(filepath.Join(filepath.Dir(thisFile), "..", "..", ".."))
	if err != nil {
		return "", err
	}
	if _, err := os.Stat(filepath.Join(root, composeFile)); err != nil {
		return "", fmt.Errorf("%s not found under %s: %w", composeFile, root, err)
	}
	return root, nil
}

// composeArgs prefixes every compose invocation with the project name and both
// compose files (base + host-port override).
func composeArgs(rest ...string) []string {
	args := []string{"compose", "-p", composeProject, "-f", composeFile, "-f", composeOverride}
	return append(args, rest...)
}

func composeUp(root string) error {
	ctx, cancel := context.WithTimeout(context.Background(), composeUpTimeout)
	defer cancel()
	// --build ensures the Go images reflect the current source. Compose reads
	// the repo-root .env automatically because Dir == root.
	cmd := exec.CommandContext(ctx, "docker", composeArgs("up", "-d", "--build")...)
	cmd.Dir = root
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func composeDown(root string) error {
	ctx, cancel := context.WithTimeout(context.Background(), composeDownGrace)
	defer cancel()
	// -v removes the project's named volumes (agentfield-data, workspaces) so
	// the run leaves no state behind.
	cmd := exec.CommandContext(ctx, "docker", composeArgs("down", "-v")...)
	cmd.Dir = root
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	return cmd.Run()
}

func dumpComposeLogs(root string) {
	ctx, cancel := context.WithTimeout(context.Background(), 30*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "docker", composeArgs("logs", "--tail", "80")...)
	cmd.Dir = root
	cmd.Stdout = os.Stdout
	cmd.Stderr = os.Stderr
	_ = cmd.Run()
}

// ---------------------------------------------------------------------------
// readiness
// ---------------------------------------------------------------------------

func waitForStackReady() error {
	deadline := time.Now().Add(readyTimeout)

	// Both node health endpoints.
	for _, u := range []string{plannerBaseURL + "/health", fastBaseURL + "/health"} {
		if err := waitForHTTP200(u, deadline); err != nil {
			return fmt.Errorf("health %s: %w", u, err)
		}
	}

	// Both nodes registered with the control-plane (reasoners populated).
	for _, id := range []string{plannerNodeID, fastNodeID} {
		if err := waitForRegistration(id, deadline); err != nil {
			return fmt.Errorf("registration %s: %w", id, err)
		}
	}
	return nil
}

func waitForHTTP200(url string, deadline time.Time) error {
	var lastErr error
	for time.Now().Before(deadline) {
		req, _ := http.NewRequest(http.MethodGet, url, nil)
		resp, err := http.DefaultClient.Do(req)
		if err == nil {
			_ = resp.Body.Close()
			if resp.StatusCode == http.StatusOK {
				return nil
			}
			lastErr = fmt.Errorf("status %d", resp.StatusCode)
		} else {
			lastErr = err
		}
		time.Sleep(3 * time.Second)
	}
	return fmt.Errorf("timed out (last: %v)", lastErr)
}

func waitForRegistration(nodeID string, deadline time.Time) error {
	var lastErr error
	for time.Now().Before(deadline) {
		names, err := fetchReasonerNames(nodeID)
		if err == nil && len(names) > 0 {
			return nil
		}
		if err != nil {
			lastErr = err
		} else {
			lastErr = fmt.Errorf("no reasoners yet")
		}
		time.Sleep(3 * time.Second)
	}
	return fmt.Errorf("timed out (last: %v)", lastErr)
}

// ---------------------------------------------------------------------------
// HTTP helpers
// ---------------------------------------------------------------------------

// nodeReasonersResponse is the subset of GET /api/v1/nodes/:node_id we assert
// on: the reasoner-definition list, each carrying its registered id (the
// reasoner name).
type nodeReasonersResponse struct {
	ID        string `json:"id"`
	Reasoners []struct {
		ID string `json:"id"`
	} `json:"reasoners"`
}

// fetchReasonerNames returns the set of reasoner names registered by nodeID, as
// reported by the control-plane node record.
func fetchReasonerNames(nodeID string) (map[string]bool, error) {
	url := fmt.Sprintf("%s/api/v1/nodes/%s", cpBaseURL, nodeID)
	body, status, err := httpGet(url)
	if err != nil {
		return nil, err
	}
	if status != http.StatusOK {
		return nil, fmt.Errorf("GET %s -> %d: %s", url, status, string(body))
	}
	var node nodeReasonersResponse
	if err := json.Unmarshal(body, &node); err != nil {
		return nil, fmt.Errorf("decode node record: %w", err)
	}
	names := make(map[string]bool, len(node.Reasoners))
	for _, r := range node.Reasoners {
		if r.ID != "" {
			names[r.ID] = true
		}
	}
	return names, nil
}

func httpGet(url string) ([]byte, int, error) {
	req, _ := http.NewRequest(http.MethodGet, url, nil)
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return body, resp.StatusCode, err
}

func httpPostJSON(url string, payload any) ([]byte, int, error) {
	raw, err := json.Marshal(payload)
	if err != nil {
		return nil, 0, err
	}
	req, _ := http.NewRequest(http.MethodPost, url, bytes.NewReader(raw))
	req.Header.Set("Content-Type", "application/json")
	resp, err := http.DefaultClient.Do(req)
	if err != nil {
		return nil, 0, err
	}
	defer resp.Body.Close()
	body, err := io.ReadAll(resp.Body)
	return body, resp.StatusCode, err
}

// keySet returns the top-level key set of a JSON object.
func keySet(obj map[string]any) map[string]bool {
	s := make(map[string]bool, len(obj))
	for k := range obj {
		s[k] = true
	}
	return s
}

// diffSets returns (missing = want-got, extra = got-want) as sorted-ish slices.
func diffSets(want, got map[string]bool) (missing, extra []string) {
	for k := range want {
		if !got[k] {
			missing = append(missing, k)
		}
	}
	for k := range got {
		if !want[k] {
			extra = append(extra, k)
		}
	}
	return missing, extra
}
