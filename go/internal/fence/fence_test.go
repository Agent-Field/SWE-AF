package fence

import (
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// The test binary is its own launcher, exactly as the node binary is.
func TestMain(m *testing.M) {
	if os.Getenv(EnvExec) == "1" {
		os.Exit(Main(os.Args[1:]))
	}
	os.Exit(m.Run())
}

func TestScopeIsTheBuildsOwnFolder(t *testing.T) {
	root := t.TempDir()
	cr := canonical(root)
	for _, c := range []struct{ cwd, allow string }{
		{filepath.Join(root, "demo-a"), filepath.Join(cr, "demo-a")},
		{filepath.Join(root, "demo-a", ".worktrees", "issue-01"), filepath.Join(cr, "demo-a")},
		{"/opt/elsewhere/repo", "/opt/elsewhere/repo"},
	} {
		s, ok := ScopeFor(root, c.cwd)
		if !ok || s.Deny != cr || s.Allow != c.allow {
			t.Errorf("ScopeFor(%q) = %+v %v, want deny %q allow %q", c.cwd, s, ok, cr, c.allow)
		}
	}
	if _, ok := ScopeFor(root, root); ok {
		t.Error("the root itself is no build's folder")
	}
	if _, ok := ScopeFor("", "/x"); ok {
		t.Error("no root, no fence")
	}
}

// BUILD A CANNOT READ BUILD B. Two builds side by side under one workspace
// root, as on the node on 2026-11-07: a run started for build A through the
// launcher, inside the operating system's fence, reads its own folder and
// cannot read build B's plan, list it, or write into it — the same process
// boundary codeaf's file tools and its commands both live inside.
func TestBuildACannotReadBuildB(t *testing.T) {
	requireFence(t)
	root := t.TempDir()
	a := filepath.Join(root, "factory-demo-aaaa")
	b := filepath.Join(root, "factory-demo-bbbb")
	for _, d := range []string{filepath.Join(a, ".artifacts", "plan"), filepath.Join(b, ".artifacts", "plan")} {
		if err := os.MkdirAll(d, 0o755); err != nil {
			t.Fatal(err)
		}
	}
	own := filepath.Join(a, ".artifacts", "plan", "prd.md")
	theirs := filepath.Join(b, ".artifacts", "plan", "prd.md")
	_ = os.WriteFile(own, []byte("PRD for issue #184\n"), 0o644)
	_ = os.WriteFile(theirs, []byte("PRD for issue #83\n"), 0o644)

	scope, ok := ScopeFor(root, filepath.Join(a, ".worktrees", "issue-01"))
	if !ok {
		t.Fatal("no scope")
	}
	sh, _ := exec.LookPath("sh")
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	run := func(script string) (string, error) {
		cmd := exec.Command(self, "-c", script)
		cmd.Dir = a
		env := os.Environ()
		for k, v := range Stamp(nil, sh, scope) {
			env = append(env, k+"="+v)
		}
		cmd.Env = env
		out, err := cmd.CombinedOutput()
		return string(out), err
	}

	if out, err := run("cat " + own); err != nil || !strings.Contains(out, "#184") {
		t.Fatalf("build A cannot read its own plan: %v\n%s", err, out)
	}
	if out, err := run("cat " + theirs); err == nil || strings.Contains(out, "#83") {
		t.Fatalf("build A READ build B's plan:\n%s", out)
	}
	if out, _ := run("cat ../factory-demo-bbbb/.artifacts/plan/prd.md"); strings.Contains(out, "#83") {
		t.Fatalf("build A read build B's plan by a relative path:\n%s", out)
	}
	if out, _ := run("ls " + root); strings.Contains(out, "bbbb") {
		t.Fatalf("build A can list build B:\n%s", out)
	}
	if _, err := run("echo x > " + filepath.Join(b, "planted")); err == nil {
		if _, statErr := os.Stat(filepath.Join(b, "planted")); statErr == nil {
			t.Fatal("build A wrote into build B's folder")
		}
	}
	if out, err := run("echo ok > " + filepath.Join(a, "mine") + " && cat " + filepath.Join(a, "mine")); err != nil || !strings.Contains(out, "ok") {
		t.Fatalf("build A cannot write in its own folder: %v\n%s", err, out)
	}
}

// A fence that cannot apply refuses the run rather than running it open.
func TestNoFenceRefusesTheRun(t *testing.T) {
	if _, err := Command("plan9", "/bin/true", nil, Scope{Deny: "/w", Allow: "/w/a"}); err == nil {
		t.Fatal("an unknown system must refuse")
	}
}

func requireFence(t *testing.T) {
	t.Helper()
	switch runtime.GOOS {
	case "darwin":
		if _, err := os.Stat("/usr/bin/sandbox-exec"); err != nil {
			t.Skip("no sandbox-exec")
		}
	case "linux":
		bw, err := exec.LookPath("bwrap")
		if err != nil {
			t.Skip("no bwrap on this machine")
		}
		if out, err := exec.Command(bw, "--dev-bind", "/", "/", "true").CombinedOutput(); err != nil {
			t.Skipf("bwrap cannot create a namespace here: %v %s", err, out)
		}
	default:
		t.Skip("no fence on " + runtime.GOOS)
	}
}
