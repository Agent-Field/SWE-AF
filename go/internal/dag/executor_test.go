package dag

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"sync"
	"sync/atomic"
	"testing"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Mock CallFn — a scripted reasoner dispatcher keyed by role suffix, with an
// ordered call log, per-role counts, and run_coder concurrency tracking.
// ---------------------------------------------------------------------------

type mockCall struct {
	mu        sync.Mutex
	log       []string
	counts    map[string]int
	handlers  map[string]func(kwargs map[string]any) (map[string]any, error)
	active    int32
	maxActive int32
}

func newMock() *mockCall {
	return &mockCall{counts: map[string]int{}, handlers: map[string]func(map[string]any) (map[string]any, error){}}
}

func lastSeg(target string) string {
	if i := strings.LastIndex(target, "."); i >= 0 {
		return target[i+1:]
	}
	return target
}

func (m *mockCall) on(role string, h func(kwargs map[string]any) (map[string]any, error)) {
	m.handlers[role] = h
}

func (m *mockCall) count(role string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	return m.counts[role]
}

func (m *mockCall) indexOf(role string) int {
	m.mu.Lock()
	defer m.mu.Unlock()
	for i, r := range m.log {
		if r == role {
			return i
		}
	}
	return -1
}

func (m *mockCall) fn(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
	role := lastSeg(target)
	m.mu.Lock()
	m.log = append(m.log, role)
	m.counts[role]++
	m.mu.Unlock()

	if h := m.handlers[role]; h != nil {
		return h(kwargs)
	}
	return m.defaultFor(role, kwargs)
}

