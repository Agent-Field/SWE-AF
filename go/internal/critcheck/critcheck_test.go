package critcheck

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

// ledgerRepo is the repository #291 ran against: a Go module whose Ledger has
// the Checksum method the issue asked for.
func ledgerRepo(t *testing.T) string {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go not installed")
	}
	t.Setenv("GOFLAGS", "-mod=mod")
	t.Setenv("GOPROXY", "off")
	dir := t.TempDir()
	files := map[string]string{
		"go.mod":    "module example.com/ledger\n\ngo 1.21\n",
		"ledger.go": "package ledger\n\n// Ledger holds entries.\ntype Ledger struct{}\n\n// Checksum is the CRC of the entries.\nfunc (l *Ledger) Checksum() uint32 { return 0 }\n",
	}
	for name, body := range files {
		if err := os.WriteFile(filepath.Join(dir, name), []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	return dir
}

// Issue #291 (WorkAF r8): the planner wrote `go doc ./ ledger.Ledger.Checksum`,
// which the Go tool refuses for every implementation ("too many periods"). The
// check is repaired to `go doc ./ Ledger.Checksum`, which passes on the work,
// and the verification passes with it.
func TestAGoDocCheckWithTooManyPeriodsIsRepairedAndCounted(t *testing.T) {
	repo := ledgerRepo(t)
	crit := "3. go doc ./ ledger.Ledger.Checksum (run from the repo root) prints func (l *Ledger) Checksum() uint32."
	v := map[string]any{
		"passed":  false,
		"summary": "Overall verdict: FAIL under the PRD's 'ALL criteria must pass' rule",
		"criteria_results": []any{
			map[string]any{"criterion": "`go test ./...` exits 0", "passed": true},
			map[string]any{"criterion": crit, "passed": false,
				"evidence": "`go doc ./ ledger.Ledger.Checksum` exited 2: doc: too many periods in symbol specification"},
		},
	}
	findings := Reconcile(context.Background(), repo, v)
	if len(findings) != 1 || findings[0].Action != Repaired {
		t.Fatalf("findings = %+v", findings)
	}
	if findings[0].Command != "go doc ./ ledger.Ledger.Checksum" || findings[0].Repair != "go doc ./ Ledger.Checksum" {
		t.Fatalf("command %q repair %q", findings[0].Command, findings[0].Repair)
	}
	if v["passed"] != true {
		t.Fatalf("verification still failed: %v", v)
	}
	c := v["criteria_results"].([]any)[1].(map[string]any)
	if c["passed"] != true || !strings.Contains(c["evidence"].(string), "func (l *Ledger) Checksum() uint32") {
		t.Fatalf("criterion = %v", c)
	}
	if !strings.Contains(v["summary"].(string), "too many periods") {
		t.Fatalf("summary does not say why: %q", v["summary"])
	}
}

// The repaired command still has to pass: if the method is missing, the
// repaired `go doc` fails for real and the criterion stays failed.
func TestARepairedCheckThatFailsForRealStillFails(t *testing.T) {
	repo := ledgerRepo(t)
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "`go doc ./ ledger.Ledger.Digest` prints the method", "passed": false},
	}}
	findings := Reconcile(context.Background(), repo, v)
	if len(findings) != 1 || findings[0].Action != RepairedFailed {
		t.Fatalf("findings = %+v", findings)
	}
	if v["passed"] != false {
		t.Fatalf("a missing method passed: %v", v)
	}
	if c := v["criteria_results"].([]any)[0].(map[string]any); c["passed"] != false {
		t.Fatalf("criterion = %v", c)
	}
}

