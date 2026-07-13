package gitops

import (
	"context"
	"encoding/json"
	"errors"
	"reflect"
	"sort"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
)

// ---------------------------------------------------------------------------
// Test doubles
// ---------------------------------------------------------------------------

type noteCall struct {
	message string
	tags    []string
}

type mockApp struct {
	harnessFn  func(ctx context.Context, prompt string, schema map[string]any, dest any, opts harness.Options) (*harness.Result, error)
	lastPrompt string
	lastOpts   harness.Options
	calls      int
	notes      []noteCall
}

func (m *mockApp) Harness(ctx context.Context, prompt string, schema map[string]any, dest any, opts harness.Options) (*harness.Result, error) {
	m.calls++
	m.lastPrompt = prompt
	m.lastOpts = opts
	return m.harnessFn(ctx, prompt, schema, dest, opts)
}

func (m *mockApp) Note(ctx context.Context, message string, tags ...string) {
	m.notes = append(m.notes, noteCall{message: message, tags: tags})
}

// successHarness unmarshals body into dest and reports a parsed result, mirroring
// the real SDK harness on a valid structured response.
func successHarness(body string) func(context.Context, string, map[string]any, any, harness.Options) (*harness.Result, error) {
	return func(_ context.Context, _ string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
		if err := json.Unmarshal([]byte(body), dest); err != nil {
			return nil, err
		}
		return &harness.Result{Parsed: dest}, nil
	}
}

// parseFailHarness returns a result the harness could not parse into T (Parsed
// nil), the branch that drives each role's deterministic fallback.
func parseFailHarness() func(context.Context, string, map[string]any, any, harness.Options) (*harness.Result, error) {
	return func(_ context.Context, _ string, _ map[string]any, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{Parsed: nil}, nil
	}
}

// fatalHarness returns an is_error result whose message matches a fatal API
// pattern, which harnessx.Run classifies as a *fatal.FatalHarnessError.
func fatalHarness() func(context.Context, string, map[string]any, any, harness.Options) (*harness.Result, error) {
	return func(_ context.Context, _ string, _ map[string]any, _ any, _ harness.Options) (*harness.Result, error) {
		return &harness.Result{IsError: true, ErrorMessage: "credit balance is too low"}, nil
	}
}

// errHarness makes the harness call itself fail (non-fatal infra error), the
// Python `except Exception` branch.
func errHarness() func(context.Context, string, map[string]any, any, harness.Options) (*harness.Result, error) {
	return func(_ context.Context, _ string, _ map[string]any, _ any, _ harness.Options) (*harness.Result, error) {
		return nil, errors.New("boom")
	}
}

func newDeps(fn func(context.Context, string, map[string]any, any, harness.Options) (*harness.Result, error)) (*Deps, *mockApp) {
	app := &mockApp{harnessFn: fn}
	return &Deps{App: app}, app
}

// ---------------------------------------------------------------------------
// Assertion helpers
// ---------------------------------------------------------------------------

func jsonKeys(t *testing.T, v any) []string {
	t.Helper()
	b, err := json.Marshal(v)
	if err != nil {
		t.Fatalf("marshal result: %v", err)
	}
	var m map[string]json.RawMessage
	if err := json.Unmarshal(b, &m); err != nil {
		t.Fatalf("unmarshal result to map: %v", err)
	}
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}

func hasNote(app *mockApp, wantMsg string, wantTags ...string) bool {
	for _, n := range app.notes {
		if n.message == wantMsg && reflect.DeepEqual(n.tags, wantTags) {
			return true
		}
	}
	return false
}

func hasNoteWithTags(app *mockApp, wantTags ...string) bool {
	for _, n := range app.notes {
		if reflect.DeepEqual(n.tags, wantTags) {
			return true
		}
	}
	return false
}

// ---------------------------------------------------------------------------
// Registration surface
// ---------------------------------------------------------------------------

func TestHandlers_ExactNames(t *testing.T) {
	got := Handlers()
	want := []string{
		"run_git_init",
		"run_workspace_setup",
		"run_workspace_cleanup",
		"run_merger",
		"run_integration_tester",
		"run_repo_finalize",
		"run_github_pr",
	}
	if len(got) != len(want) {
		t.Fatalf("Handlers() has %d entries, want %d", len(got), len(want))
	}
	for _, name := range want {
		if got[name] == nil {
			t.Errorf("Handlers() missing %q", name)
		}
	}
}

// ---------------------------------------------------------------------------
// Success: exact output key set + start/complete notes per role
// ---------------------------------------------------------------------------

