package critcheck

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func gitIn(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", append([]string{"-c", "user.name=t", "-c", "user.email=t@t"}, args...)...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

// issue306Repo is the repository #306 ran against (WorkAF r9): a ledger
// module with auth/ and cmd/ beside it, and the HasPositive change COMMITTED on
// top of the run's base, leaving the working tree clean. It returns the repo
// and the base commit. extra adds files to the committed change.
func issue306Repo(t *testing.T, extra map[string]string) (string, string) {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	dir := t.TempDir()
	write := func(files map[string]string) {
		for name, body := range files {
			if err := os.MkdirAll(filepath.Dir(filepath.Join(dir, name)), 0o755); err != nil {
				t.Fatal(err)
			}
			if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
				t.Fatal(err)
			}
		}
	}
	gitIn(t, dir, "init", "-q", "-b", "main")
	write(map[string]string{
		"go.mod":       "module example.com/ledger\n\ngo 1.21\n",
		"ledger.go":    "package ledger\n",
		"auth/auth.go": "package auth\n",
		"cmd/main.go":  "package main\n",
	})
	gitIn(t, dir, "add", "-A")
	gitIn(t, dir, "commit", "-q", "-m", "base")
	base := gitIn(t, dir, "rev-parse", "HEAD")
	change := map[string]string{
		"ledger.go":      "package ledger\n\nfunc (l *Ledger) HasPositive() bool { return false }\n",
		"ledger_test.go": "package ledger\n",
	}
	for k, v := range extra {
		change[k] = v
	}
	write(change)
	gitIn(t, dir, "add", "-A")
	gitIn(t, dir, "commit", "-q", "-m", "issue/ledger-haspositive: add HasPositive")
	return dir, base
}

// ac6 is #306's AC-6 as the verifier failed it.
const ac6 = "6. `git status --porcelain` lists only ledger.go and ledger_test.go as modified (nothing under auth/ or cmd/)."

func ac6Verdict() map[string]any {
	return map[string]any{
		"passed":  false,
		"summary": "Overall FAIL (passed = false): 6 of 7 acceptance criteria pass; AC-6 does not hold",
		"criteria_results": []any{
			map[string]any{"criterion": "1. `go test ./... -run TestHasPositive` exits 0", "passed": true},
			map[string]any{"criterion": ac6, "passed": false,
				"evidence": "`git status --porcelain` prints nothing because the change is committed at HEAD"},
			map[string]any{"criterion": "The project builds and its tests pass", "passed": true, ProjectCheckField: true},
		},
	}
}

// Issue #306 (WorkAF r9): AC-6 read the working tree after the change was
// committed, so it failed correct work. It is rewritten to compare with the
// base, the two named files are what the work changed, nothing under auth/ or
// cmd/ is, and the verification passes.
func TestAWorkingTreeCheckAfterTheCommitIsJudgedAgainstTheBase(t *testing.T) {
	repo, base := issue306Repo(t, nil)
	v := ac6Verdict()
	findings := ReconcileFrom(context.Background(), repo, base, v)
	if len(findings) != 1 || findings[0].Action != Rewritten || !findings[0].WorkingTree {
		t.Fatalf("findings = %+v", findings)
	}
	want := "git diff --name-only " + base[:12] + "...HEAD"
	if findings[0].Repair != want {
		t.Fatalf("repair = %q, want %q", findings[0].Repair, want)
	}
	if v["passed"] != true {
		t.Fatalf("verification still failed: %v", v["summary"])
	}
	c := v["criteria_results"].([]any)[1].(map[string]any)
	ev, _ := c["evidence"].(string)
	if c["passed"] != true || !strings.Contains(ev, "ledger_test.go") || !strings.Contains(ev, "reads uncommitted changes") {
		t.Fatalf("criterion = %v", c)
	}
}

// A real failure still fails: the same criterion over work that also touched
// auth/ stays failed, and the evidence says which file broke it.
func TestAWorkingTreeCheckStillFailsWorkThatBreaksIt(t *testing.T) {
	repo, base := issue306Repo(t, map[string]string{"auth/auth.go": "package auth\n\nvar Leak = 1\n"})
	v := ac6Verdict()
	findings := ReconcileFrom(context.Background(), repo, base, v)
	if len(findings) != 1 || findings[0].Action != RewrittenFailed {
		t.Fatalf("findings = %+v", findings)
	}
	if v["passed"] != false {
		t.Fatalf("verification passed on work that touched auth/")
	}
	c := v["criteria_results"].([]any)[1].(map[string]any)
	if c["passed"] != false || !strings.Contains(c["evidence"].(string), "auth/ is among the changed files") {
		t.Fatalf("criterion = %v", c)
	}
}