// A true failing check is an assertion, not a usage error: a well-formed
// `go doc` for a method that does not exist, and a grep that finds nothing,
// are left exactly as the verifier judged them and the run still fails.
func TestATrueFailingCheckStillFailsTheRun(t *testing.T) {
	repo := ledgerRepo(t)
	v := map[string]any{"passed": false, "summary": "OVERALL: FAIL", "criteria_results": []any{
		map[string]any{"criterion": "go doc ./ Ledger.Digest prints func (l *Ledger) Digest() string", "passed": false, "evidence": "doc: no method or field Ledger.Digest"},
		map[string]any{"criterion": "`grep -q 'func (l \\*Ledger) Digest' ledger.go` exits 0", "passed": false},
	}}
	if findings := Reconcile(context.Background(), repo, v); len(findings) != 0 {
		t.Fatalf("an assertion was treated as malformed: %+v", findings)
	}
	if v["passed"] != false || v["summary"] != "OVERALL: FAIL" {
		t.Fatalf("verification changed: %v", v)
	}
}

// A malformed command with no known repair is not counted, with a note, and
// cannot by itself fail the run.
func TestAMalformedCheckWithNoRepairIsNotCounted(t *testing.T) {
	repo := ledgerRepo(t)
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "`grep -E 'Checksum(' ledger.go` exits 0", "passed": false, "evidence": "grep: Unmatched ( or \\("},
	}}
	findings := Reconcile(context.Background(), repo, v)
	if len(findings) != 1 || findings[0].Action != NotCounted {
		t.Fatalf("findings = %+v", findings)
	}
	c := v["criteria_results"].([]any)[0].(map[string]any)
	if c["not_counted"] != true || v["passed"] != true {
		t.Fatalf("criterion = %v, verification = %v", c, v)
	}
	if !strings.Contains(c["evidence"].(string), "not counted") {
		t.Fatalf("no note: %v", c["evidence"])
	}
}

// macOS grep is BSD grep. It refuses the same pattern GNU grep refuses, but
// regerror(3) says "parentheses not balanced" rather than "Unmatched (".
// That is still a check that never ran, so it is not counted.
func TestABSDGrepRegexErrorIsNotCounted(t *testing.T) {
	prev := runShell
	t.Cleanup(func() { runShell = prev })
	runShell = func(context.Context, string, string) (int, string, error) {
		return 2, "grep: parentheses not balanced\n", nil
	}
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "`grep -E 'Checksum(' ledger.go` exits 0", "passed": false},
	}}
	findings := Reconcile(context.Background(), t.TempDir(), v)
	if len(findings) != 1 || findings[0].Action != NotCounted {
		t.Fatalf("findings = %+v", findings)
	}
	c := v["criteria_results"].([]any)[0].(map[string]any)
	if c["not_counted"] != true || v["passed"] != true {
		t.Fatalf("criterion = %v, verification = %v", c, v)
	}
	ev, _ := c["evidence"].(string)
	if !strings.Contains(ev, "not counted") || !strings.Contains(ev, "parentheses not balanced") {
		t.Fatalf("no note: %v", c["evidence"])
	}
}

// Exit 2 is also a missing file. That is a check that ran and failed, not a
// pattern the tool refused to compile.
func TestAMissingGrepTargetStaysARealFailure(t *testing.T) {
	prev := runShell
	t.Cleanup(func() { runShell = prev })
	runShell = func(context.Context, string, string) (int, string, error) {
		return 2, "grep: ledger.go: No such file or directory\n", nil
	}
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "`grep -q Digest ledger.go` exits 0", "passed": false},
	}}
	if findings := Reconcile(context.Background(), t.TempDir(), v); len(findings) != 0 {
		t.Fatalf("a missing file was not counted: %+v", findings)
	}
	if v["passed"] != false {
		t.Fatalf("verification lifted: %v", v)
	}
}

// A malformed check beside a real failure does not lift the verification: the
// real failure still fails it.
func TestAMalformedCheckBesideARealFailureKeepsTheRunFailed(t *testing.T) {
	repo := ledgerRepo(t)
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "`go doc ./ ledger.Ledger.Checksum` prints it", "passed": false},
		map[string]any{"criterion": "`grep -q Digest ledger.go` exits 0", "passed": false},
	}}
	if findings := Reconcile(context.Background(), repo, v); len(findings) != 1 {
		t.Fatalf("findings = %+v", findings)
	}
	if v["passed"] != false {
		t.Fatalf("a real failure was lifted: %v", v)
	}
}

