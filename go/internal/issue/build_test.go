package issue

// Contract tests for the issue-level build orchestrator, mirroring
// tests/issue/test_implement_issue.py:
//
//   C1. Branch contains the implementation; caller's tree/branch untouched.
//   C2. Concurrent calls yield independent branches.
//   C3. No planning roles invoked; call count bounded.
//   C4. Default config pushes nothing and opens no PR.
//   C5. Verifier failure surfaces (success=false) instead of claiming success.
//   C6. Exhaustion / blocking review / coder crash surface debt or failure.
//
// Everything is real (temp git repo, worktrees, the real coding loop) except
// the scripted CallFn.

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
)

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

func gitT(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-C", dir}, args...)...)
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v failed: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func initRepo(t *testing.T) string {
	t.Helper()
	repo := filepath.Join(t.TempDir(), "repo")
	if err := os.MkdirAll(repo, 0o755); err != nil {
		t.Fatal(err)
	}
	gitT(t, repo, "init", "-q")
	gitT(t, repo, "checkout", "-q", "-b", "main")
	gitT(t, repo, "config", "user.email", "test@example.com")
	gitT(t, repo, "config", "user.name", "Test")
	if err := os.WriteFile(filepath.Join(repo, "README.md"), []byte("# Test\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, repo, "add", "README.md")
	gitT(t, repo, "commit", "-q", "-m", "initial commit")
	return repo
}

var planningTargets = map[string]bool{
	"run_product_manager": true, "run_architect": true, "run_tech_lead": true,
	"run_sprint_planner": true, "run_issue_writer": true, "run_environment_scout": true,
}

type recorder struct {
	mu      sync.Mutex
	targets []string
}

func (r *recorder) add(target string) {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.targets = append(r.targets, target)
}

func (r *recorder) names() []string {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := make([]string, len(r.targets))
	for i, t := range r.targets {
		out[i] = t[strings.LastIndex(t, ".")+1:]
	}
	return out
}

type scriptOpts struct {
	coderCommits    bool
	coderWrites     bool
	reviewerReplies []map[string]any
	verifierReply   map[string]any
	coderErr        error
}

// scriptedCallFn mirrors tests/issue/conftest.make_call_fn: the fake coder
// writes (and by default commits) one file per iteration in the worktree.
func scriptedCallFn(t *testing.T, rec *recorder, opts scriptOpts) CallFn {
	t.Helper()
	if opts.reviewerReplies == nil {
		opts.reviewerReplies = []map[string]any{
			{"approved": true, "blocking": false, "summary": "LGTM"},
		}
	}
	reviews := 0
	var mu sync.Mutex

	return func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		rec.add(target)
		name := target[strings.LastIndex(target, ".")+1:]
		switch name {
		case "run_coder":
			if opts.coderErr != nil {
				return nil, opts.coderErr
			}
			worktree, _ := kwargs["worktree_path"].(string)
			iteration := 1
			if v, ok := kwargs["iteration"].(int); ok {
				iteration = v
			}
			if opts.coderWrites {
				path := filepath.Join(worktree, "feature.py")
				if err := os.WriteFile(path, []byte(fmt.Sprintf("VALUE = %d\n", iteration)), 0o644); err != nil {
					return nil, err
				}
				if opts.coderCommits {
					gitT(t, worktree, "add", "feature.py")
					gitT(t, worktree, "commit", "-q", "-m", fmt.Sprintf("feat: iteration %d", iteration))
				}
			}
			files := []any{}
			if opts.coderWrites {
				files = []any{"feature.py"}
			}
			return map[string]any{
				"files_changed": files,
				"summary":       fmt.Sprintf("iteration %d", iteration),
				"complete":      true,
			}, nil
		case "run_code_reviewer":
			mu.Lock()
			idx := reviews
			if idx >= len(opts.reviewerReplies) {
				idx = len(opts.reviewerReplies) - 1
			}
			reviews++
			mu.Unlock()
			out := map[string]any{}
			for k, v := range opts.reviewerReplies[idx] {
				out[k] = v
			}
			return out, nil
		case "run_qa":
			return map[string]any{"passed": true, "summary": "qa ok"}, nil
		case "run_qa_synthesizer":
			return map[string]any{"action": "approve", "summary": "synth ok"}, nil
		case "run_verifier":
			if opts.verifierReply != nil {
				return opts.verifierReply, nil
			}
			return map[string]any{"passed": true, "summary": "verified"}, nil
		}
		return nil, fmt.Errorf("unexpected call target: %s", target)
	}
}

func runImplement(t *testing.T, repo string, callFn CallFn, input map[string]any) map[string]any {
	t.Helper()
	deps := &Deps{Call: callFn, NodeID: "test-node"}
	base := map[string]any{"issue": map[string]any{
		"title":               "Add retry helper",
		"description":         "Add a retry helper with exponential backoff.",
		"acceptance_criteria": []any{"retry() retries up to 3 times"},
	}, "repo_path": repo}
	for k, v := range input {
		base[k] = v
	}
	raw, err := ImplementIssue(context.Background(), deps, base)
	if err != nil {
		t.Fatalf("ImplementIssue: %v", err)
	}
	result, ok := raw.(map[string]any)
	if !ok {
		t.Fatalf("result type %T", raw)
	}
	return result
}

// ---------------------------------------------------------------------------
// C1 — happy path
// ---------------------------------------------------------------------------

func TestCreatesBranchAndLeavesCallerUntouched(t *testing.T) {
	repo := initRepo(t)
	headBefore := gitT(t, repo, "rev-parse", "HEAD")
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{coderCommits: true, coderWrites: true}), nil)

	if result["success"] != true {
		t.Fatalf("success = %v (result: %v)", result["success"], result["summary"])
	}
	if result["outcome"] != "completed" {
		t.Errorf("outcome = %v", result["outcome"])
	}
	branch, _ := result["branch"].(string)
	if !strings.HasPrefix(branch, "issue/") {
		t.Fatalf("branch = %q", branch)
	}
	if n := len(result["commits"].([]string)); n != 1 {
		t.Errorf("commits = %d, want 1", n)
	}
	if v, _ := result["verification"].(map[string]any); v == nil || v["passed"] != true {
		t.Errorf("verification = %v", result["verification"])
	}

	// Caller repo untouched.
	if got := gitT(t, repo, "rev-parse", "--abbrev-ref", "HEAD"); got != "main" {
		t.Errorf("caller branch = %q", got)
	}
	if got := gitT(t, repo, "rev-parse", "HEAD"); got != headBefore {
		t.Errorf("caller HEAD moved: %q", got)
	}
	if got := gitT(t, repo, "status", "--porcelain"); got != "" {
		t.Errorf("caller status dirty: %q", got)
	}

	// The branch carries the implementation; the worktree is gone.
	if got := gitT(t, repo, "show", branch+":feature.py"); got == "" {
		t.Error("feature.py missing on branch")
	}
	entries, _ := os.ReadDir(filepath.Join(repo, ".worktrees"))
	if len(entries) != 0 {
		t.Errorf("worktrees not cleaned: %v", entries)
	}
}

