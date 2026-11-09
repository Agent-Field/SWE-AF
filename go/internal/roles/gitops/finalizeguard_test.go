package gitops

import (
	"context"
	"encoding/json"
	"os"
	"os/exec"
	"path/filepath"
	"reflect"
	"strings"
	"testing"

	"github.com/Agent-Field/agentfield/sdk/go/harness"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

func gitT(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	cmd.Dir = dir
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %v: %v\n%s", args, err, out)
	}
	return strings.TrimSpace(string(out))
}

func writeFiles(t *testing.T, dir string, files map[string]string) {
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
}

func commitAll(t *testing.T, dir, msg string) {
	t.Helper()
	gitT(t, dir, "add", "-A")
	gitT(t, dir, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", msg)
}

const factoryYAML = "start: agent-ready\nmax_size: M\nnever:\n  - auth/**\n"

// Job 77 (issue #265 → PR #275, 2026-11-08): the cleanup agent read the
// repository's own .codeaf/factory.yaml as a pipeline leftover and deleted it
// in "chore: finalize repo for handoff". The guard puts back every file that
// existed before the run, and every non-.gitignore edit, and reports them,
// while the cleanup's legitimate work — removing a build output the run
// committed, extending .gitignore — stands.
func TestFinalizeRestoresTheRepositorysOwnConfig(t *testing.T) {
	t.Setenv(config.EnvStrictBuild, "1")
	repo := t.TempDir()
	gitT(t, repo, "init", "-q", "-b", "main")
	writeFiles(t, repo, map[string]string{
		".codeaf/factory.yaml":     factoryYAML,
		".github/workflows/ci.yml": "on: push\n",
		".gitignore":               "*.log\n",
		"ledger.go":                "package ledger\n",
	})
	commitAll(t, repo, "base")
	base := gitT(t, repo, "rev-parse", "HEAD")

	// The run's own work: a real change, and a build output it committed.
	writeFiles(t, repo, map[string]string{
		"ledger.go":      "package ledger\n\nfunc HasZero() bool { return false }\n",
		"ledger_test.go": "package ledger\n",
		"bin/ledger":     "\x7fELF",
	})
	commitAll(t, repo, "issue/has-zero")

	var prompt string
	agent := func(_ context.Context, p string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
		prompt = p
		// What the cleanup agent did, and more: delete the config dirs, delete
		// the build output, edit source, extend .gitignore, commit.
		for _, rm := range []string{".codeaf", ".github", "bin"} {
			if err := os.RemoveAll(filepath.Join(repo, rm)); err != nil {
				t.Fatal(err)
			}
		}
		writeFiles(t, repo, map[string]string{
			"ledger.go":  "package ledger // tidied\n",
			".gitignore": "*.log\n.artifacts/\n.worktrees/\nbin/\n",
		})
		commitAll(t, repo, "chore: finalize repo for handoff")
		body := `{"success":true,"files_removed":[".codeaf/",".github/workflows/ci.yml","bin/ledger"],"gitignore_updated":true,"summary":"Removed pipeline leftovers."}`
		if err := json.Unmarshal([]byte(body), dest); err != nil {
			return nil, err
		}
		return &harness.Result{Parsed: dest}, nil
	}
	deps, app := newDeps(agent)
	out, err := RunRepoFinalize(context.Background(), deps, map[string]any{"repo_path": repo, "base_ref": base})
	if err != nil {
		t.Fatal(err)
	}
	res := out.(schemas.RepoFinalizeResult)

	// The prompt names the files the run created and protects the config.
	for _, want := range []string{"`bin/ledger`", "`ledger_test.go`", ".codeaf/"} {
		if !strings.Contains(prompt, want) {
			t.Errorf("prompt missing %q:\n%s", want, prompt)
		}
	}
	if strings.Contains(prompt, "`ledger.go`") {
		t.Errorf("prompt lists a base file as the run's: %s", prompt)
	}

	// The repository's own files are back, byte for byte.
	read := func(p string) string { b, _ := os.ReadFile(filepath.Join(repo, p)); return string(b) }
	if read(".codeaf/factory.yaml") != factoryYAML {
		t.Fatalf(".codeaf/factory.yaml not restored: %q", read(".codeaf/factory.yaml"))
	}
	if read(".github/workflows/ci.yml") != "on: push\n" {
		t.Fatal(".github/workflows/ci.yml not restored")
	}
	// The cleanup's edit to source is undone; the run's own change stands.
	if !strings.Contains(read("ledger.go"), "HasZero") || strings.Contains(read("ledger.go"), "tidied") {
		t.Fatalf("ledger.go = %q", read("ledger.go"))
	}
	// Its legitimate work stands.
	if _, err := os.Stat(filepath.Join(repo, "bin/ledger")); !os.IsNotExist(err) {
		t.Fatal("the run's own build output was put back")
	}
	if !strings.Contains(read(".gitignore"), ".worktrees/") {
		t.Fatal("the .gitignore extension was undone")
	}

	// Committed, so the pull request carries the restoration, and clean.
	if st := gitT(t, repo, "status", "--porcelain"); st != "" {
		t.Fatalf("working tree not clean:\n%s", st)
	}
	if d := gitT(t, repo, "diff", "--name-status", base, "HEAD", "--", ".codeaf", ".github"); d != "" {
		t.Fatalf("the branch still changes the config against base:\n%s", d)
	}

	// Reported: files_restored, files_removed without them, the summary, a note.
	want := []string{".codeaf/factory.yaml", ".github/workflows/ci.yml", "ledger.go"}
	if !reflect.DeepEqual(res.FilesRestored, want) {
		t.Fatalf("files_restored = %v, want %v", res.FilesRestored, want)
	}
	if !reflect.DeepEqual(res.FilesRemoved, []string{"bin/ledger"}) {
		t.Fatalf("files_removed = %v", res.FilesRemoved)
	}
	if !strings.Contains(res.Summary, ".codeaf/factory.yaml") {
		t.Fatalf("summary = %q", res.Summary)
	}
	if !hasNoteWithTags(app, "repo_finalize", "restored") {
		t.Fatalf("no restored note: %v", app.notes)
	}
}

// Without a base the guard protects every tracked file: nothing says which
// are the run's, so the cleanup may remove only untracked debris.
func TestFinalizeWithoutABaseProtectsEveryTrackedFile(t *testing.T) {
	t.Setenv(config.EnvStrictBuild, "1")
	repo := t.TempDir()
	gitT(t, repo, "init", "-q", "-b", "main")
	writeFiles(t, repo, map[string]string{".codeaf/factory.yaml": factoryYAML, "out.bin": "x"})
	commitAll(t, repo, "base")

	agent := func(_ context.Context, p string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
		if !strings.Contains(p, "None are known") {
			t.Errorf("prompt does not say no tracked file is the run's:\n%s", p)
		}
		gitT(t, repo, "rm", "-q", "-r", ".codeaf", "out.bin") // left uncommitted
		return &harness.Result{Parsed: dest}, json.Unmarshal([]byte(`{"success":true,"files_removed":[".codeaf","out.bin"]}`), dest)
	}
	deps, _ := newDeps(agent)
	out, err := RunRepoFinalize(context.Background(), deps, map[string]any{"repo_path": repo})
	if err != nil {
		t.Fatal(err)
	}
	res := out.(schemas.RepoFinalizeResult)
	if !reflect.DeepEqual(res.FilesRestored, []string{".codeaf/factory.yaml", "out.bin"}) || len(res.FilesRemoved) != 0 {
		t.Fatalf("restored=%v removed=%v", res.FilesRestored, res.FilesRemoved)
	}
	if st := gitT(t, repo, "status", "--porcelain"); st != "" {
		t.Fatalf("working tree not clean:\n%s", st)
	}
}

// A cleanup that only does its job is left alone: no restore, no commit.
func TestFinalizeLeavesALegitimateCleanupAlone(t *testing.T) {
	t.Setenv(config.EnvStrictBuild, "1")
	repo := t.TempDir()
	gitT(t, repo, "init", "-q", "-b", "main")
	writeFiles(t, repo, map[string]string{".codeaf/factory.yaml": factoryYAML})
	commitAll(t, repo, "base")
	base := gitT(t, repo, "rev-parse", "HEAD")
	writeFiles(t, repo, map[string]string{"debug.log": "x"})
	commitAll(t, repo, "run")

	agent := func(_ context.Context, _ string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
		gitT(t, repo, "rm", "-q", "debug.log")
		writeFiles(t, repo, map[string]string{".gitignore": "*.log\n"})
		commitAll(t, repo, "chore: finalize repo for handoff")
		return &harness.Result{Parsed: dest}, json.Unmarshal([]byte(`{"success":true,"files_removed":["debug.log"],"gitignore_updated":true,"summary":"ok"}`), dest)
	}
	deps, _ := newDeps(agent)
	before := gitT(t, repo, "rev-list", "--count", "HEAD")
	out, err := RunRepoFinalize(context.Background(), deps, map[string]any{"repo_path": repo, "base_ref": base})
	if err != nil {
		t.Fatal(err)
	}
	res := out.(schemas.RepoFinalizeResult)
	if len(res.FilesRestored) != 0 || res.Summary != "ok" {
		t.Fatalf("result = %+v", res)
	}
	if after := gitT(t, repo, "rev-list", "--count", "HEAD"); after != "3" || before != "2" {
		t.Fatalf("commits before=%s after=%s, want 2 then 3 (the cleanup's own only)", before, after)
	}
}

// N3: With SWE_STRICT_BUILD unset, finalize behaves as main's finalize did —
// pre-existing tracked files removed or edited by the cleanup agent are neither
// refused nor restored. With SWE_STRICT_BUILD set (or strict:true threaded from
// build_issue), the guard restores them.
func TestFinalizeGuardGatedByStrictSwitch(t *testing.T) {
	runCase := func(t *testing.T, strictEnv, strictInput bool) (
		repo string, res schemas.RepoFinalizeResult, app *mockApp,
	) {
		t.Helper()
		t.Setenv(config.EnvStrictBuild, "1")
		if !strictEnv {
			if err := os.Unsetenv(config.EnvStrictBuild); err != nil {
				t.Fatal(err)
			}
		}

		repo = t.TempDir()
		gitT(t, repo, "init", "-q", "-b", "main")
		writeFiles(t, repo, map[string]string{
			".codeaf/factory.yaml": factoryYAML,
			"ledger.go":            "package ledger\n",
		})
		commitAll(t, repo, "base")
		base := gitT(t, repo, "rev-parse", "HEAD")

		agent := func(_ context.Context, _ string, _ map[string]any, dest any, _ harness.Options) (*harness.Result, error) {
			if err := os.RemoveAll(filepath.Join(repo, ".codeaf")); err != nil {
				t.Fatal(err)
			}
			writeFiles(t, repo, map[string]string{
				"ledger.go": "package ledger // edited by finalize\n",
			})
			commitAll(t, repo, "chore: finalize repo for handoff")
			body := `{"success":true,"files_removed":[".codeaf/factory.yaml"],"gitignore_updated":false,"summary":"Cleaned up."}`
			return &harness.Result{Parsed: dest}, json.Unmarshal([]byte(body), dest)
		}
		deps, app := newDeps(agent)
		in := map[string]any{"repo_path": repo, "base_ref": base}
		if strictInput {
			in["strict"] = true
		}
		out, err := RunRepoFinalize(context.Background(), deps, in)
		if err != nil {
			t.Fatal(err)
		}
		return repo, out.(schemas.RepoFinalizeResult), app
	}

	t.Run("switch unset lets finalize remove and edit pre-existing files as on main", func(t *testing.T) {
		repo, res, app := runCase(t, false, false)
		if _, err := os.Stat(filepath.Join(repo, ".codeaf/factory.yaml")); !os.IsNotExist(err) {
			t.Fatalf(".codeaf/factory.yaml still exists (err=%v), want removed as on main", err)
		}
		gotLedger, err := os.ReadFile(filepath.Join(repo, "ledger.go"))
		if err != nil || string(gotLedger) != "package ledger // edited by finalize\n" {
			t.Fatalf("ledger.go = %q (%v), want finalize edit preserved", string(gotLedger), err)
		}
		if commits := gitT(t, repo, "rev-list", "--count", "HEAD"); commits != "2" {
			t.Fatalf("commit count = %s, want 2 (base + finalize only, no restore commit)", commits)
		}
		if len(res.FilesRestored) != 0 {
			t.Fatalf("files_restored = %v, want empty", res.FilesRestored)
		}
		if !reflect.DeepEqual(res.FilesRemoved, []string{".codeaf/factory.yaml"}) {
			t.Fatalf("files_removed = %v, want [.codeaf/factory.yaml]", res.FilesRemoved)
		}
		if res.Summary != "Cleaned up." {
			t.Fatalf("summary = %q, want %q", res.Summary, "Cleaned up.")
		}
		if hasNoteWithTags(app, "repo_finalize", "restored") {
			t.Fatalf("unexpected restored note when switch unset: %v", app.notes)
		}
	})

	t.Run("switch set restores pre-existing files", func(t *testing.T) {
		repo, res, app := runCase(t, true, false)
		gotFactory, err := os.ReadFile(filepath.Join(repo, ".codeaf/factory.yaml"))
		if err != nil || string(gotFactory) != factoryYAML {
			t.Fatalf(".codeaf/factory.yaml = %q (%v), want restored", string(gotFactory), err)
		}
		gotLedger, err := os.ReadFile(filepath.Join(repo, "ledger.go"))
		if err != nil || string(gotLedger) != "package ledger\n" {
			t.Fatalf("ledger.go = %q (%v), want restored", string(gotLedger), err)
		}
		wantRestored := []string{".codeaf/factory.yaml", "ledger.go"}
		if !reflect.DeepEqual(res.FilesRestored, wantRestored) {
			t.Fatalf("files_restored = %v, want %v", res.FilesRestored, wantRestored)
		}
		if len(res.FilesRemoved) != 0 {
			t.Fatalf("files_removed = %v, want empty", res.FilesRemoved)
		}
		if !hasNoteWithTags(app, "repo_finalize", "restored") {
			t.Fatalf("missing restored note: %v", app.notes)
		}
	})

	t.Run("strict input flag without switch restores pre-existing files", func(t *testing.T) {
		repo, res, _ := runCase(t, false, true)
		gotFactory, err := os.ReadFile(filepath.Join(repo, ".codeaf/factory.yaml"))
		if err != nil || string(gotFactory) != factoryYAML {
			t.Fatalf(".codeaf/factory.yaml = %q (%v), want restored", string(gotFactory), err)
		}
		wantRestored := []string{".codeaf/factory.yaml", "ledger.go"}
		if !reflect.DeepEqual(res.FilesRestored, wantRestored) {
			t.Fatalf("files_restored = %v, want %v", res.FilesRestored, wantRestored)
		}
	})
}