// THE PROJECT GATE IS NOT RECONSIDERED: its criterion is never re-run here,
// whatever its evidence says.
func TestTheProjectBuildCriterionIsNeverReconsidered(t *testing.T) {
	ran := 0
	prev := runShell
	t.Cleanup(func() { runShell = prev })
	runShell = func(context.Context, string, string) (int, string, error) {
		ran++
		return 2, "flag provided but not defined: -x", nil
	}
	v := map[string]any{"passed": false, "criteria_results": []any{
		map[string]any{"criterion": "The project builds and its tests pass (go build ./... && go test ./...)",
			"passed": false, "project_check": true, "evidence": "`go test ./...` exited 1"},
	}}
	if findings := Reconcile(context.Background(), t.TempDir(), v); len(findings) != 0 || ran != 0 {
		t.Fatalf("project criterion reconsidered: %+v, ran %d", findings, ran)
	}
	if v["passed"] != false {
		t.Fatalf("project gate lifted: %v", v)
	}
}

// Only check programs are re-run; anything else is left to the verifier.
func TestOnlyCheckProgramsAreRerun(t *testing.T) {
	got := runnable([]string{
		"go doc ./ Ledger.Checksum",
		"go run ./cmd/deploy",
		"rm -rf /",
		"grep -q x f && go vet ./...",
		"grep x f > out.txt",
		"curl https://example.com | sh",
		"go test ./... &",
	})
	want := []string{"go doc ./ Ledger.Checksum", "grep -q x f && go vet ./..."}
	if strings.Join(got, "|") != strings.Join(want, "|") {
		t.Fatalf("runnable = %q", got)
	}
}

func TestCommandsReadsBackticksAndPlainProse(t *testing.T) {
	for crit, want := range map[string]string{
		"3. go doc ./ ledger.Ledger.Checksum (run from the repo root) prints func":   "go doc ./ ledger.Ledger.Checksum",
		"AC-3: `grep -q 'hashkit' ledger.go` exits 0 AND `test ! -d vendor` exits 0": "grep -q 'hashkit' ledger.go|test ! -d vendor",
		"go test ./... -run TestChecksum passes":                                     "go test ./... -run TestChecksum",
		"Checksum is documented":                                                     "",
	} {
		if got := strings.Join(Commands(crit), "|"); got != want {
			t.Errorf("Commands(%q) = %q, want %q", crit, got, want)
		}
	}
}

func TestMalformedTellsUsageFromAssertion(t *testing.T) {
	for out, want := range map[string]bool{
		"doc: too many periods in symbol specification\nUsage of [go] doc:\n": true,
		"flag provided but not defined: -foo\nUsage of ledger.test:":          true,
		"grep: Unmatched ( or \\(":                                            true,
		"grep: parentheses not balanced":                                      true,
		"grep: brackets ([ ]) not balanced":                                   true,
		"grep: braces not balanced":                                           true,
		"grep: trailing backslash (\\)":                                       true,
		"grep: invalid regular expression":                                    true,
		"grep: invalid character class":                                       true,
		"grep: invalid character range":                                       true,
		"grep: invalid backreference number":                                  true,
		"grep: invalid repetition count(s)":                                   true,
		"grep: invalid collating element":                                     true,
		"grep: repetition-operator operand invalid":                           true,
		"grep: empty (sub)expression":                                         true,
		"egrep: parentheses not balanced":                                     true,
		"grep: ledger.go: No such file or directory":                          false,
		"grep: out of memory":                                                 false,
		"sh: 1: Syntax error: \"(\" unexpected":                               true,
		"doc: no method or field Ledger.Digest in package ./":                 false,
		"--- FAIL: TestX (0.00s)\n    usage: wrong\nFAIL\texample 0.1s":       false,
		"": false,
	} {
		if got := Malformed(out) != ""; got != want {
			t.Errorf("Malformed(%q) = %v, want %v", out, got, want)
		}
	}
}