func TestUncommittedCoderWorkGetsCheckpointCommit(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{coderCommits: false, coderWrites: true}),
		map[string]any{"config": map[string]any{"verify": false}})

	if result["success"] != true {
		t.Fatalf("success = %v", result["success"])
	}
	branch := result["branch"].(string)
	msg := gitT(t, repo, "log", "-1", "--format=%s", branch)
	if !strings.Contains(msg, "checkpoint") {
		t.Errorf("checkpoint commit message = %q", msg)
	}
}

func TestBytecodeJunkNeverLandsOnBranch(t *testing.T) {
	// The real coder runs tests in the worktree, generating __pycache__, and
	// a sloppy model may even commit it. Neither may reach the branch.
	repo := initRepo(t)
	rec := &recorder{}
	inner := scriptedCallFn(t, rec, scriptOpts{coderCommits: false, coderWrites: true})
	callFn := func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		if strings.HasSuffix(target, "run_coder") {
			worktree, _ := kwargs["worktree_path"].(string)
			pycache := filepath.Join(worktree, "pkg", "__pycache__")
			if err := os.MkdirAll(pycache, 0o755); err != nil {
				return nil, err
			}
			if err := os.WriteFile(filepath.Join(pycache, "x.cpython-312.pyc"), []byte{0}, 0o644); err != nil {
				return nil, err
			}
		}
		return inner(ctx, target, kwargs)
	}
	result := runImplement(t, repo, callFn,
		map[string]any{"config": map[string]any{"verify": false}})

	if result["success"] != true {
		t.Fatalf("success = %v", result["success"])
	}
	for _, f := range result["files_changed"].([]string) {
		if strings.Contains(f, "__pycache__") || strings.HasSuffix(f, ".pyc") {
			t.Errorf("junk in files_changed: %s", f)
		}
	}
	tracked := gitT(t, repo, "ls-tree", "-r", "--name-only", result["branch"].(string))
	if strings.Contains(tracked, "__pycache__") {
		t.Errorf("junk tracked on branch:\n%s", tracked)
	}
}

