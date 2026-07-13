package advisor

import (
	_ "embed"
	"strings"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// ---------------------------------------------------------------------------
// Golden fixtures. The *_system.txt files hold the exact bytes of the Python
// SYSTEM_PROMPT constants; the *_rendered.txt files hold the exact output of the
// Python task-prompt functions for the inputs replicated below. Both were
// extracted by running the real Python source (see scratchpad/gen_advisor.py),
// so equality here proves byte-for-byte parity.
// ---------------------------------------------------------------------------

//go:embed testdata/retry_advisor_system.txt
var goldenRetryAdvisorSystem string

//go:embed testdata/issue_advisor_system.txt
var goldenIssueAdvisorSystem string

//go:embed testdata/replanner_system.txt
var goldenReplannerSystem string

//go:embed testdata/fix_generator_system.txt
var goldenFixGeneratorSystem string

//go:embed testdata/ci_fixer_system.txt
var goldenCIFixerSystem string

//go:embed testdata/pr_resolver_system.txt
var goldenPRResolverSystem string

//go:embed testdata/fast_planner_system.txt
var goldenFastPlannerSystem string

//go:embed testdata/retry_advisor_rendered.txt
var goldenRetryAdvisorRendered string

//go:embed testdata/issue_advisor_rendered.txt
var goldenIssueAdvisorRendered string

//go:embed testdata/replanner_rendered.txt
var goldenReplannerRendered string

//go:embed testdata/fix_generator_rendered.txt
var goldenFixGeneratorRendered string

//go:embed testdata/ci_fixer_rendered.txt
var goldenCIFixerRendered string

//go:embed testdata/ci_fixer_rendered_empty.txt
var goldenCIFixerRenderedEmpty string

//go:embed testdata/pr_resolver_rendered.txt
var goldenPRResolverRendered string

//go:embed testdata/pr_resolver_rendered_merged.txt
var goldenPRResolverRenderedMerged string

//go:embed testdata/fast_planner_rendered.txt
var goldenFastPlannerRendered string

//go:embed testdata/fast_planner_rendered_noctx.txt
var goldenFastPlannerRenderedNoCtx string

func assertEqual(t *testing.T, name, got, want string) {
	t.Helper()
	if got == want {
		return
	}
	// Report the first byte position that differs to make failures debuggable.
	limit := len(got)
	if len(want) < limit {
		limit = len(want)
	}
	idx := -1
	for i := 0; i < limit; i++ {
		if got[i] != want[i] {
			idx = i
			break
		}
	}
	if idx == -1 {
		idx = limit
	}
	lo := idx - 40
	if lo < 0 {
		lo = 0
	}
	t.Errorf("%s mismatch at byte %d (len got=%d want=%d)\n got: %q\nwant: %q",
		name, idx, len(got), len(want),
		safeSlice(got, lo, idx+40), safeSlice(want, lo, idx+40))
}

func safeSlice(s string, lo, hi int) string {
	if lo < 0 {
		lo = 0
	}
	if hi > len(s) {
		hi = len(s)
	}
	return s[lo:hi]
}

// TestSystemPrompts verifies each SYSTEM_PROMPT constant matches the Python
// source byte-for-byte.
func TestSystemPrompts(t *testing.T) {
	cases := []struct {
		name string
		got  string
		want string
	}{
		{"RetryAdvisorSystemPrompt", RetryAdvisorSystemPrompt, goldenRetryAdvisorSystem},
		{"IssueAdvisorSystemPrompt", IssueAdvisorSystemPrompt, goldenIssueAdvisorSystem},
		{"ReplannerSystemPrompt", ReplannerSystemPrompt, goldenReplannerSystem},
		{"FixGeneratorSystemPrompt", FixGeneratorSystemPrompt, goldenFixGeneratorSystem},
		{"CIFixerSystemPrompt", CIFixerSystemPrompt, goldenCIFixerSystem},
		{"PRResolverSystemPrompt", PRResolverSystemPrompt, goldenPRResolverSystem},
		{"FastPlannerSystemPrompt", FastPlannerSystemPrompt, goldenFastPlannerSystem},
	}
	for _, c := range cases {
		assertEqual(t, c.name, c.got, c.want)
	}
}

func TestRetryAdvisorTaskPrompt(t *testing.T) {
	manifest := &schemas.WorkspaceManifest{
		Repos: []schemas.WorkspaceRepo{
			{RepoName: "api", Role: "primary", AbsolutePath: "/ws/api"},
			{RepoName: "lib", Role: "dependency", AbsolutePath: "/ws/lib"},
		},
	}
	issue := map[string]any{
		"name":                "add-auth",
		"title":               "Add auth",
		"description":         "Implement JWT auth",
		"acceptance_criteria": []any{"login works", "logout works"},
		"depends_on":          []any{"db-schema"},
		"provides":            []any{"auth-api"},
		"files_to_create":     []any{"auth.py"},
		"files_to_modify":     []any{"app.py", "config.py"},
		"retry_context":       "Previously tried bcrypt; too slow.",
		"previous_error":      "ImportError: jwt",
		"failure_notes":       []any{"upstream db not ready", "flaky test"},
	}
	got := RetryAdvisorTaskPrompt(RetryAdvisorTaskOptions{
		Issue:               issue,
		ErrorMessage:        "ModuleNotFoundError: No module named 'jwt'",
		ErrorContext:        "Traceback...\n  line 5",
		AttemptNumber:       2,
		PRDSummary:          "Build an auth service.",
		ArchitectureSummary: "FastAPI + Postgres.",
		PRDPath:             "/plan/prd.md",
		ArchitecturePath:    "/plan/arch.md",
		WorkspaceManifest:   manifest,
	})
	assertEqual(t, "retry_advisor_rendered", got, goldenRetryAdvisorRendered)
}

func TestIssueAdvisorTaskPrompt(t *testing.T) {
	issue := map[string]any{
		"name":                "parser",
		"title":               "Parser",
		"description":         "Parse config",
		"acceptance_criteria": []any{"parses yaml"},
		"depends_on":          []any{"schema"},
		"provides":            []any{"config-api"},
		"parent_issue_name":   "big-config",
	}
	orig := map[string]any{"acceptance_criteria": []any{"parses yaml", "parses toml"}}
	failure := map[string]any{
		"outcome":       "FAILED_RECOVERABLE",
		"error_message": "assertion failed",
		"attempts":      3,
		"files_changed": []any{"parser.py"},
		"error_context": strings.Repeat("long trace ", 300),
	}
	iters := []map[string]any{
		{"iteration": 1, "action": "code", "qa_passed": true, "review_approved": false,
			"review_blocking": true, "summary": strings.Repeat("close but reviewer blocked on naming ", 10)},
		{"iteration": 2, "action": "fix", "qa_passed": false, "review_approved": false, "summary": "regressed"},
	}
	adapts := []map[string]any{
		{"adaptation_type": "RETRY_MODIFIED", "rationale": "dropped toml", "dropped_criteria": []any{"parses toml"}},
	}
	dag := map[string]any{
		"completed_issues":  []any{map[string]any{"issue_name": "schema"}},
		"failed_issues":     []any{map[string]any{"issue_name": "legacy"}},
		"prd_summary":       strings.Repeat("A config service. ", 40),
		"prd_path":          "/plan/prd.md",
		"architecture_path": "/plan/arch.md",
		"issues_dir":        "/plan/issues",
	}
	prior := []map[string]any{
		{"question": "Drop toml support?", "status": "submitted",
			"values": map[string]any{"choice": "yes", "note": "toml rarely used"}, "feedback": "go ahead"},
	}
	got := IssueAdvisorTaskPrompt(IssueAdvisorTaskOptions{
		Issue:                 issue,
		OriginalIssue:         orig,
		FailureResult:         failure,
		IterationHistory:      iters,
		DAGStateSummary:       dag,
		AdvisorInvocation:     2,
		MaxAdvisorInvocations: 2,
		PreviousAdaptations:   adapts,
		WorktreePath:          "/wt/parser",
		WorkspaceManifest:     nil,
		PriorUserResponses:    prior,
	})
	assertEqual(t, "issue_advisor_rendered", got, goldenIssueAdvisorRendered)
}

func TestReplannerTaskPrompt(t *testing.T) {
	ds := schemas.DAGState{
		OriginalPlanSummary: "Ship an API.",
		PRDSummary:          "PRD text.",
		ArchitectureSummary: "Arch text.",
		PRDPath:             "/plan/prd.md",
		ArchitecturePath:    "/plan/arch.md",
		IssuesDir:           "/plan/issues",
		RepoPath:            "/repo",
		AllIssues: []map[string]any{
			{"name": "a", "title": "Issue A", "depends_on": []any{}, "provides": []any{"x"}, "description": "do a"},
			{"name": "b", "title": "Issue B", "depends_on": []any{"a"}, "provides": []any{"y"}, "description": "do b"},
			{"name": "c", "title": "Issue C", "depends_on": []any{"b"}, "provides": []any{}, "description": "do c"},
		},
		Levels: [][]string{{"a"}, {"b"}, {"c"}},
		CompletedIssues: []schemas.IssueResult{
			{IssueName: "a", ResultSummary: "did a", FilesChanged: []string{"a.py"}, Attempts: 1},
		},
		FailedIssues: []schemas.IssueResult{
			{IssueName: "b", Attempts: 2, ErrorMessage: "boom", ErrorContext: "trace of b"},
		},
		SkippedIssues: nil,
		ReplanHistory: []schemas.ReplanDecision{
			{Action: schemas.ReplanAction("modify_dag"), Rationale: "split b", Summary: "was too big"},
		},
		AccumulatedDebt: []map[string]any{
			{"severity": "high", "type": "missing_feature", "description": "no retries"},
		},
	}
	failed := []schemas.IssueResult{
		{IssueName: "b", Attempts: 2, ErrorMessage: "boom", ErrorContext: "trace of b"},
	}
	escalation := []map[string]any{
		{"issue_name": "b", "escalation_context": "dep missing",
			"adaptations": []any{map[string]any{"adaptation_type": "RETRY_APPROACH", "rationale": "tried other lib"}}},
	}
	adaptation := []map[string]any{
		{"adaptation_type": "RETRY_MODIFIED", "rationale": "relaxed b", "dropped_criteria": []any{"strict mode"}},
	}
	prior := []map[string]any{
		{"question": "Abort?", "status": "submitted", "values": map[string]any{"decision": "continue"}},
	}
	got := ReplannerTaskPrompt(ReplannerTaskOptions{
		DAGState:           ds,
		FailedIssues:       failed,
		EscalationNotes:    escalation,
		AdaptationHistory:  adaptation,
		PriorUserResponses: prior,
	})
	assertEqual(t, "replanner_rendered", got, goldenReplannerRendered)
}

func TestFixGeneratorTaskPrompt(t *testing.T) {
	criteria := []map[string]any{
		{"criterion": "login returns 200", "evidence": "got 500", "issue_name": "auth"},
		{"criterion": "logout clears session", "evidence": "(none)"},
	}
	dag := map[string]any{
		"completed_issues":  []any{map[string]any{"n": 1}, map[string]any{"n": 2}},
		"accumulated_debt":  []any{map[string]any{"severity": "low", "type": "cosmetic", "criterion": "spacing"}},
		"prd_path":          "/plan/prd.md",
		"architecture_path": "/plan/arch.md",
	}
	prd := map[string]any{
		"validated_description": strings.Repeat("An auth service. ", 60),
		"acceptance_criteria":   []any{"users can log in", "users can log out"},
	}
	got := FixGeneratorTaskPrompt(FixGeneratorTaskOptions{
		FailedCriteria:  criteria,
		DAGStateSummary: dag,
		PRD:             prd,
	})
	assertEqual(t, "fix_generator_rendered", got, goldenFixGeneratorRendered)
}

func TestCIFixerTaskPrompt(t *testing.T) {
	checks := []any{
		schemas.CIFailedCheck{Name: "unit-tests", Workflow: "ci.yml", Conclusion: "failure",
			DetailsURL: "https://ci/1", LogsExcerpt: "E   assert 1 == 2"},
		map[string]any{"name": "lint", "workflow": "", "conclusion": "", "details_url": "", "logs_excerpt": ""},
	}
	got := CIFixerTaskPrompt(CIFixerTaskOptions{
		RepoPath:          "/repo",
		PRNumber:          42,
		PRURL:             "https://gh/pr/42",
		IntegrationBranch: "integ",
		BaseBranch:        "main",
		FailedChecks:      checks,
		Iteration:         2,
		MaxIterations:     3,
		Goal:              "Add feature X",
		CompletedIssues:   []map[string]any{{"issue_name": "auth", "result_summary": "added auth"}},
		PreviousAttempts:  []map[string]any{{"summary": "bumped dep", "commit_sha": "abcdef1234"}},
	})
	assertEqual(t, "ci_fixer_rendered", got, goldenCIFixerRendered)

	gotEmpty := CIFixerTaskPrompt(CIFixerTaskOptions{
		RepoPath:          "/repo",
		PRNumber:          7,
		PRURL:             "https://gh/pr/7",
		IntegrationBranch: "integ",
		BaseBranch:        "main",
		FailedChecks:      nil,
		Iteration:         1,
		MaxIterations:     2,
	})
	assertEqual(t, "ci_fixer_rendered_empty", gotEmpty, goldenCIFixerRenderedEmpty)
}

func TestPRResolverTaskPrompt(t *testing.T) {
	checks := []any{
		schemas.CIFailedCheck{Name: "tests", Workflow: "ci.yml", Conclusion: "failure",
			DetailsURL: "https://ci/9", LogsExcerpt: "FAIL test_x"},
	}
	comments := []any{
		map[string]any{"comment_id": 101, "thread_id": "T1", "path": "app.py", "line": 12,
			"author": "alice", "body": "rename this", "url": "https://gh/c/101"},
		map[string]any{"comment_id": 102, "thread_id": "", "path": "", "line": 0,
			"author": "bob", "body": "general note", "url": ""},
	}
	got := PRResolverTaskPrompt(PRResolverTaskOptions{
		RepoPath:          "/repo",
		PRNumber:          42,
		PRURL:             "https://gh/pr/42",
		HeadBranch:        "feature",
		BaseBranch:        "main",
		MergeState:        "conflict",
		ConflictedFiles:   []string{"app.py", "config.py"},
		FailedChecks:      checks,
		ReviewComments:    comments,
		Goal:              "Make it faster",
		AdditionalContext: "Keep API stable.",
	})
	assertEqual(t, "pr_resolver_rendered", got, goldenPRResolverRendered)

	gotMerged := PRResolverTaskPrompt(PRResolverTaskOptions{
		RepoPath:        "/repo",
		PRNumber:        8,
		PRURL:           "https://gh/pr/8",
		HeadBranch:      "feature",
		BaseBranch:      "main",
		MergeState:      "merged",
		ConflictedFiles: nil,
		FailedChecks:    nil,
		ReviewComments:  nil,
	})
	assertEqual(t, "pr_resolver_rendered_merged", gotMerged, goldenPRResolverRenderedMerged)
}

func TestFastPlannerTaskPrompt(t *testing.T) {
	got := FastPlannerTaskPrompt(FastPlannerTaskOptions{
		Goal:              "Build a TODO app",
		RepoPath:          "/repo",
		MaxTasks:          5,
		AdditionalContext: "Use SQLite.",
	})
	assertEqual(t, "fast_planner_rendered", got, goldenFastPlannerRendered)

	gotNoCtx := FastPlannerTaskPrompt(FastPlannerTaskOptions{
		Goal:     "Build a TODO app",
		RepoPath: "/repo",
		MaxTasks: 5,
	})
	assertEqual(t, "fast_planner_rendered_noctx", gotNoCtx, goldenFastPlannerRenderedNoCtx)
}