// The cheap check before any work starts: a plan's `go doc` criteria in the
// refused form are respelled where they stand, and nothing else is touched.
func TestFixPlanRespellsTheRefusedGoDocForm(t *testing.T) {
	plan := map[string]any{
		"prd": map[string]any{"acceptance_criteria": []any{
			"3. go doc ./ ledger.Ledger.Checksum (run from the repo root) prints func (l *Ledger) Checksum() uint32.",
			"`go doc ledger.Ledger.Checksum` prints it",
			"`go doc -all ./ Ledger` lists Checksum",
		}},
		"issues": []any{map[string]any{"acceptance_criteria": []any{"`go doc -short . ledger.Ledger.Checksum` exits 0"}}},
	}
	rw := FixPlan(plan)
	if len(rw) != 2 {
		t.Fatalf("rewrites = %+v", rw)
	}
	ac := plan["prd"].(map[string]any)["acceptance_criteria"].([]any)
	if ac[0] != "3. go doc ./ Ledger.Checksum (run from the repo root) prints func (l *Ledger) Checksum() uint32." {
		t.Fatalf("prd criterion = %q", ac[0])
	}
	if ac[1] != "`go doc ledger.Ledger.Checksum` prints it" || ac[2] != "`go doc -all ./ Ledger` lists Checksum" {
		t.Fatalf("a valid form was touched: %q", ac)
	}
	issue := plan["issues"].([]any)[0].(map[string]any)["acceptance_criteria"].([]any)[0]
	if issue != "`go doc -short . Ledger.Checksum` exits 0" {
		t.Fatalf("issue criterion = %q", issue)
	}
}

func TestRepairLeavesTheOneArgumentFormAlone(t *testing.T) {
	const tooMany = "doc: too many periods in symbol specification"
	for line, want := range map[string]string{
		"go doc ./ ledger.Ledger.Checksum":      "go doc ./ Ledger.Checksum",
		"go doc -u ./x pkg.T.M && go vet ./...": "go doc -u ./x T.M && go vet ./...",
		"go doc ledger.Ledger.Checksum":         "",
		"go doc -all ledger.Ledger.Checksum":    "",
	} {
		if got := Repair(line, tooMany); got != want {
			t.Errorf("Repair(%q) = %q, want %q", line, got, want)
		}
	}
}

