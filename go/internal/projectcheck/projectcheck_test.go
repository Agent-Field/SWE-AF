package projectcheck

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"
)

// gitRepo makes a git repository in a temp dir holding files, committed, and
// returns its path and the commit.
func gitRepo(t *testing.T, files map[string]string) (string, string) {
	t.Helper()
	dir := t.TempDir()
	git(t, dir, "init", "-q", "-b", "main")
	commitFiles(t, dir, "base", files)
	return dir, git(t, dir, "rev-parse", "HEAD")
}

func commitFiles(t *testing.T, dir, msg string, files map[string]string) {
	t.Helper()
	for name, body := range files {
		p := filepath.Join(dir, name)
		if err := os.MkdirAll(filepath.Dir(p), 0o755); err != nil {
			t.Fatal(err)
		}
		if err := os.WriteFile(p, []byte(body), 0o644); err != nil {
			t.Fatal(err)
		}
	}
	git(t, dir, "add", "-A")
	git(t, dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
}

func git(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func needGo(t *testing.T) {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("go not installed")
	}
	// No network: a missing module must fail on the spot, not be fetched.
	t.Setenv("GOPROXY", "off")
	t.Setenv("GOFLAGS", "-mod=readonly")
}

const goMod = "module example.com/ledger\n\ngo 1.21\n"
const goodLedger = "package ledger\n\nfunc Sum(xs []int) int {\n\tt := 0\n\tfor _, x := range xs {\n\t\tt += x\n\t}\n\treturn t\n}\n"

// Issue #269's shape: the work imports a module that does not exist.
const brokenDigest = "package ledger\n\nimport \"github.com/santoshkumarradha/hashkit-does-not-exist\"\n\nfunc Digest() string { return hashkit.Sum256Hex(nil) }\n"

func TestDetectReadsTheRepositoryItself(t *testing.T) {
	dir := t.TempDir()
	if got := Detect(dir); len(got) != 0 {
		t.Fatalf("empty dir detected %v", got)
	}
	os.WriteFile(filepath.Join(dir, "go.mod"), []byte(goMod), 0o644)
	os.WriteFile(filepath.Join(dir, "package.json"), []byte(`{"scripts":{"build":"tsc","test":"echo \"Error: no test specified\" && exit 1"}}`), 0o644)
	var got []string
	for _, c := range Detect(dir) {
		got = append(got, c.String())
	}
	// No node_modules: the npm scripts cannot run, so they are not detected.
	if want := []string{"go build ./...", "go test ./..."}; !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
	os.Mkdir(filepath.Join(dir, "node_modules"), 0o755)
	got = nil
	for _, c := range Detect(dir) {
		got = append(got, c.String())
	}
	// npm's placeholder test script is not a test command.
	if want := []string{"go build ./...", "go test ./...", "npm run build"}; !reflect.DeepEqual(got, want) {
		t.Fatalf("got %v want %v", got, want)
	}
}

// The #269 regression at the package level: work that does not compile fails
// the check, with the command and its output as evidence, and the base that
// compiled proves the failure is the run's.
func TestRunFailsWorkThatDoesNotCompile(t *testing.T) {
	needGo(t)
	dir, base := gitRepo(t, map[string]string{"go.mod": goMod, "ledger.go": goodLedger})
	commitFiles(t, dir, "digest", map[string]string{"digest.go": brokenDigest})

	o := Run(context.Background(), dir, base)
	if !o.Ran || o.Passed || o.PreExisting {
		t.Fatalf("outcome = %+v", o)
	}
	if o.FailedCommand != "go build ./..." || o.ExitCode == 0 {
		t.Fatalf("failed %q exit %d", o.FailedCommand, o.ExitCode)
	}
	if !strings.Contains(o.Output, "hashkit-does-not-exist") {
		t.Fatalf("output does not name the missing module:\n%s", o.Output)
	}
	// The base worktree is gone again.
	if wt := git(t, dir, "worktree", "list"); strings.Count(wt, "\n") != 0 {
		t.Fatalf("worktree left behind:\n%s", wt)
	}

	v := map[string]any{"passed": true, "summary": "all structural criteria pass", "criteria_results": []any{}}
	o.Fold(v, "")
	if v["passed"] != false {
		t.Fatal("fold left the verification passed")
	}
	crit := v["criteria_results"].([]any)
	if len(crit) != 1 || !strings.Contains(crit[0].(map[string]any)["criterion"].(string), "go build ./...") {
		t.Fatalf("criteria = %v", crit)
	}
	if !strings.HasPrefix(v["summary"].(string), "OVERALL: FAIL.") {
		t.Fatalf("summary = %q", v["summary"])
	}
}

// A build that was already red before the run is reported, not blamed on it.
func TestRunExcusesAFailureThatPredatesTheRun(t *testing.T) {
	needGo(t)
	dir, base := gitRepo(t, map[string]string{"go.mod": goMod, "ledger.go": goodLedger, "digest.go": brokenDigest})
	commitFiles(t, dir, "readme", map[string]string{"README.md": "hi\n"})

	o := Run(context.Background(), dir, base)
	if !o.Ran || !o.Passed || !o.PreExisting || o.FailedCommand != "go build ./..." {
		t.Fatalf("outcome = %+v", o)
	}
}

func TestRunPassesWorkThatBuildsAndTests(t *testing.T) {
	needGo(t)
	dir, base := gitRepo(t, map[string]string{"go.mod": goMod, "ledger.go": goodLedger})
	commitFiles(t, dir, "test", map[string]string{"ledger_test.go": "package ledger\n\nimport \"testing\"\n\nfunc TestSum(t *testing.T) {\n\tif Sum([]int{1, 2}) != 3 {\n\t\tt.Fatal()\n\t}\n}\n"})
	o := Run(context.Background(), dir, base)
	if !o.Ran || !o.Passed || o.FailedCommand != "" {
		t.Fatalf("outcome = %+v", o)
	}
}

// A test that fails is as red as a build that fails.
func TestRunFailsAFailingTest(t *testing.T) {
	needGo(t)
	dir, base := gitRepo(t, map[string]string{"go.mod": goMod, "ledger.go": goodLedger})
	commitFiles(t, dir, "test", map[string]string{"ledger_test.go": "package ledger\n\nimport \"testing\"\n\nfunc TestSum(t *testing.T) {\n\tif Sum([]int{1, 2}) != 4 {\n\t\tt.Fatal(\"want 4\")\n\t}\n}\n"})
	o := Run(context.Background(), dir, base)
	if o.Passed || o.FailedCommand != "go test ./..." {
		t.Fatalf("outcome = %+v", o)
	}
}

func TestRunSkipsWhenNothingChanged(t *testing.T) {
	dir, base := gitRepo(t, map[string]string{"go.mod": goMod, "ledger.go": brokenDigest})
	calls := 0
	prev := runCommand
	runCommand = func(context.Context, string, []string) (int, string, error) { calls++; return 1, "", nil }
	t.Cleanup(func() { runCommand = prev })
	o := Run(context.Background(), dir, base)
	if o.Ran || !o.Passed || calls != 0 {
		t.Fatalf("outcome = %+v calls=%d", o, calls)
	}
	if o := Run(context.Background(), dir, ""); o.Ran || !o.Passed {
		t.Fatalf("no base: %+v", o)
	}
}