func TestScrubUntracksAgentCommittedJunk(t *testing.T) {
	repo := initRepo(t)
	_, baseSHA, err := resolveBase(repo, "")
	if err != nil {
		t.Fatal(err)
	}
	worktree := filepath.Join(repo, ".worktrees", "wt-junk")
	if err := addWorktree(repo, worktree, "issue/junk", baseSHA); err != nil {
		t.Fatal(err)
	}
	pycache := filepath.Join(worktree, "pkg", "__pycache__")
	if err := os.MkdirAll(pycache, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(pycache, "m.cpython-312.pyc"), []byte{0}, 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, worktree, "add", "-A", ".")
	gitT(t, worktree, "commit", "-q", "-m", "agent committed junk")

	sha, err := scrubTrackedJunk(worktree, "junk-issue")
	if err != nil || sha == "" {
		t.Fatalf("scrub: sha=%q err=%v", sha, err)
	}
	if tracked := gitT(t, worktree, "ls-files"); strings.Contains(tracked, "__pycache__") {
		t.Errorf("junk still tracked:\n%s", tracked)
	}
	if again, err := scrubTrackedJunk(worktree, "junk-issue"); err != nil || again != "" {
		t.Errorf("scrub not idempotent: sha=%q err=%v", again, err)
	}
}

func TestBaseBranchOverride(t *testing.T) {
	repo := initRepo(t)
	gitT(t, repo, "checkout", "-q", "-b", "dev")
	if err := os.WriteFile(filepath.Join(repo, "dev.txt"), []byte("dev\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitT(t, repo, "add", "dev.txt")
	gitT(t, repo, "commit", "-q", "-m", "dev work")
	gitT(t, repo, "checkout", "-q", "main")

	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{coderCommits: true, coderWrites: true}),
		map[string]any{"base_branch": "dev", "config": map[string]any{"verify": false}})

	if result["success"] != true || result["base_branch"] != "dev" {
		t.Fatalf("result = %v", result)
	}
	if got := gitT(t, repo, "show", result["branch"].(string)+":dev.txt"); got != "dev" {
		t.Errorf("dev.txt on branch = %q", got)
	}
}

// ---------------------------------------------------------------------------
// C2 — parallel fan-out
// ---------------------------------------------------------------------------

func TestConcurrentBuildsOnSameRepo(t *testing.T) {
	repo := initRepo(t)
	results := make([]map[string]any, 2)
	var wg sync.WaitGroup
	for i := 0; i < 2; i++ {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			rec := &recorder{}
			deps := &Deps{
				Call:   scriptedCallFn(t, rec, scriptOpts{coderCommits: true, coderWrites: true}),
				NodeID: "test-node",
			}
			raw, err := ImplementIssue(context.Background(), deps, map[string]any{
				"issue": map[string]any{
					"title":       fmt.Sprintf("Issue %d", i),
					"description": "concurrent",
				},
				"repo_path": repo,
				"config":    map[string]any{"verify": false},
			})
			if err != nil {
				t.Errorf("ImplementIssue[%d]: %v", i, err)
				return
			}
			results[i] = raw.(map[string]any)
		}(i)
	}
	wg.Wait()

	if results[0] == nil || results[1] == nil {
		t.Fatal("missing results")
	}
	if results[0]["success"] != true || results[1]["success"] != true {
		t.Fatalf("successes: %v / %v", results[0]["summary"], results[1]["summary"])
	}
	if results[0]["branch"] == results[1]["branch"] {
		t.Errorf("branches collide: %v", results[0]["branch"])
	}
	if got := gitT(t, repo, "rev-parse", "--abbrev-ref", "HEAD"); got != "main" {
		t.Errorf("caller branch = %q", got)
	}
}