// A named file the work did not change fails the rewritten check.
func TestAWorkingTreeCheckNamingAnUnchangedFileFails(t *testing.T) {
	repo, base := issue306Repo(t, nil)
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "`git status --porcelain` lists ledger.go and README.md", "passed": false},
	}}
	findings := ReconcileFrom(context.Background(), repo, base, v)
	if len(findings) != 1 || findings[0].Action != RewrittenFailed || v["passed"] != false {
		t.Fatalf("findings = %+v, passed = %v", findings, v["passed"])
	}
}

// A check that asserts by exit status is judged by the rewritten command's.
func TestAPipedWorkingTreeCheckIsJudgedByItsExitStatus(t *testing.T) {
	repo, base := issue306Repo(t, nil)
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "`git diff --name-only | grep -q ledger_test.go` exits 0", "passed": false},
		map[string]any{"criterion": "`git status --porcelain | grep -q README.md` exits 0", "passed": false},
	}}
	findings := ReconcileFrom(context.Background(), repo, base, v)
	if len(findings) != 2 || findings[0].Action != Rewritten || findings[1].Action != RewrittenFailed {
		t.Fatalf("findings = %+v", findings)
	}
	if !strings.HasPrefix(findings[0].Repair, "git diff --name-only "+base[:12]+"...HEAD | grep -q") {
		t.Fatalf("repair = %q", findings[0].Repair)
	}
	if v["passed"] != false {
		t.Fatal("a real grep miss was lifted")
	}
}

// A working-tree check whose words name no file cannot be judged; it is not
// counted, with the committed file list as its evidence.
func TestAWorkingTreeCheckThatNamesNoFileIsNotCounted(t *testing.T) {
	repo, base := issue306Repo(t, nil)
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "`git diff --stat` shows the change", "passed": false},
	}}
	findings := ReconcileFrom(context.Background(), repo, base, v)
	if len(findings) != 1 || findings[0].Action != NotCounted || v["passed"] != true {
		t.Fatalf("findings = %+v, passed = %v", findings, v["passed"])
	}
	c := v["criteria_results"].([]any)[0].(map[string]any)
	if c["not_counted"] != true || !strings.Contains(c["evidence"].(string), "ledger_test.go") {
		t.Fatalf("criterion = %v", c)
	}
}

// When the work is NOT committed the working-tree check is a fair check and is
// left to the verifier, and so is every check when no base is known.
func TestAWorkingTreeCheckOverUncommittedWorkIsLeftAlone(t *testing.T) {
	repo, base := issue306Repo(t, nil)
	if err := os.WriteFile(filepath.Join(repo, "ledger.go"), []byte("package ledger\n// dirty\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	v := ac6Verdict()
	if findings := ReconcileFrom(context.Background(), repo, base, v); len(findings) != 0 || v["passed"] != false {
		t.Fatalf("findings = %+v", findings)
	}
	repo, _ = issue306Repo(t, nil)
	v = ac6Verdict()
	if findings := Reconcile(context.Background(), repo, v); len(findings) != 0 || v["passed"] != false {
		t.Fatalf("findings without a base = %+v", findings)
	}
}

// The project build gate's criterion is never reconsidered, even beside a
// working-tree check: a red build stays red.
func TestAWorkingTreeRewriteCannotLiftTheProjectGate(t *testing.T) {
	repo, base := issue306Repo(t, nil)
	v := ac6Verdict()
	gate := v["criteria_results"].([]any)[2].(map[string]any)
	gate["passed"] = false
	gate["criterion"] = "The project builds and its tests pass: `git status --porcelain` exited 1"
	ReconcileFrom(context.Background(), repo, base, v)
	if gate["passed"] != false || v["passed"] != false {
		t.Fatalf("the project gate was lifted: %v", v)
	}
}

func TestWorkingTreeRewriteSpellings(t *testing.T) {
	isRev := func(a string) bool { return a == "main" || a == "HEAD~1" }
	for in, want := range map[string]string{
		"git status --porcelain":                "git diff --name-only B...HEAD",
		"git status -s":                         "git diff --name-only B...HEAD",
		"git diff --name-only":                  "git diff --name-only B...HEAD",
		"git diff --cached --name-only":         "git diff --name-only B...HEAD",
		"git diff --stat -- ledger.go":          "git diff --stat B...HEAD -- ledger.go",
		"git diff ledger.go":                    "git diff B...HEAD -- ledger.go",
		"! git diff --quiet":                    "! git diff --quiet B...HEAD",
		"git diff --name-only | grep ledger.go": "git diff --name-only B...HEAD | grep ledger.go",
		"git diff main --name-only":             "",
		"git diff HEAD~1":                       "",
		"git diff base..HEAD":                   "",
		"go test ./...":                         "",
	} {
		if got := WorkingTreeRewrite(in, "B", isRev); got != want {
			t.Errorf("WorkingTreeRewrite(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestThePlannerRuleForbidsWorkingTreeChecks(t *testing.T) {
	if !strings.Contains(PlannerRule, "Never check the working tree") ||
		!strings.Contains(PlannerRule, "git diff --name-only main...HEAD") {
		t.Fatalf("PlannerRule = %q", PlannerRule)
	}
}