func (m *mockCall) defaultFor(role string, kwargs map[string]any) (map[string]any, error) {
	switch role {
	case "run_workspace_setup":
		var workspaces []any
		for _, iss := range asMapSlice(kwargs["issues"]) {
			name := asStr(iss["name"])
			workspaces = append(workspaces, map[string]any{
				"issue_name":    name,
				"branch_name":   "issue/" + name,
				"worktree_path": "/wt/" + name,
			})
		}
		return map[string]any{"success": true, "workspaces": workspaces}, nil
	case "run_coder":
		return map[string]any{
			"files_changed": []any{"f.go"},
			"summary":       "implemented",
			"complete":      true,
			"repo_name":     asStr(kwargs["target_repo"]),
		}, nil
	case "run_code_reviewer":
		return map[string]any{"approved": true, "blocking": false, "summary": "lgtm", "debt_items": []any{}}, nil
	case "run_qa":
		return map[string]any{"passed": true, "summary": "ok", "test_failures": []any{}}, nil
	case "run_qa_synthesizer":
		return map[string]any{"action": "approve", "summary": "ok", "stuck": false}, nil
	case "run_merger":
		var merged []any
		for _, b := range asMapSlice(kwargs["branches_to_merge"]) {
			merged = append(merged, asStr(b["branch_name"]))
		}
		return map[string]any{
			"success": true, "merged_branches": merged, "failed_branches": []any{},
			"needs_integration_test": false, "summary": "merged",
		}, nil
	case "run_integration_tester":
		return map[string]any{"passed": true, "summary": "ok"}, nil
	case "run_workspace_cleanup":
		return map[string]any{"success": true, "cleaned": kwargs["branches_to_clean"]}, nil
	case "run_git_init":
		return map[string]any{"success": true, "integration_branch": "integ/main", "mode": "fresh"}, nil
	case "run_issue_writer":
		return map[string]any{"success": true}, nil
	case "run_retry_advisor":
		return map[string]any{"should_retry": false}, nil
	default:
		return map[string]any{}, nil
	}
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func testCfg(t *testing.T, overrides map[string]any) *config.ExecutionConfig {
	t.Helper()
	c, err := config.LoadExecutionConfig(overrides)
	if err != nil {
		t.Fatalf("LoadExecutionConfig: %v", err)
	}
	return c
}

// makePlan builds a minimal PlanResult dict from issue dicts and levels.
func makePlan(issues []map[string]any, levels [][]string) map[string]any {
	iss := make([]any, len(issues))
	for i := range issues {
		iss[i] = issues[i]
	}
	lv := make([]any, len(levels))
	for i := range levels {
		names := make([]any, len(levels[i]))
		for j, n := range levels[i] {
			names[j] = n
		}
		lv[i] = names
	}
	return map[string]any{
		"issues":         iss,
		"levels":         lv,
		"rationale":      "test plan",
		"artifacts_dir":  "",
		"prd":            map[string]any{"validated_description": "Test PRD", "acceptance_criteria": []any{}},
		"architecture":   map[string]any{"summary": "Test architecture"},
		"file_conflicts": []any{},
	}
}

func issue(name string, dependsOn ...string) map[string]any {
	deps := make([]any, len(dependsOn))
	for i, d := range dependsOn {
		deps[i] = d
	}
	return map[string]any{
		"name":                name,
		"title":               "Issue " + name,
		"description":         "do " + name,
		"acceptance_criteria": []any{"works"},
		"depends_on":          deps,
	}
}

func names(results []schemas.IssueResult) map[string]bool {
	s := map[string]bool{}
	for _, r := range results {
		s[r.IssueName] = true
	}
	return s
}

// ---------------------------------------------------------------------------
// Contract: basic completion + level barrier
// ---------------------------------------------------------------------------

func TestSingleIssueCompletes(t *testing.T) {
	m := newMock()
	plan := makePlan([]map[string]any{issue("a")}, [][]string{{"a"}})
	cfg := testCfg(t, nil)

	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", cfg)
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if !names(state.CompletedIssues)["a"] {
		t.Fatalf("expected 'a' completed, got %v", state.CompletedIssues)
	}
	if len(state.FailedIssues) != 0 {
		t.Fatalf("expected no failures, got %v", state.FailedIssues)
	}
}

func TestLevelBarrierWaitsForAll(t *testing.T) {
	m := newMock()
	// c depends on a and b; c must not start until level 0 (a,b) is fully done.
	var coderOrder []string
	var mu sync.Mutex
	m.on("run_coder", func(kwargs map[string]any) (map[string]any, error) {
		iss := asMap(kwargs["issue"])
		time.Sleep(10 * time.Millisecond)
		mu.Lock()
		coderOrder = append(coderOrder, asStr(iss["name"]))
		mu.Unlock()
		return map[string]any{"files_changed": []any{"f.go"}, "summary": "s", "complete": true}, nil
	})
	plan := makePlan(
		[]map[string]any{issue("a"), issue("b"), issue("c", "a", "b")},
		[][]string{{"a", "b"}, {"c"}},
	)
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", testCfg(t, nil))
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if len(state.CompletedIssues) != 3 {
		t.Fatalf("expected 3 completed, got %d", len(state.CompletedIssues))
	}
	// c must be the last coder call (barrier).
	if len(coderOrder) != 3 || coderOrder[2] != "c" {
		t.Fatalf("barrier violated: coder order = %v (c must be last)", coderOrder)
	}
}

// ---------------------------------------------------------------------------
// Contract: concurrency bounded to max_concurrent_issues
// ---------------------------------------------------------------------------

func TestConcurrencyBounded(t *testing.T) {
	m := newMock()
	m.on("run_coder", func(kwargs map[string]any) (map[string]any, error) {
		cur := atomic.AddInt32(&m.active, 1)
		for {
			prev := atomic.LoadInt32(&m.maxActive)
			if cur <= prev || atomic.CompareAndSwapInt32(&m.maxActive, prev, cur) {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
		atomic.AddInt32(&m.active, -1)
		return map[string]any{"files_changed": []any{"f.go"}, "summary": "s", "complete": true}, nil
	})

	var issues []map[string]any
	var level []string
	for _, n := range []string{"i1", "i2", "i3", "i4", "i5", "i6"} {
		issues = append(issues, issue(n))
		level = append(level, n)
	}
	cfg := testCfg(t, map[string]any{"max_concurrent_issues": 3})
	dagState := initDAGState(makePlan(issues, [][]string{level}), "/repo", nil, "")

	lr := executeLevel(context.Background(), issues, nil, dagState, cfg, 0, m.fn, "swe-planner", nil, nil)
	if len(lr.Completed) != 6 {
		t.Fatalf("expected 6 completed, got %d", len(lr.Completed))
	}
	if got := atomic.LoadInt32(&m.maxActive); got > 3 {
		t.Fatalf("concurrency exceeded limit: maxActive=%d > 3", got)
	}
}

func TestUnlimitedConcurrencyWhenZero(t *testing.T) {
	m := newMock()
	m.on("run_coder", func(kwargs map[string]any) (map[string]any, error) {
		cur := atomic.AddInt32(&m.active, 1)
		for {
			prev := atomic.LoadInt32(&m.maxActive)
			if cur <= prev || atomic.CompareAndSwapInt32(&m.maxActive, prev, cur) {
				break
			}
		}
		time.Sleep(20 * time.Millisecond)
		atomic.AddInt32(&m.active, -1)
		return map[string]any{"files_changed": []any{"f.go"}, "summary": "s", "complete": true}, nil
	})
	var issues []map[string]any
	var level []string
	for _, n := range []string{"i1", "i2", "i3", "i4"} {
		issues = append(issues, issue(n))
		level = append(level, n)
	}
	cfg := testCfg(t, map[string]any{"max_concurrent_issues": 0})
	dagState := initDAGState(makePlan(issues, [][]string{level}), "/repo", nil, "")
	executeLevel(context.Background(), issues, nil, dagState, cfg, 0, m.fn, "swe-planner", nil, nil)
	if got := atomic.LoadInt32(&m.maxActive); got != 4 {
		t.Fatalf("expected all 4 concurrent (unlimited), got maxActive=%d", got)
	}
}

// ---------------------------------------------------------------------------
// Contract: per-issue advisor timeout -> failure path (does not hang)
// ---------------------------------------------------------------------------

func TestAdvisorTimeoutFailsNotHang(t *testing.T) {
	m := newMock()
	// Reviewer blocks -> FAILED_UNRECOVERABLE -> advisor invoked. Advisor sleeps
	// past the 1s agent timeout -> callWithTimeout fires -> advisor fails ->
	// last coding-loop result returned.
	m.on("run_code_reviewer", func(kwargs map[string]any) (map[string]any, error) {
		return map[string]any{"approved": false, "blocking": true, "summary": "unsafe"}, nil
	})
	m.on("run_issue_advisor", func(kwargs map[string]any) (map[string]any, error) {
		time.Sleep(2 * time.Second)
		return map[string]any{"action": "accept_with_debt"}, nil
	})
	cfg := testCfg(t, map[string]any{"agent_timeout_seconds": 1, "enable_replanning": false})

	done := make(chan struct{})
	var state *schemas.DAGState
	go func() {
		s, _ := RunDAG(context.Background(), makePlan([]map[string]any{issue("a")}, [][]string{{"a"}}), "/repo", m.fn, "swe-planner", cfg)
		state = s
		close(done)
	}()
	select {
	case <-done:
	case <-time.After(10 * time.Second):
		t.Fatal("RunDAG hung on advisor timeout")
	}
	if !names(state.FailedIssues)["a"] {
		t.Fatalf("expected 'a' failed after advisor timeout, got failed=%v", state.FailedIssues)
	}
}

// ---------------------------------------------------------------------------
// Contract: checkpoints written at save points + round-trip a Python golden
// ---------------------------------------------------------------------------

func TestCheckpointWrittenAndRoundTrips(t *testing.T) {
	dir := t.TempDir()
	plan := makePlan([]map[string]any{issue("a")}, [][]string{{"a"}})
	plan["artifacts_dir"] = dir
	m := newMock()

	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", testCfg(t, nil))
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	cpPath := filepath.Join(dir, "execution", "checkpoint.json")
	if _, err := os.Stat(cpPath); err != nil {
		t.Fatalf("checkpoint not written: %v", err)
	}
	loaded := loadCheckpoint(dir)
	if loaded == nil {
		t.Fatal("loadCheckpoint returned nil")
	}
	if loaded.CurrentLevel != state.CurrentLevel {
		t.Fatalf("round-trip current_level mismatch: %d != %d", loaded.CurrentLevel, state.CurrentLevel)
	}
	if !names(loaded.CompletedIssues)["a"] {
		t.Fatalf("round-trip lost completed issue 'a'")
	}
}

func TestLoadPythonGoldenCheckpoint(t *testing.T) {
	dir := t.TempDir()
	execDir := filepath.Join(dir, "execution")
	if err := os.MkdirAll(execDir, 0o755); err != nil {
		t.Fatal(err)
	}
	// A realistic Python-shaped checkpoint (DAGState.model_dump()). Note:
	// max_replans is intentionally absent to exercise UnmarshalJSON default (=2).
	golden := `{
  "repo_path": "/tmp/repo",
  "artifacts_dir": "` + dir + `",
  "prd_path": "/tmp/repo/.artifacts/plan/prd.md",
  "architecture_path": "",
  "issues_dir": "",
  "original_plan_summary": "plan",
  "prd_summary": "summary",
  "architecture_summary": "arch",
  "all_issues": [
    {"name": "a", "depends_on": [], "sequence_number": 1},
    {"name": "b", "depends_on": ["a"], "sequence_number": 2}
  ],
  "levels": [["a"], ["b"]],
  "completed_issues": [
    {"issue_name": "a", "outcome": "completed", "result_summary": "done"}
  ],
  "failed_issues": [],
  "skipped_issues": [],
  "in_flight_issues": [],
  "current_level": 1,
  "replan_count": 0,
  "replan_history": [],
  "git_integration_branch": "integ/main",
  "merged_branches": ["issue/a"],
  "accumulated_debt": [{"type": "missing_functionality", "description": "x"}],
  "adaptation_history": [],
  "workspace_manifest": null,
  "build_id": "bid-1"
}`
	if err := os.WriteFile(filepath.Join(execDir, "checkpoint.json"), []byte(golden), 0o644); err != nil {
		t.Fatal(err)
	}
	loaded := loadCheckpoint(dir)
	if loaded == nil {
		t.Fatal("failed to load golden checkpoint")
	}
	if loaded.CurrentLevel != 1 {
		t.Fatalf("current_level: got %d want 1", loaded.CurrentLevel)
	}
	if loaded.MaxReplans != 2 {
		t.Fatalf("max_replans default not seeded: got %d want 2", loaded.MaxReplans)
	}
	if len(loaded.AllIssues) != 2 || asStr(loaded.AllIssues[1]["name"]) != "b" {
		t.Fatalf("all_issues not round-tripped: %v", loaded.AllIssues)
	}
	if len(loaded.CompletedIssues) != 1 || loaded.CompletedIssues[0].IssueName != "a" {
		t.Fatalf("completed_issues not round-tripped: %v", loaded.CompletedIssues)
	}
	// Completed issue attempts default (1) seeded on a checkpoint that omitted it.
	if loaded.CompletedIssues[0].Attempts != 1 {
		t.Fatalf("IssueResult.attempts default not seeded: got %d want 1", loaded.CompletedIssues[0].Attempts)
	}
	// Re-marshal must succeed (round-trippable).
	if _, err := json.Marshal(loaded); err != nil {
		t.Fatalf("re-marshal failed: %v", err)
	}
}

// ---------------------------------------------------------------------------
// Contract: split gate creates sub-issues and removes parent
// ---------------------------------------------------------------------------

func TestSplitGateCreatesSubIssuesRemovesParent(t *testing.T) {
	m := newMock()
	m.on("run_code_reviewer", func(kwargs map[string]any) (map[string]any, error) {
		return map[string]any{"approved": false, "blocking": true, "summary": "too big"}, nil
	})
	m.on("run_issue_advisor", func(kwargs map[string]any) (map[string]any, error) {
		return map[string]any{
			"action": "split",
			"sub_issues": []any{
				map[string]any{"name": "a1", "title": "A1", "description": "part 1", "depends_on": []any{}},
				map[string]any{"name": "a2", "title": "A2", "description": "part 2", "depends_on": []any{}},
			},
			"split_rationale": "too big",
		}, nil
	})
	plan := makePlan([]map[string]any{issue("a")}, [][]string{{"a"}})
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", testCfg(t, nil))
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	got := map[string]bool{}
	for _, i := range state.AllIssues {
		got[asStr(i["name"])] = true
	}
	if !got["a1"] || !got["a2"] {
		t.Fatalf("sub-issues not added: all_issues=%v", got)
	}
	// The parent is removed from the executable set (levels), though it remains
	// in all_issues as a failed entry (apply_replan keeps completed/failed).
	for _, lvl := range state.Levels {
		if contains(lvl, "a") {
			t.Fatalf("parent 'a' not removed from levels: %v", state.Levels)
		}
	}
	if !names(state.FailedIssues)["a"] {
		t.Fatalf("expected parent 'a' recorded as failed (needs_split), got %v", state.FailedIssues)
	}
	if m.count("run_issue_writer") != 2 {
		t.Fatalf("expected 2 issue_writer calls, got %d", m.count("run_issue_writer"))
	}
}

// ---------------------------------------------------------------------------
// Contract: replan MODIFY_DAG resets to level 0
// ---------------------------------------------------------------------------

func TestReplanModifyDAGResetsLevel0(t *testing.T) {
	m := newMock()
	m.on("run_code_reviewer", func(kwargs map[string]any) (map[string]any, error) {
		iss := asMap(kwargs["issue"])
		if asStr(iss["name"]) == "a" {
			return map[string]any{"approved": false, "blocking": true, "summary": "bad"}, nil
		}
		return map[string]any{"approved": true, "blocking": false, "summary": "ok"}, nil
	})
	m.on("run_replanner", func(kwargs map[string]any) (map[string]any, error) {
		return map[string]any{
			"action":              "modify_dag",
			"rationale":           "restructure",
			"new_issues":          []any{map[string]any{"name": "b", "title": "B", "description": "new", "depends_on": []any{}}},
			"removed_issue_names": []any{"a"},
			"summary":             "modified",
		}, nil
	})
	cfg := testCfg(t, map[string]any{"enable_issue_advisor": false})
	plan := makePlan([]map[string]any{issue("a")}, [][]string{{"a"}})
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", cfg)
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	// b was added by the replan and executed because the level reset to 0.
	if !names(state.CompletedIssues)["b"] {
		t.Fatalf("replan-added 'b' did not execute (level not reset to 0): completed=%v", state.CompletedIssues)
	}
	if state.ReplanCount != 1 {
		t.Fatalf("expected replan_count 1, got %d", state.ReplanCount)
	}
	if !names(state.FailedIssues)["a"] {
		t.Fatalf("expected 'a' failed, got %v", state.FailedIssues)
	}
}

// ---------------------------------------------------------------------------
// Contract: replan CONTINUE skips downstream
// ---------------------------------------------------------------------------

func TestReplanContinueSkipsDownstream(t *testing.T) {
	m := newMock()
	m.on("run_code_reviewer", func(kwargs map[string]any) (map[string]any, error) {
		iss := asMap(kwargs["issue"])
		if asStr(iss["name"]) == "a" {
			return map[string]any{"approved": false, "blocking": true, "summary": "bad"}, nil
		}
		return map[string]any{"approved": true, "blocking": false, "summary": "ok"}, nil
	})
	m.on("run_replanner", func(kwargs map[string]any) (map[string]any, error) {
		return map[string]any{"action": "continue", "rationale": "proceed", "summary": "cont"}, nil
	})
	cfg := testCfg(t, map[string]any{"enable_issue_advisor": false})
	plan := makePlan(
		[]map[string]any{issue("a"), issue("b", "a")},
		[][]string{{"a"}, {"b"}},
	)
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", cfg)
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if !contains(state.SkippedIssues, "b") {
		t.Fatalf("expected 'b' skipped downstream, got skipped=%v", state.SkippedIssues)
	}
	if !names(state.FailedIssues)["a"] {
		t.Fatalf("expected 'a' failed, got %v", state.FailedIssues)
	}
}

// ---------------------------------------------------------------------------
// Contract: level-failure threshold aborts remaining levels
// ---------------------------------------------------------------------------

func TestLevelFailureThresholdAborts(t *testing.T) {
	m := newMock()
	m.on("run_code_reviewer", func(kwargs map[string]any) (map[string]any, error) {
		return map[string]any{"approved": false, "blocking": true, "summary": "bad"}, nil
	})
	cfg := testCfg(t, map[string]any{"enable_issue_advisor": false, "enable_replanning": false})
	plan := makePlan(
		[]map[string]any{issue("a"), issue("b"), issue("c", "a")},
		[][]string{{"a", "b"}, {"c"}},
	)
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", cfg)
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if len(state.FailedIssues) != 2 {
		t.Fatalf("expected 2 failures in level 0, got %d", len(state.FailedIssues))
	}
	if !contains(state.SkippedIssues, "c") {
		t.Fatalf("expected 'c' skipped by abort threshold, got skipped=%v", state.SkippedIssues)
	}
	// 'c' must never have been executed.
	if m.count("run_coder") != 2 {
		t.Fatalf("expected exactly 2 coder calls (level 0 only), got %d", m.count("run_coder"))
	}
}

// ---------------------------------------------------------------------------
// Contract: debt notes injected into downstream issues
// ---------------------------------------------------------------------------

func TestDebtNotesInjectedDownstream(t *testing.T) {
	m := newMock()
	m.on("run_code_reviewer", func(kwargs map[string]any) (map[string]any, error) {
		iss := asMap(kwargs["issue"])
		if asStr(iss["name"]) == "a" {
			return map[string]any{"approved": false, "blocking": true, "summary": "gaps"}, nil
		}
		return map[string]any{"approved": true, "blocking": false, "summary": "ok"}, nil
	})
	m.on("run_issue_advisor", func(kwargs map[string]any) (map[string]any, error) {
		return map[string]any{
			"action":                "accept_with_debt",
			"missing_functionality": []any{"edge cases"},
			"debt_severity":         "medium",
			"summary":               "accepted with debt",
		}, nil
	})
	plan := makePlan(
		[]map[string]any{issue("a"), issue("b", "a")},
		[][]string{{"a"}, {"b"}},
	)
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", testCfg(t, nil))
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if len(state.AccumulatedDebt) == 0 {
		t.Fatalf("expected accumulated debt, got none")
	}
	var bIssue map[string]any
	for _, i := range state.AllIssues {
		if asStr(i["name"]) == "b" {
			bIssue = i
		}
	}
	notes := asAnySlice(bIssue["debt_notes"])
	if len(notes) == 0 || !strings.Contains(asStr(notes[0]), "completed with debt") {
		t.Fatalf("expected debt_notes on downstream 'b', got %v", bIssue["debt_notes"])
	}
}

// ---------------------------------------------------------------------------
// Contract: cleanup awaited before advancing to next level
// ---------------------------------------------------------------------------

func TestCleanupAwaitedBeforeAdvance(t *testing.T) {
	m := newMock()
	m.on("run_workspace_cleanup", func(kwargs map[string]any) (map[string]any, error) {
		time.Sleep(30 * time.Millisecond)
		return map[string]any{"success": true, "cleaned": kwargs["branches_to_clean"]}, nil
	})
	plan := makePlan(
		[]map[string]any{issue("a"), issue("b", "a")},
		[][]string{{"a"}, {"b"}},
	)
	git := map[string]any{"integration_branch": "integ/main", "original_branch": "main", "mode": "existing"}
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", testCfg(t, nil), WithGitConfig(git))
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if len(state.CompletedIssues) != 2 {
		t.Fatalf("expected 2 completed, got %d", len(state.CompletedIssues))
	}
	// The level-0 cleanup must complete before the level-1 worktree setup.
	firstCleanup := m.indexOf("run_workspace_cleanup")
	// second setup call is the level-1 setup.
	setupIdx := []int{}
	m.mu.Lock()
	for i, r := range m.log {
		if r == "run_workspace_setup" {
			setupIdx = append(setupIdx, i)
		}
	}
	m.mu.Unlock()
	if firstCleanup < 0 || len(setupIdx) < 2 {
		t.Fatalf("expected at least 2 setups and a cleanup; setups=%v cleanup=%d", setupIdx, firstCleanup)
	}
	if firstCleanup > setupIdx[1] {
		t.Fatalf("cleanup (idx %d) not awaited before level-1 setup (idx %d)", firstCleanup, setupIdx[1])
	}
}

// ---------------------------------------------------------------------------
// Contract: multi-repo git init runs once per repo; manifest stored on state
// ---------------------------------------------------------------------------

func makeManifest() map[string]any {
	return dumpToMap(&schemas.WorkspaceManifest{
		WorkspaceRoot:   "/ws",
		PrimaryRepoName: "api",
		Repos: []schemas.WorkspaceRepo{
			{RepoName: "api", RepoURL: "https://github.com/org/api.git", Role: "primary", AbsolutePath: "/ws/api", Branch: "main"},
			{RepoName: "lib", RepoURL: "https://github.com/org/lib.git", Role: "dependency", AbsolutePath: "/ws/lib", Branch: "main"},
		},
	})
}

func TestMultiRepoGitInitPerRepo(t *testing.T) {
	m := newMock()
	plan := makePlan(nil, nil) // no issues -> exercises init only
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", testCfg(t, nil),
		WithWorkspaceManifest(makeManifest()))
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if m.count("run_git_init") != 2 {
		t.Fatalf("expected 2 git_init calls (one per repo), got %d", m.count("run_git_init"))
	}
	if state.WorkspaceManifest == nil {
		t.Fatal("workspace_manifest not stored on dag_state")
	}
	if asStr(state.WorkspaceManifest["primary_repo_name"]) != "api" {
		t.Fatalf("primary_repo_name not preserved: %v", state.WorkspaceManifest["primary_repo_name"])
	}
	// git_init_result populated on each repo.
	mf, err := manifestFromMap(state.WorkspaceManifest)
	if err != nil {
		t.Fatal(err)
	}
	for _, r := range mf.Repos {
		if r.GitInitResult == nil {
			t.Fatalf("git_init_result not populated for repo %s", r.RepoName)
		}
	}
}

func TestWorkspaceManifestNoneSingleRepo(t *testing.T) {
	m := newMock()
	plan := makePlan(nil, nil)
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", testCfg(t, nil))
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if state.WorkspaceManifest != nil {
		t.Fatalf("expected nil workspace_manifest for single-repo, got %v", state.WorkspaceManifest)
	}
	if m.count("run_git_init") != 0 {
		t.Fatalf("expected no git_init in single-repo path, got %d", m.count("run_git_init"))
	}
}

// ---------------------------------------------------------------------------
// Contract: repo_name backfill from target_repo in executeLevel
// ---------------------------------------------------------------------------

func TestRepoNameBackfilledFromTargetRepo(t *testing.T) {
	m := newMock()
	// Coder returns no repo_name; executeLevel must backfill from target_repo.
	m.on("run_coder", func(kwargs map[string]any) (map[string]any, error) {
		return map[string]any{"files_changed": []any{"f.go"}, "summary": "s", "complete": true}, nil
	})
	iss := issue("feat")
	iss["target_repo"] = "myrepo"
	dagState := initDAGState(makePlan([]map[string]any{iss}, [][]string{{"feat"}}), "/repo", nil, "")
	lr := executeLevel(context.Background(), []map[string]any{iss}, nil, dagState, testCfg(t, nil), 0, m.fn, "swe-planner", nil, nil)
	if len(lr.Completed) != 1 || lr.Completed[0].RepoName != "myrepo" {
		t.Fatalf("repo_name not backfilled: %+v", lr.Completed)
	}
}

// ---------------------------------------------------------------------------
// Contract: git merge gate runs merger + integration; single-repo path
// ---------------------------------------------------------------------------

func TestMergeGateSingleRepo(t *testing.T) {
	m := newMock()
	m.on("run_merger", func(kwargs map[string]any) (map[string]any, error) {
		var merged []any
		for _, b := range asMapSlice(kwargs["branches_to_merge"]) {
			merged = append(merged, asStr(b["branch_name"]))
		}
		return map[string]any{"success": true, "merged_branches": merged, "failed_branches": []any{},
			"needs_integration_test": true, "summary": "merged"}, nil
	})
	plan := makePlan([]map[string]any{issue("a")}, [][]string{{"a"}})
	git := map[string]any{"integration_branch": "integ/main", "original_branch": "main", "mode": "existing"}
	state, err := RunDAG(context.Background(), plan, "/repo", m.fn, "swe-planner", testCfg(t, nil), WithGitConfig(git))
	if err != nil {
		t.Fatalf("RunDAG: %v", err)
	}
	if m.count("run_workspace_setup") != 1 {
		t.Fatalf("expected worktree setup, got %d", m.count("run_workspace_setup"))
	}
	if m.count("run_merger") != 1 {
		t.Fatalf("expected 1 merger call, got %d", m.count("run_merger"))
	}
	if m.count("run_integration_tester") != 1 {
		t.Fatalf("expected integration test (merger requested it), got %d", m.count("run_integration_tester"))
	}
	if len(state.MergeResults) != 1 {
		t.Fatalf("expected 1 merge result recorded, got %d", len(state.MergeResults))
	}
	if !contains(state.MergedBranches, "issue/a") {
		t.Fatalf("expected 'issue/a' in merged branches, got %v", state.MergedBranches)
	}
}

// ---------------------------------------------------------------------------
// Contract: context cancellation stops the run at a level boundary
// ---------------------------------------------------------------------------

func TestContextCancellationStops(t *testing.T) {
	m := newMock()
	ctx, cancel := context.WithCancel(context.Background())
	cancel() // cancel before running
	plan := makePlan([]map[string]any{issue("a")}, [][]string{{"a"}})
	_, err := RunDAG(ctx, plan, "/repo", m.fn, "swe-planner", testCfg(t, nil))
	if err == nil {
		t.Fatal("expected cancellation error, got nil")
	}
	if m.count("run_coder") != 0 {
		t.Fatalf("expected no coder calls after cancel, got %d", m.count("run_coder"))
	}
}