// ---------------------------------------------------------------------------
// C3 — call budget
// ---------------------------------------------------------------------------

func TestNoPlanningAgentsAndBoundedCalls(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{coderCommits: true, coderWrites: true}), nil)

	names := rec.names()
	for _, n := range names {
		if planningTargets[n] {
			t.Errorf("planning agent invoked: %s", n)
		}
	}
	// 1 iteration: coder + reviewer, then one verifier pass.
	if len(names) != 3 {
		t.Errorf("call count = %d (%v), want 3", len(names), names)
	}
	if result["iterations"] != 1 {
		t.Errorf("iterations = %v", result["iterations"])
	}
}

func TestFlaggedPathUsesQAAndSynthesizer(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{coderCommits: true, coderWrites: true}),
		map[string]any{
			"issue": map[string]any{
				"title": "Risky change", "description": "d", "needs_deeper_qa": true,
			},
			"config": map[string]any{"verify": false},
		})

	if result["success"] != true {
		t.Fatalf("success = %v", result["success"])
	}
	names := strings.Join(rec.names(), ",")
	if !strings.Contains(names, "run_qa") || !strings.Contains(names, "run_qa_synthesizer") {
		t.Errorf("flagged path roles missing: %s", names)
	}
}

// ---------------------------------------------------------------------------
// C4 — no side effects by default
// ---------------------------------------------------------------------------

func TestNoPRAndNoPushByDefault(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{coderCommits: true, coderWrites: true}), nil)

	for _, n := range rec.names() {
		if n == "run_github_pr" {
			t.Error("run_github_pr invoked with enable_github_pr=false")
		}
	}
	if result["pr_url"] != "" {
		t.Errorf("pr_url = %v", result["pr_url"])
	}
	if got := gitT(t, repo, "remote"); got != "" {
		t.Errorf("unexpected remote: %q", got)
	}
}

func TestPRSkippedWithoutRemoteEvenWhenEnabled(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{coderCommits: true, coderWrites: true}),
		map[string]any{"config": map[string]any{"verify": false, "enable_github_pr": true}})

	if result["success"] != true || result["pr_url"] != "" {
		t.Fatalf("result = %v / %v", result["success"], result["pr_url"])
	}
	for _, n := range rec.names() {
		if n == "run_github_pr" {
			t.Error("run_github_pr invoked without a remote")
		}
	}
}

// ---------------------------------------------------------------------------
// C5 — verification
// ---------------------------------------------------------------------------

func TestVerifierFailureFailsBuildButKeepsBranch(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{
			coderCommits: true, coderWrites: true,
			verifierReply: map[string]any{"passed": false, "summary": "AC not met"},
		}), nil)

	if result["success"] != false {
		t.Errorf("success = %v, want false", result["success"])
	}
	if result["outcome"] != "completed" {
		t.Errorf("outcome = %v", result["outcome"])
	}
	if branch, _ := result["branch"].(string); !strings.HasPrefix(branch, "issue/") {
		t.Errorf("branch = %v (partial work should be kept)", result["branch"])
	}
}

// ---------------------------------------------------------------------------
// C6 — failure modes
// ---------------------------------------------------------------------------