func TestRoles_SuccessKeySetAndNotes(t *testing.T) {
	tests := []struct {
		name        string
		handler     Handler
		input       map[string]any
		body        string
		wantKeys    []string
		startTags   []string
		completeTag []string
	}{
		{
			name:    "git_init",
			handler: RunGitInit,
			input:   map[string]any{"repo_path": "/repo", "goal": "do the thing"},
			body:    `{"mode":"fresh","integration_branch":"feat/x","success":true}`,
			wantKeys: []string{"error_message", "initial_commit_sha", "integration_branch",
				"mode", "original_branch", "remote_default_branch", "remote_url", "repo_name", "success"},
			startTags:   []string{"git_init", "start"},
			completeTag: []string{"git_init", "complete"},
		},
		{
			name:        "workspace_setup",
			handler:     RunWorkspaceSetup,
			input:       map[string]any{"repo_path": "/repo", "integration_branch": "feat/x", "worktrees_dir": "/wt", "issues": []any{map[string]any{"name": "a"}}},
			body:        `{"workspaces":[{"issue_name":"a","branch_name":"issue/1-a","worktree_path":"/wt/a"}],"success":true}`,
			wantKeys:    []string{"success", "workspaces"},
			startTags:   []string{"workspace_setup", "start"},
			completeTag: []string{"workspace_setup", "complete"},
		},
		{
			name:        "workspace_cleanup",
			handler:     RunWorkspaceCleanup,
			input:       map[string]any{"repo_path": "/repo", "worktrees_dir": "/wt", "branches_to_clean": []any{"b1"}},
			body:        `{"success":true,"cleaned":["b1"]}`,
			wantKeys:    []string{"cleaned", "success"},
			startTags:   []string{"workspace_cleanup", "start"},
			completeTag: []string{"workspace_cleanup", "complete"},
		},
		{
			name:    "merger",
			handler: RunMerger,
			input:   map[string]any{"repo_path": "/repo", "integration_branch": "feat/x", "branches_to_merge": []any{map[string]any{"branch_name": "issue/1-a"}}, "file_conflicts": []any{}, "prd_summary": "p", "architecture_summary": "a"},
			body:    `{"success":true,"merged_branches":["issue/1-a"],"failed_branches":[],"needs_integration_test":false,"summary":"ok"}`,
			wantKeys: []string{"conflict_resolutions", "failed_branches", "integration_test_rationale",
				"merge_commit_sha", "merged_branches", "needs_integration_test", "pre_merge_sha", "repo_name", "success", "summary"},
			startTags:   []string{"merger", "start"},
			completeTag: []string{"merger", "complete"},
		},
		{
			name:    "integration_tester",
			handler: RunIntegrationTester,
			input:   map[string]any{"repo_path": "/repo", "integration_branch": "feat/x", "merged_branches": []any{map[string]any{"branch_name": "issue/1-a"}}, "prd_summary": "p", "architecture_summary": "a", "conflict_resolutions": []any{}},
			body:    `{"passed":true,"tests_run":3,"tests_passed":3,"tests_failed":0,"summary":"ok"}`,
			wantKeys: []string{"failure_details", "passed", "summary", "tests_failed",
				"tests_passed", "tests_run", "tests_written"},
			startTags:   []string{"integration_tester", "start"},
			completeTag: []string{"integration_tester", "complete"},
		},
		{
			name:        "repo_finalize",
			handler:     RunRepoFinalize,
			input:       map[string]any{"repo_path": "/repo"},
			body:        `{"success":true,"files_removed":["node_modules"],"gitignore_updated":true,"summary":"clean"}`,
			wantKeys:    []string{"files_removed", "gitignore_updated", "success", "summary"},
			startTags:   []string{"repo_finalize", "start"},
			completeTag: []string{"repo_finalize", "complete"},
		},
		{
			name:        "github_pr",
			handler:     RunGitHubPR,
			input:       map[string]any{"repo_path": "/repo", "integration_branch": "feat/x", "base_branch": "main", "goal": "g"},
			body:        `{"success":true,"pr_url":"https://github.com/o/r/pull/1","pr_number":1}`,
			wantKeys:    []string{"error_message", "pr_number", "pr_url", "success"},
			startTags:   []string{"github_pr", "start"},
			completeTag: []string{"github_pr", "complete"},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			deps, app := newDeps(successHarness(tt.body))
			out, err := tt.handler(context.Background(), deps, tt.input)
			if err != nil {
				t.Fatalf("handler returned error: %v", err)
			}
			if got := jsonKeys(t, out); !reflect.DeepEqual(got, tt.wantKeys) {
				t.Errorf("output keys = %v, want %v", got, tt.wantKeys)
			}
			if !hasNoteWithTags(app, tt.startTags...) {
				t.Errorf("missing start note with tags %v; notes=%v", tt.startTags, app.notes)
			}
			if !hasNoteWithTags(app, tt.completeTag...) {
				t.Errorf("missing complete note with tags %v; notes=%v", tt.completeTag, app.notes)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Parse-failure fallbacks (deterministic, never an error, no error note)
// ---------------------------------------------------------------------------

func TestRoles_ParseFailureFallback(t *testing.T) {
	tests := []struct {
		name       string
		handler    Handler
		input      map[string]any
		wantKeys   []string
		wantString map[string]string // json key -> expected value substring
	}{
		{
			name:     "git_init",
			handler:  RunGitInit,
			input:    map[string]any{"repo_path": "/repo", "goal": "g"},
			wantKeys: []string{"error_message", "initial_commit_sha", "integration_branch", "mode", "original_branch", "remote_default_branch", "remote_url", "repo_name", "success"},
			wantString: map[string]string{
				"mode":          "unknown",
				"error_message": "Git init agent failed to produce a valid result.",
			},
		},
		{
			name:     "workspace_setup",
			handler:  RunWorkspaceSetup,
			input:    map[string]any{"repo_path": "/repo", "integration_branch": "b", "worktrees_dir": "/wt", "issues": []any{}},
			wantKeys: []string{"success", "workspaces"},
		},
		{
			name:     "workspace_cleanup",
			handler:  RunWorkspaceCleanup,
			input:    map[string]any{"repo_path": "/repo", "worktrees_dir": "/wt", "branches_to_clean": []any{}},
			wantKeys: []string{"cleaned", "success"},
		},
		{
			name:     "merger",
			handler:  RunMerger,
			input:    map[string]any{"repo_path": "/repo", "integration_branch": "b", "branches_to_merge": []any{map[string]any{"branch_name": "issue/1-a"}}, "file_conflicts": []any{}, "prd_summary": "p", "architecture_summary": "a"},
			wantKeys: []string{"conflict_resolutions", "failed_branches", "integration_test_rationale", "merge_commit_sha", "merged_branches", "needs_integration_test", "pre_merge_sha", "repo_name", "success", "summary"},
			wantString: map[string]string{
				"summary": "Merger agent failed to produce a valid result.",
			},
		},
		{
			name:     "integration_tester",
			handler:  RunIntegrationTester,
			input:    map[string]any{"repo_path": "/repo", "integration_branch": "b", "merged_branches": []any{}, "prd_summary": "p", "architecture_summary": "a", "conflict_resolutions": []any{}},
			wantKeys: []string{"failure_details", "passed", "summary", "tests_failed", "tests_passed", "tests_run", "tests_written"},
			wantString: map[string]string{
				"summary": "Integration tester agent failed to produce a valid result.",
			},
		},
		{
			name:     "repo_finalize",
			handler:  RunRepoFinalize,
			input:    map[string]any{"repo_path": "/repo"},
			wantKeys: []string{"files_removed", "gitignore_updated", "success", "summary"},
			wantString: map[string]string{
				"summary": "Repo finalize agent failed to produce a valid result.",
			},
		},
		{
			name:     "github_pr",
			handler:  RunGitHubPR,
			input:    map[string]any{"repo_path": "/repo", "integration_branch": "b", "base_branch": "main", "goal": "g"},
			wantKeys: []string{"error_message", "pr_number", "pr_url", "success"},
			wantString: map[string]string{
				"error_message": "GitHub PR agent failed to produce a valid result.",
			},
		},
	}

	for _, tt := range tests {
		t.Run(tt.name, func(t *testing.T) {
			deps, app := newDeps(parseFailHarness())
			out, err := tt.handler(context.Background(), deps, tt.input)
			if err != nil {
				t.Fatalf("fallback path returned error: %v", err)
			}
			if got := jsonKeys(t, out); !reflect.DeepEqual(got, tt.wantKeys) {
				t.Errorf("fallback keys = %v, want %v", got, tt.wantKeys)
			}
			// Fallback must report failure via success=false where present.
			b, _ := json.Marshal(out)
			var m map[string]any
			_ = json.Unmarshal(b, &m)
			if s, ok := m["success"]; ok {
				if sb, ok := s.(bool); ok && sb {
					t.Errorf("fallback success = true, want false")
				}
			}
			for k, want := range tt.wantString {
				got, _ := m[k].(string)
				if !strings.Contains(got, want) {
					t.Errorf("fallback[%q] = %q, want to contain %q", k, got, want)
				}
			}
			// A pure parse failure (no exception) must NOT emit an error note.
			if hasNoteWithTags(app, tt.name, "error") {
				t.Errorf("parse-failure fallback should not emit an error note; notes=%v", app.notes)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Fatal propagation: a fatal harness error surfaces, not a fallback
// ---------------------------------------------------------------------------

func TestRoles_FatalPropagates(t *testing.T) {
	handlers := map[string]struct {
		h     Handler
		input map[string]any
	}{
		"git_init":           {RunGitInit, map[string]any{"repo_path": "/repo", "goal": "g"}},
		"merger":             {RunMerger, map[string]any{"repo_path": "/repo", "integration_branch": "b", "branches_to_merge": []any{}, "file_conflicts": []any{}, "prd_summary": "p", "architecture_summary": "a"}},
		"integration_tester": {RunIntegrationTester, map[string]any{"repo_path": "/repo", "integration_branch": "b", "merged_branches": []any{}, "prd_summary": "p", "architecture_summary": "a", "conflict_resolutions": []any{}}},
		"github_pr":          {RunGitHubPR, map[string]any{"repo_path": "/repo", "integration_branch": "b", "base_branch": "main", "goal": "g"}},
	}

	for name, tc := range handlers {
		t.Run(name, func(t *testing.T) {
			deps, _ := newDeps(fatalHarness())
			out, err := tc.h(context.Background(), deps, tc.input)
			if err == nil {
				t.Fatalf("expected fatal error, got nil (out=%v)", out)
			}
			var fErr *fatal.FatalHarnessError
			if !errors.As(err, &fErr) {
				t.Fatalf("error = %T (%v), want *fatal.FatalHarnessError", err, err)
			}
			if out != nil {
				t.Errorf("fatal path returned a value %v, want nil", out)
			}
		})
	}
}

// ---------------------------------------------------------------------------
// Non-fatal harness error → error note + fallback
// ---------------------------------------------------------------------------

func TestRunGitInit_HarnessErrorNotesAndFallsBack(t *testing.T) {
	deps, app := newDeps(errHarness())
	out, err := RunGitInit(context.Background(), deps, map[string]any{"repo_path": "/repo", "goal": "g"})
	if err != nil {
		t.Fatalf("non-fatal harness error should fall back, got err: %v", err)
	}
	if !hasNoteWithTags(app, "git_init", "error") {
		t.Errorf("expected a git_init/error note; notes=%v", app.notes)
	}
	keys := jsonKeys(t, out)
	if !reflect.DeepEqual(keys, []string{"error_message", "initial_commit_sha", "integration_branch", "mode", "original_branch", "remote_default_branch", "remote_url", "repo_name", "success"}) {
		t.Errorf("fallback keys = %v", keys)
	}
}

// ---------------------------------------------------------------------------
// git_init: previous_error injected into the system prompt on retry
// ---------------------------------------------------------------------------

func TestRunGitInit_PreviousErrorInSystemPrompt(t *testing.T) {
	deps, app := newDeps(successHarness(`{"mode":"fresh","success":true}`))
	_, err := RunGitInit(context.Background(), deps, map[string]any{
		"repo_path":      "/repo",
		"goal":           "g",
		"previous_error": "branch already exists",
	})
	if err != nil {
		t.Fatalf("handler error: %v", err)
	}
	sp := app.lastOpts.SystemPrompt
	if !strings.Contains(sp, "## IMPORTANT: Retry Context") {
		t.Errorf("system prompt missing retry-context header; got:\n%s", sp)
	}
	if !strings.Contains(sp, "The previous attempt failed with error: 'branch already exists'") {
		t.Errorf("system prompt missing previous_error text; got:\n%s", sp)
	}
}

func TestRunGitInit_NoPreviousErrorLeavesSystemPromptClean(t *testing.T) {
	deps, app := newDeps(successHarness(`{"mode":"fresh","success":true}`))
	_, err := RunGitInit(context.Background(), deps, map[string]any{"repo_path": "/repo", "goal": "g"})
	if err != nil {
		t.Fatalf("handler error: %v", err)
	}
	if strings.Contains(app.lastOpts.SystemPrompt, "Retry Context") {
		t.Errorf("system prompt should not contain retry context when previous_error is empty")
	}
}

// ---------------------------------------------------------------------------
// workspace_setup returns per-issue WorkspaceInfo entries
// ---------------------------------------------------------------------------

func TestRunWorkspaceSetup_PerIssueEntries(t *testing.T) {
	body := `{"workspaces":[
		{"issue_name":"a","branch_name":"issue/build-01-a","worktree_path":"/wt/a"},
		{"issue_name":"b","branch_name":"issue/build-02-b","worktree_path":"/wt/b"}
	],"success":true}`
	deps, app := newDeps(successHarness(body))
	out, err := RunWorkspaceSetup(context.Background(), deps, map[string]any{
		"repo_path":          "/repo",
		"integration_branch": "feat/x",
		"worktrees_dir":      "/wt",
		"build_id":           "build",
		"issues":             []any{map[string]any{"name": "a"}, map[string]any{"name": "b"}},
	})
	if err != nil {
		t.Fatalf("handler error: %v", err)
	}
	res, ok := out.(workspaceSetupResult)
	if !ok {
		t.Fatalf("output type = %T, want workspaceSetupResult", out)
	}
	if len(res.Workspaces) != 2 {
		t.Fatalf("workspaces = %d, want 2", len(res.Workspaces))
	}
	if res.Workspaces[0].IssueName != "a" || res.Workspaces[0].BranchName != "issue/build-01-a" || res.Workspaces[0].WorktreePath != "/wt/a" {
		t.Errorf("workspace[0] = %+v, unexpected", res.Workspaces[0])
	}
	if res.Workspaces[1].IssueName != "b" {
		t.Errorf("workspace[1].IssueName = %q, want b", res.Workspaces[1].IssueName)
	}
	// Start note formats the issue-name list as a Python list repr.
	if !hasNote(app, "Workspace setup: creating 2 worktrees for ['a', 'b']", "workspace_setup", "start") {
		t.Errorf("start note not formatted with Python list repr; notes=%v", app.notes)
	}
}

// ---------------------------------------------------------------------------
// Note-message formatting parity (Python bool / list repr)
// ---------------------------------------------------------------------------

func TestNoteFormatting_MergerComplete(t *testing.T) {
	deps, app := newDeps(successHarness(`{"success":true,"merged_branches":["b1","b2"],"failed_branches":[],"needs_integration_test":true,"summary":"ok"}`))
	_, err := RunMerger(context.Background(), deps, map[string]any{
		"repo_path": "/repo", "integration_branch": "feat/x",
		"branches_to_merge": []any{map[string]any{"branch_name": "b1"}},
		"file_conflicts":    []any{}, "prd_summary": "p", "architecture_summary": "a",
	})
	if err != nil {
		t.Fatalf("handler error: %v", err)
	}
	if !hasNote(app, "Merger complete: merged=['b1', 'b2'], failed=[], needs_test=True", "merger", "complete") {
		t.Errorf("merger complete note formatting mismatch; notes=%v", app.notes)
	}
}

func TestNoteFormatting_GitInitStartTruncates(t *testing.T) {
	longGoal := strings.Repeat("x", 100)
	deps, app := newDeps(successHarness(`{"mode":"fresh","success":true}`))
	_, err := RunGitInit(context.Background(), deps, map[string]any{"repo_path": "/repo", "goal": longGoal})
	if err != nil {
		t.Fatalf("handler error: %v", err)
	}
	want := "Git init starting for: " + strings.Repeat("x", 80)
	if !hasNote(app, want, "git_init", "start") {
		t.Errorf("git_init start note not truncated to 80 chars; notes=%v", app.notes)
	}
}

// ---------------------------------------------------------------------------
// Input defaults: absent model / ai_provider fall back to sonnet / claude-code
// ---------------------------------------------------------------------------

func TestRunRepoFinalize_DefaultModelAndProvider(t *testing.T) {
	deps, app := newDeps(successHarness(`{"success":true,"summary":"clean"}`))
	_, err := RunRepoFinalize(context.Background(), deps, map[string]any{"repo_path": "/repo"})
	if err != nil {
		t.Fatalf("handler error: %v", err)
	}
	if app.lastOpts.Model != "sonnet" {
		t.Errorf("Model = %q, want sonnet (default)", app.lastOpts.Model)
	}
	if app.lastOpts.Provider != "claude-code" {
		t.Errorf("Provider = %q, want claude-code (adapter for default ai_provider=claude)", app.lastOpts.Provider)
	}
	if app.lastOpts.Cwd != "/repo" {
		t.Errorf("Cwd = %q, want /repo", app.lastOpts.Cwd)
	}
}
