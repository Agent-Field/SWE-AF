package orch

import (
	"context"
	"fmt"
	"os"
	"path/filepath"
	"strings"
	"sync"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// cloneRepos clones all repos from cfg.Repos concurrently and returns a
// WorkspaceManifest. Ports _clone_repos (app.py:96-223).
//
//   - workspace_root = <dirname(artifactsDir)>/workspace (created).
//   - Each repo: use spec.RepoPath directly if given, else `git clone` (with
//     --branch when set) into workspace_root/<name>. name = mount_point, else the
//     repo name derived from the URL, else the basename of repo_path.
//   - On ANY clone failure the already-cloned dirs are removed and a
//     RuntimeError-equivalent ("Multi-repo clone failed: ...") is returned; no
//     orphaned successful clones remain.
//   - Branches are resolved via `git rev-parse --abbrev-ref HEAD`, falling back to
//     spec.Branch or "HEAD".
//   - Every WorkspaceRepo.GitInitResult is nil at this stage (populated later by
//     _init_all_repos in the DAG executor).
func cloneRepos(ctx context.Context, cfg *config.BuildConfig, artifactsDir string) (*schemas.WorkspaceManifest, error) {
	workspaceRoot := filepath.Join(filepath.Dir(artifactsDir), "workspace")
	if err := os.MkdirAll(workspaceRoot, 0o755); err != nil {
		return nil, err
	}

	var (
		mu          sync.Mutex
		clonedPaths []string
	)

	// _clone_single: returns (repo_name, absolute_path) or an error. Mirrors the
	// nested async _clone_single closure.
	cloneSingle := func(spec schemas.RepoSpec) (string, string, error) {
		name := spec.MountPoint
		if name == "" {
			if spec.RepoURL != "" {
				name = deriveRepoName(spec.RepoURL)
			} else {
				name = filepath.Base(strings.TrimRight(spec.RepoPath, "/"))
			}
		}
		dest := filepath.Join(workspaceRoot, name)

		// repo_path given → use it directly, no clone.
		if spec.RepoPath != "" {
			return name, spec.RepoPath, nil
		}

		gitDir := filepath.Join(dest, ".git")
		if spec.RepoURL != "" && !pathExists(gitDir) {
			if err := os.MkdirAll(dest, 0o755); err != nil {
				return "", "", err
			}
			args := []string{"clone", spec.RepoURL, dest}
			if spec.Branch != "" {
				args = append(args, "--branch", spec.Branch)
			}
			proc := runGit(ctx, "", args...)
			if proc.ExitCode != 0 {
				return "", "", fmt.Errorf("git clone %q failed (exit %d): %s",
					spec.RepoURL, proc.ExitCode, strings.TrimSpace(proc.Stderr))
			}
			mu.Lock()
			clonedPaths = append(clonedPaths, dest)
			mu.Unlock()
		}

		return name, dest, nil
	}

	// Clone all repos concurrently (asyncio.gather with return_exceptions=True).
	type cloneOutcome struct {
		name string
		path string
		err  error
	}
	cloneResults := make([]cloneOutcome, len(cfg.Repos))
	var wg sync.WaitGroup
	for i := range cfg.Repos {
		wg.Add(1)
		go func(i int) {
			defer wg.Done()
			n, p, err := cloneSingle(cfg.Repos[i])
			cloneResults[i] = cloneOutcome{name: n, path: p, err: err}
		}(i)
	}
	wg.Wait()

	// Check for failures; clean up partial (successful) clones and raise.
	var errMsgs []string
	for _, r := range cloneResults {
		if r.err != nil {
			errMsgs = append(errMsgs, r.err.Error())
		}
	}
	if len(errMsgs) > 0 {
		for _, p := range clonedPaths {
			_ = os.RemoveAll(p)
		}
		return nil, fmt.Errorf("Multi-repo clone failed: %s", strings.Join(errMsgs, "; "))
	}

	// Resolve branches concurrently.
	branches := make([]string, len(cfg.Repos))
	var bwg sync.WaitGroup
	for i := range cfg.Repos {
		bwg.Add(1)
		go func(i int) {
			defer bwg.Done()
			branches[i] = resolveBranch(ctx, cfg.Repos[i], cloneResults[i].path)
		}(i)
	}
	bwg.Wait()

	// Build the WorkspaceRepo list.
	repos := make([]schemas.WorkspaceRepo, 0, len(cfg.Repos))
	primaryRepoName := ""
	for i := range cfg.Repos {
		spec := cfg.Repos[i]
		name := cloneResults[i].name
		path := cloneResults[i].path
		repos = append(repos, schemas.WorkspaceRepo{
			RepoName:      name,
			RepoURL:       spec.RepoURL,
			Role:          spec.Role,
			AbsolutePath:  path,
			Branch:        branches[i],
			SparsePaths:   spec.SparsePaths,
			CreatePR:      spec.CreatePR,
			GitInitResult: nil,
		})
		if spec.Role == "primary" {
			primaryRepoName = name
		}
	}

	return &schemas.WorkspaceManifest{
		WorkspaceRoot:   workspaceRoot,
		Repos:           repos,
		PrimaryRepoName: primaryRepoName,
	}, nil
}

// resolveBranch resolves the actual checked-out branch via `git rev-parse
// --abbrev-ref HEAD`, falling back to spec.Branch or "HEAD" on any error/empty
// output. Ports the nested _resolve_branch closure.
func resolveBranch(ctx context.Context, spec schemas.RepoSpec, path string) string {
	fallback := spec.Branch
	if fallback == "" {
		fallback = "HEAD"
	}
	r := runGit(ctx, "", "-C", path, "rev-parse", "--abbrev-ref", "HEAD")
	if r.ExitCode == 0 {
		if out := strings.TrimSpace(r.Stdout); out != "" {
			return out
		}
	}
	return fallback
}

// pathExists reports whether p exists on disk (os.path.exists).
func pathExists(p string) bool {
	_, err := os.Stat(p)
	return err == nil
}

// isDir reports whether p is an existing directory (os.path.isdir).
func isDir(p string) bool {
	fi, err := os.Stat(p)
	return err == nil && fi.IsDir()
}