func TestExhaustionCompletesWithDebt(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{
			coderCommits: true, coderWrites: true,
			reviewerReplies: []map[string]any{
				{"approved": false, "blocking": false, "summary": "needs polish"},
			},
		}),
		map[string]any{"config": map[string]any{"verify": false, "max_coding_iterations": 2}})

	if result["outcome"] != "completed_with_debt" {
		t.Errorf("outcome = %v", result["outcome"])
	}
	if result["success"] != true {
		t.Errorf("success = %v", result["success"])
	}
	if result["iterations"] != 2 {
		t.Errorf("iterations = %v", result["iterations"])
	}
}

func TestBlockingReviewFailsButSalvagesCommits(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{
			coderCommits: true, coderWrites: true,
			reviewerReplies: []map[string]any{
				{"approved": false, "blocking": true, "summary": "introduces data loss"},
			},
		}),
		map[string]any{"config": map[string]any{"verify": false}})

	if result["success"] != false || result["outcome"] != "failed_unrecoverable" {
		t.Fatalf("result = %v / %v", result["success"], result["outcome"])
	}
	if branch, _ := result["branch"].(string); !strings.HasPrefix(branch, "issue/") {
		t.Errorf("branch = %v (commits should be salvaged)", result["branch"])
	}
}

func TestNoCommitsDeletesBranch(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{coderWrites: false}),
		map[string]any{"config": map[string]any{"verify": false}})

	if result["success"] != false || result["branch"] != "" {
		t.Fatalf("result = %v / %v", result["success"], result["branch"])
	}
	if got := gitT(t, repo, "branch", "--list", "issue/*"); got != "" {
		t.Errorf("stray issue branches: %q", got)
	}
}

func TestFatalCoderErrorReturnsStructuredFailureAndCleansUp(t *testing.T) {
	repo := initRepo(t)
	rec := &recorder{}
	result := runImplement(t, repo,
		scriptedCallFn(t, rec, scriptOpts{
			coderWrites: true,
			coderErr:    &fatal.FatalHarnessError{OriginalMessage: "credit balance too low"},
		}),
		map[string]any{"config": map[string]any{"verify": false}})

	if result["success"] != false || result["outcome"] != "error" {
		t.Fatalf("result = %v / %v", result["success"], result["outcome"])
	}
	if msg, _ := result["error_message"].(string); !strings.Contains(msg, "credit balance") {
		t.Errorf("error_message = %q", msg)
	}
	if got := gitT(t, repo, "branch", "--list", "issue/*"); got != "" {
		t.Errorf("stray issue branches: %q", got)
	}
	entries, _ := os.ReadDir(filepath.Join(repo, ".worktrees"))
	if len(entries) != 0 {
		t.Errorf("worktrees not cleaned: %v", entries)
	}
}

// ---------------------------------------------------------------------------
// Setup validation
// ---------------------------------------------------------------------------

func TestSetupValidationErrors(t *testing.T) {
	deps := &Deps{Call: func(context.Context, string, map[string]any) (map[string]any, error) {
		return nil, fmt.Errorf("must not be called")
	}, NodeID: "test-node"}
	issueMap := map[string]any{"title": "t", "description": "d"}

	if _, err := ImplementIssue(context.Background(), deps, map[string]any{
		"issue": issueMap, "repo_path": filepath.Join(t.TempDir(), "nope"),
	}); err == nil || !strings.Contains(err.Error(), "does not exist") {
		t.Errorf("missing repo: err = %v", err)
	}

	plain := t.TempDir()
	if _, err := ImplementIssue(context.Background(), deps, map[string]any{
		"issue": issueMap, "repo_path": plain,
	}); err == nil || !strings.Contains(err.Error(), "not a git repository") {
		t.Errorf("non-repo: err = %v", err)
	}

	repo := initRepo(t)
	if _, err := ImplementIssue(context.Background(), deps, map[string]any{
		"issue": issueMap, "repo_path": repo, "base_branch": "ghost",
	}); err == nil || !strings.Contains(err.Error(), "base branch") {
		t.Errorf("unknown base: err = %v", err)
	}

	if _, err := ImplementIssue(context.Background(), deps, map[string]any{
		"issue": issueMap,
	}); err == nil || !strings.Contains(err.Error(), "repo_path is required") {
		t.Errorf("missing repo_path: err = %v", err)
	}
}
