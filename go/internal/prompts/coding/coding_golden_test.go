package coding

import (
	"os"
	"path/filepath"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// golden reads a fixture generated from the Python prompt modules.
func golden(t *testing.T, name string) string {
	t.Helper()
	b, err := os.ReadFile(filepath.Join("testdata", name))
	if err != nil {
		t.Fatalf("read testdata/%s: %v", name, err)
	}
	return string(b)
}

func eq(t *testing.T, name, got, want string) {
	t.Helper()
	if got != want {
		// Find first divergence for a precise report.
		n := len(got)
		if len(want) < n {
			n = len(want)
		}
		i := 0
		for i < n && got[i] == want[i] {
			i++
		}
		lo := i - 40
		if lo < 0 {
			lo = 0
		}
		t.Fatalf("%s mismatch at byte %d (got len=%d want len=%d)\n got ...%q...\nwant ...%q...",
			name, i, len(got), len(want), safeSlice(got, lo, i+40), safeSlice(want, lo, i+40))
	}
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

// ---------------------------------------------------------------------------
// Shared fixtures (mirror scratchpad/gen_coding.py)
// ---------------------------------------------------------------------------

func manifestMulti() *schemas.WorkspaceManifest {
	return &schemas.WorkspaceManifest{
		WorkspaceRoot: "/ws",
		Repos: []schemas.WorkspaceRepo{
			{RepoName: "api", RepoURL: "u1", Role: "primary", AbsolutePath: "/ws/api", Branch: "main"},
			{RepoName: "web", RepoURL: "u2", Role: "dependency", AbsolutePath: "/ws/web", Branch: "dev"},
		},
		PrimaryRepoName: "api",
	}
}

func manifestSingle() *schemas.WorkspaceManifest {
	return &schemas.WorkspaceManifest{
		WorkspaceRoot: "/ws",
		Repos: []schemas.WorkspaceRepo{
			{RepoName: "api", RepoURL: "u1", Role: "primary", AbsolutePath: "/ws/api", Branch: "main"},
		},
		PrimaryRepoName: "api",
	}
}

func issueFull() map[string]any {
	return map[string]any{
		"name":                "lexer",
		"title":               "Build the lexer",
		"description":         "A lexer for the language",
		"acceptance_criteria": []any{"tokenizes ints", "handles whitespace"},
		"depends_on":          []any{"grammar", "types"},
		"provides":            []any{"Lexer", "Token"},
		"files_to_create":     []any{"src/lexer.rs"},
		"files_to_modify":     []any{"src/lib.rs"},
		"testing_strategy":    "unit tests per token type",
		"guidance": map[string]any{
			"testing_guidance": "test each token",
			"review_focus":     "edge cases",
			"risk_rationale":   "core module",
			"estimated_scope":  "large",
			"needs_new_tests":  true,
			"needs_deeper_qa":  true,
		},
		"failure_notes":      []any{"upstream types changed"},
		"integration_branch": "integration/build-1",
		"sequence_number":    3,
	}
}

func issueMin() map[string]any {
	return map[string]any{
		"name":                "cfg",
		"title":               "Config tweak",
		"acceptance_criteria": []any{"flag works"},
	}
}

func projectContextFull() map[string]any {
	return map[string]any{"prd_path": "/a/prd.md", "architecture_path": "/a/arch.md", "issues_dir": "/a/issues"}
}

func memoryFull() map[string]any {
	return map[string]any{
		"codebase_conventions":  []any{"snake_case", "tabs"},
		"failure_patterns":      []any{map[string]any{"pattern": "nil deref", "issue": "auth", "description": "missing check"}},
		"dependency_interfaces": []any{map[string]any{"issue": "grammar", "summary": "defines AST", "exports": []any{"AST", "Node"}}},
		"bug_patterns":          []any{map[string]any{"type": "off-by-one", "frequency": 3, "modules": []any{"lexer", "parser"}}},
	}
}

// ---------------------------------------------------------------------------
// System prompt constants
// ---------------------------------------------------------------------------

func TestSystemPrompts(t *testing.T) {
	cases := []struct {
		name string
		got  string
		fixt string
	}{
		{"coder", CoderSystemPrompt, "sys_coder.txt"},
		{"qa", QASystemPrompt, "sys_qa.txt"},
		{"code_reviewer", CodeReviewerSystemPrompt, "sys_code_reviewer.txt"},
		{"qa_synthesizer", QASynthesizerSystemPrompt, "sys_qa_synthesizer.txt"},
		{"verifier", VerifierSystemPrompt, "sys_verifier.txt"},
		{"issue_writer", IssueWriterSystemPrompt, "sys_issue_writer.txt"},
	}
	for _, c := range cases {
		eq(t, c.name+" system prompt", c.got, golden(t, c.fixt))
	}
}

// ---------------------------------------------------------------------------
// Rendered task prompts
// ---------------------------------------------------------------------------

func TestCoderTaskPrompt(t *testing.T) {
	// Case A: feedback present, multi-repo, target repo, list conventions, all
	// optional blocks, iteration 3.
	gotA := CoderTaskPrompt(CoderTaskPromptOpts{
		Issue: issueFull(), WorktreePath: "/ws/api/wt", Feedback: "Fix the nil deref in lexer.rs:42",
		Iteration: 3, ProjectContext: projectContextFull(), MemoryContext: memoryFull(),
		WorkspaceManifest: manifestMulti(), TargetRepo: "api",
	})
	eq(t, "coder A", gotA, golden(t, "task_coder_a.txt"))

	// Case B: no feedback (else branch), single repo (no ws block), dict
	// conventions (single key), iteration default.
	gotB := CoderTaskPrompt(CoderTaskPromptOpts{
		Issue: issueMin(), WorktreePath: "/ws/api", Iteration: 1,
		MemoryContext:     map[string]any{"codebase_conventions": map[string]any{"style": "gofmt"}},
		WorkspaceManifest: manifestSingle(),
	})
	eq(t, "coder B", gotB, golden(t, "task_coder_b.txt"))
}

func TestQATaskPrompt(t *testing.T) {
	gotA := QATaskPrompt(QATaskPromptOpts{
		WorktreePath: "/ws/api/wt",
		CoderResult:  map[string]any{"summary": "implemented lexer", "files_changed": []any{"src/lexer.rs", "tests/lexer_test.rs"}, "tests_passed": true, "test_summary": "10 passed"},
		Issue:        issueFull(), IterationID: "it-1", ProjectContext: projectContextFull(),
		WorkspaceManifest: manifestMulti(), TargetRepo: "api",
	})
	eq(t, "qa A", gotA, golden(t, "task_qa_a.txt"))

	gotB := QATaskPrompt(QATaskPromptOpts{
		WorktreePath: "/ws/api", CoderResult: map[string]any{"summary": "x"}, Issue: issueMin(),
		WorkspaceManifest: manifestSingle(),
	})
	eq(t, "qa B", gotB, golden(t, "task_qa_b.txt"))
}

func TestCodeReviewerTaskPrompt(t *testing.T) {
	// Case A: qa_ran false, tests_passed false, debt + bug patterns, multi-repo.
	gotA := CodeReviewerTaskPrompt(CodeReviewerTaskPromptOpts{
		WorktreePath: "/ws/api/wt",
		CoderResult:  map[string]any{"summary": "s", "files_changed": []any{"a.rs"}, "tests_passed": false, "test_summary": "2 failed"},
		Issue:        issueFull(), IterationID: "it-1", ProjectContext: projectContextFull(), QARan: false,
		MemoryContext: memoryFull(), WorkspaceManifest: manifestMulti(), TargetRepo: "api",
	})
	eq(t, "review A", gotA, golden(t, "task_review_a.txt"))

	// Case B: qa_ran true, tests_passed true, single repo.
	gotB := CodeReviewerTaskPrompt(CodeReviewerTaskPromptOpts{
		WorktreePath: "/ws/api",
		CoderResult:  map[string]any{"summary": "s", "tests_passed": true, "test_summary": "ok"},
		Issue:        issueMin(), QARan: true, WorkspaceManifest: manifestSingle(),
	})
	eq(t, "review B", gotB, golden(t, "task_review_b.txt"))

	// Case C: tests_passed None (not reported).
	gotC := CodeReviewerTaskPrompt(CodeReviewerTaskPromptOpts{
		WorktreePath: "/ws/api", CoderResult: map[string]any{"summary": "s"}, Issue: issueMin(), QARan: false,
	})
	eq(t, "review C", gotC, golden(t, "task_review_c.txt"))
}

func TestQASynthesizerTaskPrompt(t *testing.T) {
	gotA := QASynthesizerTaskPrompt(QASynthesizerTaskPromptOpts{
		QAResult: map[string]any{"passed": false, "summary": "qa ran",
			"test_failures": []any{map[string]any{"test_name": "test_x", "file": "tests/x.rs", "error": "panic"}},
			"coverage_gaps": []any{"AC2 uncovered"}},
		ReviewResult: map[string]any{"approved": false, "blocking": true, "summary": "blocking bug",
			"debt_items": []any{map[string]any{"severity": "should_fix", "title": "refactor", "description": "long fn"}}},
		IterationHistory: []map[string]any{{"iteration": 1, "action": "FIX", "summary": "first try"}},
		IterationID:      "it-2", WorktreePath: "/ws/api/wt",
		IssueSummary:      map[string]any{"name": "lexer", "title": "Build the lexer", "acceptance_criteria": []any{"tok ints", "ws"}},
		WorkspaceManifest: manifestMulti(),
	})
	eq(t, "synth A", gotA, golden(t, "task_synth_a.txt"))

	gotB := QASynthesizerTaskPrompt(QASynthesizerTaskPromptOpts{
		QAResult:         map[string]any{"passed": true, "summary": "ok"},
		ReviewResult:     map[string]any{"approved": true, "summary": "lgtm"},
		IterationHistory: []map[string]any{},
	})
	eq(t, "synth B", gotB, golden(t, "task_synth_b.txt"))
}

func TestVerifierTaskPrompt(t *testing.T) {
	gotA := VerifierTaskPrompt(VerifierTaskPromptOpts{
		PRD: map[string]any{"validated_description": "A parser", "acceptance_criteria": []any{"parses ints", "parses floats"},
			"must_have": []any{"int parsing"}, "nice_to_have": []any{"float parsing"}},
		ArtifactsDir:    "/a/artifacts",
		CompletedIssues: []map[string]any{{"issue_name": "lexer", "result_summary": "done", "files_changed": []any{"src/lexer.rs"}}},
		FailedIssues:    []map[string]any{{"issue_name": "parser", "error_message": "timeout"}},
		SkippedIssues:   []string{"optimizer"},
		BuildHealth: map[string]any{"issues_completed": 1, "issues_failed": 1, "total_tests_reported": 10,
			"modules_passing": []any{"lexer"}, "modules_failing": []any{"parser"}, "known_risks": []any{"perf"}},
		WorkspaceManifest: manifestMulti(),
	})
	eq(t, "verifier A", gotA, golden(t, "task_verifier_a.txt"))

	gotB := VerifierTaskPrompt(VerifierTaskPromptOpts{
		PRD: map[string]any{"validated_description": "X"}, ArtifactsDir: "",
		CompletedIssues: []map[string]any{}, FailedIssues: []map[string]any{}, SkippedIssues: []string{},
	})
	eq(t, "verifier B", gotB, golden(t, "task_verifier_b.txt"))
}

func TestIssueWriterTaskPrompt(t *testing.T) {
	gotA := IssueWriterTaskPrompt(IssueWriterTaskPromptOpts{
		Issue: issueFull(), PRDSummary: "Build a compiler", ArchitectureSummary: "Layered design",
		IssuesDir: "/a/issues", PRDPath: "/a/prd.md", ArchitecturePath: "/a/arch.md",
		SiblingIssues: []map[string]any{
			{"name": "parser", "title": "Parser", "provides": []any{"Parser"}},
			{"name": "types", "title": "Types"},
		},
		WorkspaceManifest: manifestMulti(),
	})
	eq(t, "issue_writer A", gotA, golden(t, "task_issuewriter_a.txt"))

	gotB := IssueWriterTaskPrompt(IssueWriterTaskPromptOpts{
		Issue: map[string]any{"name": "cfg", "title": "Config"}, PRDSummary: "P", ArchitectureSummary: "A",
		IssuesDir: "/a/issues",
	})
	eq(t, "issue_writer B", gotB, golden(t, "task_issuewriter_b.txt"))
}
