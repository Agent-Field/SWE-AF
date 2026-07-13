package orch

import (
	"context"
	"path/filepath"
	"strings"
	"sync"
	"testing"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// gitMock records invocations and returns a caller-supplied result. It replaces
// the runGit seam for the duration of a test (analogue of patching
// subprocess.run in test_clone_repos.py).
type gitMock struct {
	mu    sync.Mutex
	calls [][]string
	fn    func(dir string, args []string) cmdResult
}

func installGitMock(t *testing.T, fn func(dir string, args []string) cmdResult) *gitMock {
	t.Helper()
	m := &gitMock{fn: fn}
	prev := runGit
	runGit = func(_ context.Context, dir string, args ...string) cmdResult {
		m.mu.Lock()
		m.calls = append(m.calls, append([]string{dir}, args...))
		m.mu.Unlock()
		return m.fn(dir, args)
	}
	t.Cleanup(func() { runGit = prev })
	return m
}

func cfgWithRepos(t *testing.T, specs ...map[string]any) *config.BuildConfig {
	t.Helper()
	cfg, err := config.LoadBuildConfig(map[string]any{"repos": toAnyList(specs)})
	if err != nil {
		t.Fatalf("LoadBuildConfig: %v", err)
	}
	return cfg
}

func toAnyList(specs []map[string]any) []any {
	out := make([]any, len(specs))
	for i, s := range specs {
		out[i] = s
	}
	return out
}

// okBranch returns a successful rev-parse ("main") result.
func okBranch() cmdResult { return cmdResult{Stdout: "main\n", ExitCode: 0} }

func TestCloneReposSingleRepo(t *testing.T) {
	installGitMock(t, func(dir string, args []string) cmdResult {
		if len(args) > 0 && args[0] == "clone" {
			return cmdResult{ExitCode: 0}
		}
		return okBranch()
	})
	cfg := cfgWithRepos(t, map[string]any{
		"repo_url": "https://github.com/org/myrepo.git", "role": "primary",
	})
	art := filepath.Join(t.TempDir(), "artifacts")

	m, err := cloneRepos(context.Background(), cfg, art)
	if err != nil {
		t.Fatalf("cloneRepos: %v", err)
	}
	if len(m.Repos) != 1 {
		t.Fatalf("want 1 repo, got %d", len(m.Repos))
	}
	if m.Repos[0].RepoURL != "https://github.com/org/myrepo.git" || m.Repos[0].Role != "primary" {
		t.Fatalf("repo fields wrong: %+v", m.Repos[0])
	}
	if m.PrimaryRepoName != "myrepo" {
		t.Fatalf("primary_repo_name = %q, want myrepo", m.PrimaryRepoName)
	}
	if m.Repos[0].GitInitResult != nil {
		t.Fatalf("git_init_result should be nil at clone stage")
	}
	// workspace_root = dirname(artifacts) + /workspace
	wantRoot := filepath.Join(filepath.Dir(art), "workspace")
	if m.WorkspaceRoot != wantRoot {
		t.Fatalf("workspace_root = %q, want %q", m.WorkspaceRoot, wantRoot)
	}
}

func TestCloneReposTwoRepos(t *testing.T) {
	installGitMock(t, func(dir string, args []string) cmdResult {
		if len(args) > 0 && args[0] == "clone" {
			return cmdResult{ExitCode: 0}
		}
		return okBranch()
	})
	cfg := cfgWithRepos(t,
		map[string]any{"repo_url": "https://github.com/org/primary.git", "role": "primary"},
		map[string]any{"repo_url": "https://github.com/org/lib.git", "role": "dependency"},
	)
	m, err := cloneRepos(context.Background(), cfg, filepath.Join(t.TempDir(), "artifacts"))
	if err != nil {
		t.Fatalf("cloneRepos: %v", err)
	}
	if len(m.Repos) != 2 {
		t.Fatalf("want 2 repos, got %d", len(m.Repos))
	}
	if m.PrimaryRepoName != "primary" {
		t.Fatalf("primary_repo_name = %q", m.PrimaryRepoName)
	}
	names := map[string]bool{}
	for _, r := range m.Repos {
		names[r.RepoName] = true
	}
	if !names["lib"] {
		t.Fatalf("dependency repo 'lib' missing: %v", names)
	}
}

func TestCloneReposRepoPathSkipsClone(t *testing.T) {
	m := installGitMock(t, func(dir string, args []string) cmdResult { return cmdResult{Stdout: "develop\n"} })
	cfg := cfgWithRepos(t, map[string]any{"repo_path": "/existing/local/repo", "role": "primary"})

	if _, err := cloneRepos(context.Background(), cfg, filepath.Join(t.TempDir(), "artifacts")); err != nil {
		t.Fatalf("cloneRepos: %v", err)
	}
	for _, call := range m.calls {
		for _, a := range call {
			if a == "clone" {
				t.Fatalf("unexpected git clone call: %v", call)
			}
		}
	}
}

func TestCloneReposBranchFallbackToSpecBranch(t *testing.T) {
	installGitMock(t, func(dir string, args []string) cmdResult {
		if len(args) > 0 && args[0] == "clone" {
			return cmdResult{ExitCode: 0}
		}
		return cmdResult{ExitCode: 128, Stderr: "fatal: not a git repo"} // rev-parse fails
	})
	cfg := cfgWithRepos(t, map[string]any{
		"repo_url": "https://github.com/org/myrepo.git", "role": "primary", "branch": "feature-x",
	})
	m, err := cloneRepos(context.Background(), cfg, filepath.Join(t.TempDir(), "artifacts"))
	if err != nil {
		t.Fatal(err)
	}
	if m.Repos[0].Branch != "feature-x" {
		t.Fatalf("branch = %q, want feature-x", m.Repos[0].Branch)
	}
}

func TestCloneReposBranchFallbackToHEAD(t *testing.T) {
	installGitMock(t, func(dir string, args []string) cmdResult {
		if len(args) > 0 && args[0] == "clone" {
			return cmdResult{ExitCode: 0}
		}
		return cmdResult{ExitCode: 128, Stderr: "fatal"}
	})
	cfg := cfgWithRepos(t, map[string]any{"repo_url": "https://github.com/org/myrepo.git", "role": "primary"})
	m, err := cloneRepos(context.Background(), cfg, filepath.Join(t.TempDir(), "artifacts"))
	if err != nil {
		t.Fatal(err)
	}
	if m.Repos[0].Branch != "HEAD" {
		t.Fatalf("branch = %q, want HEAD", m.Repos[0].Branch)
	}
}

func TestCloneReposPartialFailureCleansUp(t *testing.T) {
	installGitMock(t, func(dir string, args []string) cmdResult {
		if len(args) > 0 && args[0] == "clone" {
			// The dependency (lib) clone fails; the primary succeeds.
			for _, a := range args {
				if strings.Contains(a, "lib.git") {
					return cmdResult{ExitCode: 128, Stderr: "fatal: repo not found"}
				}
			}
			return cmdResult{ExitCode: 0}
		}
		return okBranch()
	})
	cfg := cfgWithRepos(t,
		map[string]any{"repo_url": "https://github.com/org/primary.git", "role": "primary"},
		map[string]any{"repo_url": "https://github.com/org/lib.git", "role": "dependency"},
	)
	art := filepath.Join(t.TempDir(), "artifacts")

	_, err := cloneRepos(context.Background(), cfg, art)
	if err == nil {
		t.Fatal("expected error from failed clone")
	}
	if !strings.Contains(err.Error(), "Multi-repo clone failed") {
		t.Fatalf("error = %q, want 'Multi-repo clone failed' prefix", err.Error())
	}
	// The successfully-cloned primary dir must have been removed.
	primaryDir := filepath.Join(filepath.Dir(art), "workspace", "primary")
	if pathExists(primaryDir) {
		t.Fatalf("successful clone dir %s should have been removed", primaryDir)
	}
}

// Guard: manifest built directly (no config) also honours role/name mapping.
func TestCloneReposDirectManifestFields(t *testing.T) {
	installGitMock(t, func(dir string, args []string) cmdResult {
		if len(args) > 0 && args[0] == "clone" {
			return cmdResult{ExitCode: 0}
		}
		return okBranch()
	})
	cfg := &config.BuildConfig{Repos: []schemas.RepoSpec{
		{RepoURL: "https://github.com/org/a.git", Role: "primary", CreatePR: true},
	}}
	m, err := cloneRepos(context.Background(), cfg, filepath.Join(t.TempDir(), "artifacts"))
	if err != nil {
		t.Fatal(err)
	}
	if m.Repos[0].RepoName != "a" || !m.Repos[0].CreatePR {
		t.Fatalf("unexpected repo: %+v", m.Repos[0])
	}
}