// Issue #184: `git diff -U0 main...HEAD -- textutil/ | grep -c '^-[^-]' | grep -qx 0`
// exits 0 under ordinary sh/bash when there are no deletions, but exits 1 under
// `bash -o pipefail` because `grep -c` exits 1 when it counts 0 matches.
// FixPlan must rewrite the zero-match pipeline so it gives the same verdict
// with and without pipefail, and Reconcile must repair it when verification
// fails on an addition-only diff while still failing when a line was deleted.
func TestZeroMatchGrepCountPipelineUnderPipefail(t *testing.T) {
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("git not installed")
	}
	if _, err := exec.LookPath("bash"); err != nil {
		t.Skip("bash not installed")
	}

	t.Run("FixPlan rewrites zero-count grep pipeline to pass under both bash and bash -o pipefail", func(t *testing.T) {
		orig := "12. `git diff -U0 main...HEAD -- textutil/ | grep -c '^-[^-]' | grep -qx 0` exits 0: the diff deletes no lines."
		nonZero := "11. `git diff --name-only main...HEAD | grep -c '^textutil/textutil' | grep -qx 2` exits 0"
		plan := map[string]any{
			"prd": map[string]any{
				"acceptance_criteria": []any{orig, nonZero},
			},
		}
		rw := FixPlan(plan)
		if len(rw) != 1 {
			t.Fatalf("FixPlan rewrites = %+v, want 1", rw)
		}
		ac := plan["prd"].(map[string]any)["acceptance_criteria"].([]any)
		if ac[1] != nonZero {
			t.Fatalf("non-zero count pipeline was modified: %q", ac[1])
		}
		cmds := Commands(ac[0].(string))
		if len(cmds) != 1 {
			t.Fatalf("Commands(%q) = %v, want 1 command", ac[0], cmds)
		}
		repairedCmd := strings.Replace(cmds[0], "git diff -U0 main...HEAD -- textutil/", "printf 'unchanged\\n'", 1)
		for _, argv := range [][]string{
			{"bash", "-c", repairedCmd},
			{"bash", "-o", "pipefail", "-c", repairedCmd},
		} {
			if out, err := exec.Command(argv[0], argv[1:]...).CombinedOutput(); err != nil {
				t.Fatalf("%v failed (%v): %s", argv, err, out)
			}
		}
		deletedCmd := strings.Replace(cmds[0], "git diff -U0 main...HEAD -- textutil/", "printf -- '-deleted\\n'", 1)
		for _, argv := range [][]string{
			{"bash", "-c", deletedCmd},
			{"bash", "-o", "pipefail", "-c", deletedCmd},
		} {
			if err := exec.Command(argv[0], argv[1:]...).Run(); err == nil {
				t.Fatalf("%v succeeded on deleted line, want non-zero exit", argv)
			}
		}
	})

	t.Run("Reconcile repairs zero-deletion diff check and still fails when a deletion exists", func(t *testing.T) {
		repo := t.TempDir()
		if err := os.MkdirAll(filepath.Join(repo, "textutil"), 0o755); err != nil {
			t.Fatal(err)
		}
		runGitT := func(args ...string) {
			t.Helper()
			cmd := exec.Command("git", args...)
			cmd.Dir = repo
			if out, err := cmd.CombinedOutput(); err != nil {
				t.Fatalf("git %v: %v\n%s", args, err, out)
			}
		}
		runGitT("init", "-q", "-b", "main")
		runGitT("config", "user.email", "t@example.com")
		runGitT("config", "user.name", "t")
		if err := os.WriteFile(filepath.Join(repo, "textutil", "textutil.go"),
			[]byte("package textutil\n\nfunc Reverse(s string) string { return s }\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		runGitT("add", ".")
		runGitT("commit", "-q", "-m", "base")
		runGitT("checkout", "-q", "-b", "feature/isblank")
		if err := os.WriteFile(filepath.Join(repo, "textutil", "textutil.go"),
			[]byte("package textutil\n\nfunc Reverse(s string) string { return s }\nfunc IsBlank(s string) bool { return s == \"\" }\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		runGitT("add", ".")
		runGitT("commit", "-q", "-m", "add IsBlank without deletions")

		crit := "12. `git diff -U0 main...HEAD -- textutil/ | grep -c '^-[^-]' | grep -qx 0` exits 0"
		v := map[string]any{
			"passed":  false,
			"summary": "OVERALL: FAIL",
			"criteria_results": []any{
				map[string]any{
					"criterion": crit,
					"passed":    false,
					"evidence":  "exited 1 under bash -o pipefail",
				},
			},
		}
		findings := Reconcile(context.Background(), repo, v)
		if len(findings) != 1 || findings[0].Action != Repaired {
			t.Fatalf("findings = %+v, want 1 Repaired", findings)
		}
		if v["passed"] != true {
			t.Fatalf("verification[passed] = %v, want true", v["passed"])
		}

		// Now commit a real line deletion and confirm Reconcile keeps it failed.
		if err := os.WriteFile(filepath.Join(repo, "textutil", "textutil.go"),
			[]byte("package textutil\n\nfunc IsBlank(s string) bool { return s == \"\" }\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		runGitT("add", ".")
		runGitT("commit", "-q", "-m", "delete Reverse")

		vFail := map[string]any{
			"passed":  false,
			"summary": "OVERALL: FAIL",
			"criteria_results": []any{
				map[string]any{
					"criterion": crit,
					"passed":    false,
					"evidence":  "exited 1",
				},
			},
		}
		findingsFail := Reconcile(context.Background(), repo, vFail)
		if len(findingsFail) != 1 || findingsFail[0].Action != RepairedFailed {
			t.Fatalf("findings on real deletion = %+v, want 1 RepairedFailed", findingsFail)
		}
		if vFail["passed"] != false {
			t.Fatalf("verification[passed] = %v, want false when diff deletes lines", vFail["passed"])
		}
	})
}
