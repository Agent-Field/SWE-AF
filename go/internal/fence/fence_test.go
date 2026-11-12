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
	t.Setenv(EnvFence, "1")
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

// V2: SWE_FENCE is opt-in and off by default; truthy values enable the fence,
// and unset or false values leave it off so the launcher runs the binary
// directly without refusing when no OS fence is available.
func TestFenceSwitchIsOptInAndOffByDefault(t *testing.T) {
	for _, off := range []string{"", "0", "false", "FALSE", "no", "off", "OFF", "  "} {
		t.Setenv(EnvFence, off)
		if Enabled() {
			t.Fatalf("SWE_FENCE=%q must leave the fence off", off)
		}
	}
	for _, on := range []string{"1", "true", "TRUE", "yes", "on", "ON", " 1 "} {
		t.Setenv(EnvFence, on)
		if !Enabled() {
			t.Fatalf("SWE_FENCE=%q must enable the fence", on)
		}
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

// gitFixture is a workspace root holding a shared repository and two linked
// worktrees whose metadata lives in it, each in its own build's folder, plus
// a third worktree inside the repository's own build folder (the r4 layout).
type gitFixture struct {
	root, shared, mine, theirs, inside string
}

func newGitFixture(t *testing.T) gitFixture {
	t.Helper()
	if _, err := exec.LookPath("git"); err != nil {
		t.Skip("no git")
	}
	root := canonical(t.TempDir())
	f := gitFixture{
		root:   root,
		shared: filepath.Join(root, "factory-demo-aaaa"),
		mine:   filepath.Join(root, "factory-demo-bbbb", "wt"),
		theirs: filepath.Join(root, "factory-demo-cccc", "wt"),
		inside: filepath.Join(root, "factory-demo-aaaa", ".worktrees", "issue-01"),
	}
	git := func(dir string, args ...string) {
		t.Helper()
		cmd := exec.Command("git", args...)
		cmd.Dir = dir
		cmd.Env = gitEnv()
		if out, err := cmd.CombinedOutput(); err != nil {
			t.Fatalf("git %v: %v\n%s", args, err, out)
		}
	}
	_ = os.MkdirAll(f.shared, 0o755)
	git(f.shared, "init", "-q", "-b", "main")
	_ = os.WriteFile(filepath.Join(f.shared, "README"), []byte("shared checkout\n"), 0o644)
	git(f.shared, "add", "README")
	git(f.shared, "commit", "-q", "-m", "base")
	_ = os.MkdirAll(filepath.Dir(f.mine), 0o755)
	_ = os.MkdirAll(filepath.Dir(f.theirs), 0o755)
	git(f.shared, "worktree", "add", "-q", "-b", "mine", f.mine)
	git(f.shared, "worktree", "add", "-q", "-b", "theirs", f.theirs)
	git(f.shared, "worktree", "add", "-q", "-b", "inside", f.inside)
	_ = os.WriteFile(filepath.Join(f.theirs, "plan.md"), []byte("their plan for issue #83\n"), 0o644)
	return f
}

func gitEnv() []string {
	return append(os.Environ(), "GIT_AUTHOR_NAME=t", "GIT_AUTHOR_EMAIL=t@example.com",
		"GIT_COMMITTER_NAME=t", "GIT_COMMITTER_EMAIL=t@example.com", "GIT_CONFIG_NOSYSTEM=1")
}

// The scope opens a linked worktree's own gitdir and the shared git dir only
// when they sit outside the build's folder, and never on a `.git` file that
// git's back-link does not confirm.
func TestScopeOpensOnlyThisWorktreesGitMetadata(t *testing.T) {
	f := newGitFixture(t)
	common := filepath.Join(f.shared, ".git")

	s, ok := ScopeFor(f.root, f.mine)
	if !ok || s.Allow != filepath.Dir(f.mine) {
		t.Fatalf("scope %+v %v", s, ok)
	}
	if s.GitCommon != common || s.GitDir != filepath.Join(common, "worktrees", "wt") {
		t.Fatalf("linked worktree's metadata not opened: %+v", s)
	}
	// A subfolder of the worktree finds the same `.git` on the way up.
	sub := filepath.Join(f.mine, "pkg")
	_ = os.MkdirAll(sub, 0o755)
	if s2, _ := ScopeFor(f.root, sub); s2.GitDir != s.GitDir {
		t.Fatalf("from a subfolder: %+v", s2)
	}

	// Inside its own build's folder the metadata is already visible.
	if s, _ := ScopeFor(f.root, f.inside); s.GitDir != "" || s.GitCommon != "" {
		t.Fatalf("metadata inside the build's folder must add nothing: %+v", s)
	}
	// An ordinary repository adds nothing.
	if s, _ := ScopeFor(f.root, f.shared); s.GitDir != "" {
		t.Fatalf("an ordinary repository must add nothing: %+v", s)
	}

	// A `.git` file rewritten to point at another worktree's gitdir is refused:
	// that gitdir's back-link names the other worktree, not this one.
	forged := filepath.Join(f.root, "factory-demo-dddd", "wt")
	_ = os.MkdirAll(forged, 0o755)
	_ = os.WriteFile(filepath.Join(forged, ".git"),
		[]byte("gitdir: "+filepath.Join(common, "worktrees", "wt1")+"\n"), 0o644)
	if s, _ := ScopeFor(f.root, forged); s.GitDir != "" || s.GitCommon != "" {
		t.Fatalf("a forged .git file opened another repository: %+v", s)
	}
	_ = os.WriteFile(filepath.Join(forged, ".git"), []byte("gitdir: "+common+"\n"), 0o644)
	if s, _ := ScopeFor(f.root, forged); s.GitDir != "" || s.GitCommon != "" {
		t.Fatalf("a .git file naming a main git dir opened it: %+v", s)
	}

	env := Stamp(nil, "/bin/codeaf", s)
	if env[EnvGitDir] != s.GitDir || env[EnvGitCommon] != s.GitCommon {
		t.Fatalf("stamp does not carry the git dirs: %v", env)
	}
}

// The generated fences, checked as text where the host cannot run them.
func TestTheFenceRulesForALinkedWorktree(t *testing.T) {
	s := Scope{Deny: "/w", Allow: "/w/b-1", GitCommon: "/w/a-1/repo/.git", GitDir: "/w/a-1/repo/.git/worktrees/wt"}
	p := SeatbeltProfile(s)
	want := []string{
		`(deny file-read* file-write* (subpath "/w"))`,
		`(allow file-read-metadata (literal "/w"))`,
		`(allow file-read* file-write* (subpath "/w/b-1"))`,
		`(allow file-read-metadata (literal "/w/a-1"))`,
		`(allow file-read-metadata (literal "/w/a-1/repo"))`,
		`(allow file-read* file-write* (subpath "/w/a-1/repo/.git"))`,
		`(deny file-read* file-write* (subpath "/w/a-1/repo/.git/worktrees"))`,
		`(allow file-read-metadata (literal "/w/a-1/repo/.git/worktrees"))`,
		`(allow file-read* file-write* (subpath "/w/a-1/repo/.git/worktrees/wt"))`,
	}
	at := -1
	for _, w := range want {
		i := strings.Index(p, w)
		if i < 0 || i < at {
			t.Fatalf("seatbelt rule %q missing or out of order (seatbelt's last match wins):\n%s", w, p)
		}
		at = i
	}
	// The root is stat'able, never listable: no read of its data is allowed.
	if strings.Contains(p, `(allow file-read-data (literal "/w"))`) || strings.Contains(p, `(allow file-read* (literal "/w"))`) {
		t.Fatalf("the root became listable:\n%s", p)
	}
	if strings.Contains(SeatbeltProfile(Scope{Deny: "/w", Allow: "/w/b-1"}), ".git") {
		t.Fatal("a scope without git dirs must not open any")
	}

	if _, err := exec.LookPath("bwrap"); err != nil {
		return
	}
	argv, err := Command("linux", "/bin/codeaf", nil, s)
	if err != nil {
		t.Fatal(err)
	}
	got := strings.Join(argv[1:], " ")
	if !strings.Contains(got, "--tmpfs /w --bind /w/b-1 /w/b-1 --bind /w/a-1/repo/.git /w/a-1/repo/.git "+
		"--tmpfs /w/a-1/repo/.git/worktrees --bind /w/a-1/repo/.git/worktrees/wt /w/a-1/repo/.git/worktrees/wt --die-with-parent") {
		t.Fatalf("bwrap argv: %s", got)
	}
}

// GIT WORKS IN A FENCED LINKED WORKTREE, and the fence still holds. A run
// started through the launcher commits in its own worktree — both when the
// worktree's metadata is in its own build's folder (the r4 layout, where the
// unstattable root broke every git command) and when it lives in another
// folder of the root — and still cannot read the shared checkout, another
// worktree's files or gitdir, or list the root.
func TestAFencedCoderCommitsInItsLinkedWorktree(t *testing.T) {
	requireFence(t)
	t.Setenv(EnvFence, "1")
	f := newGitFixture(t)
	sh, _ := exec.LookPath("sh")
	self, err := os.Executable()
	if err != nil {
		t.Fatal(err)
	}
	run := func(cwd, script string) (string, error) {
		scope, ok := ScopeFor(f.root, cwd)
		if !ok {
			t.Fatal("no scope")
		}
		cmd := exec.Command(self, "-c", script)
		cmd.Dir = cwd
		env := gitEnv()
		for k, v := range Stamp(nil, sh, scope) {
			env = append(env, k+"="+v)
		}
		cmd.Env = env
		out, err := cmd.CombinedOutput()
		return string(out), err
	}
	commit := "echo change > change.txt && git add change.txt && git commit -q -m change && git log --oneline -1 && git status --porcelain"
	for _, wt := range []string{f.inside, f.mine} {
		out, err := run(wt, commit)
		if err != nil || !strings.Contains(out, "change") {
			t.Fatalf("a fenced coder cannot commit in %s: %v\n%s", wt, err, out)
		}
	}
	// The commit landed on the worktree's own branch in the shared repository.
	cmd := exec.Command("git", "log", "--oneline", "-1", "mine")
	cmd.Dir = f.shared
	if out, err := cmd.CombinedOutput(); err != nil || !strings.Contains(string(out), "change") {
		t.Fatalf("branch mine: %v\n%s", err, out)
	}

	common := filepath.Join(f.shared, ".git")
	for _, probe := range []struct{ script, leak string }{
		{"cat " + filepath.Join(f.theirs, "plan.md"), "#83"},
		{"cat " + filepath.Join(f.shared, "README"), "shared checkout"},
		{"cat " + filepath.Join(common, "worktrees", "wt1", "HEAD"), "theirs"},
		{"ls " + filepath.Join(common, "worktrees"), "wt1"},
		{"ls " + f.root, "cccc"},
	} {
		if out, _ := run(f.mine, probe.script); strings.Contains(out, probe.leak) {
			t.Fatalf("the fence leaked %q to %q:\n%s", probe.leak, probe.script, out)
		}
	}
}

func TestProtectedOperatorPathsAreHiddenWhileBuildAndToolchainStayUsable(t *testing.T) {
	t.Run("ScopeFor_Stamp_SeatbeltProfile_and_Command_protect_configured_operator_paths", func(t *testing.T) {
		opDir := canonical(t.TempDir())
		parentNote := filepath.Join(opDir, "NOTES.md")
		if err := os.WriteFile(parentNote, []byte("operator spec note\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		extDir := filepath.Join(opDir, "operator-specs")
		if err := os.MkdirAll(extDir, 0o755); err != nil {
			t.Fatal(err)
		}
		root := filepath.Join(opDir, "workspaces")
		a := filepath.Join(root, "factory-demo-aaaa")
		if err := os.MkdirAll(a, 0o755); err != nil {
			t.Fatal(err)
		}

		t.Setenv("SWE_FENCE_PROTECT", parentNote+string(filepath.ListSeparator)+extDir)
		scope, ok := ScopeFor(root, filepath.Join(a, ".worktrees", "issue-01"))
		if !ok {
			t.Fatal("ScopeFor returned ok=false")
		}
		stamped := Stamp(nil, "/bin/codeaf", scope)
		if !strings.Contains(stamped["SWE_FENCE_PROTECT"], parentNote) || !strings.Contains(stamped["SWE_FENCE_PROTECT"], extDir) {
			t.Fatalf("Stamp SWE_FENCE_PROTECT = %q, want %q and %q", stamped["SWE_FENCE_PROTECT"], parentNote, extDir)
		}

		profile := SeatbeltProfile(scope)
		denyNote := `(deny file-read* file-write* (subpath ` + `"` + parentNote + `"))`
		denyExt := `(deny file-read* file-write* (subpath ` + `"` + extDir + `"))`
		allowBuild := `(allow file-read* file-write* (subpath ` + `"` + a + `"))`
		if !strings.Contains(profile, denyNote) || !strings.Contains(profile, denyExt) {
			t.Fatalf("SeatbeltProfile missing protected path deny rules:\n%s", profile)
		}
		if strings.Index(profile, denyNote) > strings.Index(profile, allowBuild) {
			t.Fatalf("SeatbeltProfile protected deny must precede build allow:\n%s", profile)
		}

		if _, err := exec.LookPath("bwrap"); err != nil {
			stubDir := t.TempDir()
			stub := filepath.Join(stubDir, "bwrap")
			if err := os.WriteFile(stub, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
				t.Fatal(err)
			}
			t.Setenv("PATH", stubDir+string(filepath.ListSeparator)+os.Getenv("PATH"))
		}
		argv, err := Command("linux", "/bin/codeaf", nil, scope)
		if err != nil {
			t.Fatalf("Command(linux): %v", err)
		}
		joined := strings.Join(argv[1:], " ")
		wantLinux := "--ro-bind /dev/null " + parentNote + " --tmpfs " + extDir + " --tmpfs " + root + " --bind " + a + " " + a
		if !strings.Contains(joined, wantLinux) {
			t.Fatalf("bwrap argv = %q, want substring %q", joined, wantLinux)
		}
	})

	t.Run("launcher_hides_protected_parent_marker_and_sibling_build_while_keeping_allowed_build_and_toolchain_usable", func(t *testing.T) {
		requireFence(t)
		t.Setenv(EnvFence, "1")
		opDir := canonical(t.TempDir())
		parentNote := filepath.Join(opDir, "NOTES.md")
		if err := os.WriteFile(parentNote, []byte("operator secret note\n"), 0o644); err != nil {
			t.Fatal(err)
		}
		siblingOpDir := filepath.Join(opDir, "operator-context")
		if err := os.MkdirAll(siblingOpDir, 0o755); err != nil {
			t.Fatal(err)
		}
		siblingOpSpec := filepath.Join(siblingOpDir, "SPEC.md")
		if err := os.WriteFile(siblingOpSpec, []byte("operator sibling spec\n"), 0o644); err != nil {
			t.Fatal(err)
		}

		root := filepath.Join(opDir, "workspaces")
		a := filepath.Join(root, "factory-demo-aaaa")
		b := filepath.Join(root, "factory-demo-bbbb")
		for _, d := range []string{filepath.Join(a, ".artifacts", "plan"), filepath.Join(b, ".artifacts", "plan")} {
			if err := os.MkdirAll(d, 0o755); err != nil {
				t.Fatal(err)
			}
		}
		own := filepath.Join(a, ".artifacts", "plan", "prd.md")
		theirs := filepath.Join(b, ".artifacts", "plan", "prd.md")
		_ = os.WriteFile(own, []byte("PRD for issue #187\n"), 0o644)
		_ = os.WriteFile(theirs, []byte("PRD for issue #83\n"), 0o644)

		t.Setenv("SWE_FENCE_PROTECT", parentNote+string(filepath.ListSeparator)+siblingOpDir)
		scope, ok := ScopeFor(root, filepath.Join(a, ".worktrees", "issue-01"))
		if !ok {
			t.Fatal("no scope")
		}
		sh, err := exec.LookPath("sh")
		if err != nil {
			t.Fatal(err)
		}
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

		if out, err := run("cat " + own); err != nil || !strings.Contains(out, "#187") {
			t.Fatalf("allowed build cannot read its own plan: %v\n%s", err, out)
		}
		if out, err := run("cat " + theirs); err == nil || strings.Contains(out, "#83") {
			t.Fatalf("allowed build read sibling build B's plan:\n%s", out)
		}
		if out, err := run("cat " + parentNote); err == nil || strings.Contains(out, "operator secret note") {
			t.Fatalf("allowed build read protected parent operator note:\n%s", out)
		}
		if out, err := run("cat " + siblingOpSpec); err == nil || strings.Contains(out, "operator sibling spec") {
			t.Fatalf("allowed build read protected operator context directory:\n%s", out)
		}
		if hostBytes, err := os.ReadFile(parentNote); err != nil || !strings.Contains(string(hostBytes), "operator secret note") {
			t.Fatalf("host parent note was altered: %v %q", err, string(hostBytes))
		}
	})
}
