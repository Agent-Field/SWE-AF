package fast

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"testing"
)

func gitOutput(t *testing.T, dir string, args ...string) string {
	t.Helper()
	cmd := exec.Command("git", args...)
	if dir != "" {
		cmd.Dir = dir
	}
	out, err := cmd.CombinedOutput()
	if err != nil {
		t.Fatalf("git %s failed: %v\n%s", strings.Join(args, " "), err, out)
	}
	return strings.TrimSpace(string(out))
}

func gitCommit(t *testing.T, dir, message string) {
	t.Helper()
	cmd := exec.Command("git", "commit", "-m", message)
	cmd.Dir = dir
	cmd.Env = append(os.Environ(),
		"GIT_AUTHOR_NAME=Fast Build Test",
		"GIT_AUTHOR_EMAIL=fast-build@example.com",
		"GIT_COMMITTER_NAME=Fast Build Test",
		"GIT_COMMITTER_EMAIL=fast-build@example.com",
	)
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Fatalf("git commit failed: %v\n%s", err, out)
	}
}

func makeLocalRemote(t *testing.T) string {
	t.Helper()
	root := t.TempDir()
	remote := filepath.Join(root, "remote.git")
	seed := filepath.Join(root, "seed")
	gitOutput(t, "", "init", "--bare", "--initial-branch=main", remote)
	gitOutput(t, "", "init", "--initial-branch=main", seed)
	if err := os.WriteFile(filepath.Join(seed, "tracked.txt"), []byte("from remote\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitOutput(t, seed, "add", "tracked.txt")
	gitCommit(t, seed, "seed")
	gitOutput(t, seed, "remote", "add", "origin", remote)
	gitOutput(t, seed, "push", "-u", "origin", "main")
	return remote
}

func cloneBuildDeps(t *testing.T, expectedFile string) (*Deps, *callScripter) {
	t.Helper()
	return buildDeps(func(_ context.Context, target string, kwargs map[string]any) (map[string]any, error) {
		switch {
		case strings.HasSuffix(target, ".run_git_init"):
			repoPath := kwargs["repo_path"].(string)
			if _, err := os.Stat(filepath.Join(repoPath, ".git")); err != nil {
				return nil, fmt.Errorf("git_init ran before clone: %w", err)
			}
			if expectedFile != "" {
				if _, err := os.Stat(filepath.Join(repoPath, expectedFile)); err != nil {
					return nil, fmt.Errorf("git_init did not see tracked file: %w", err)
				}
			}
			remote := gitOutput(t, repoPath, "remote", "get-url", "origin")
			return map[string]any{
				"success": true, "integration_branch": "feature/test", "original_branch": "main",
				"initial_commit_sha": gitOutput(t, repoPath, "rev-parse", "HEAD"),
				"mode":               "branch", "remote_url": remote, "remote_default_branch": "main",
			}, nil
		case strings.HasSuffix(target, ".fast_plan_tasks"):
			return planResultFixture, nil
		case strings.HasSuffix(target, ".fast_execute_tasks"):
			return execResultFixture, nil
		case strings.HasSuffix(target, ".fast_verify"):
			return verifyFixture(true), nil
		case strings.HasSuffix(target, ".run_repo_finalize"):
			return finalizeFixture, nil
		case strings.HasSuffix(target, ".run_github_pr"):
			return map[string]any{"pr_url": "https://example.test/pr/52"}, nil
		default:
			return nil, fmt.Errorf("unexpected call target %q", target)
		}
	})
}

func localOnlyBuildDeps() (*Deps, *callScripter) {
	return buildDeps(func(_ context.Context, target string, _ map[string]any) (map[string]any, error) {
		switch {
		case strings.HasSuffix(target, ".run_git_init"):
			return gitInitFixture, nil
		case strings.HasSuffix(target, ".fast_plan_tasks"):
			return planResultFixture, nil
		case strings.HasSuffix(target, ".fast_execute_tasks"):
			return execResultFixture, nil
		case strings.HasSuffix(target, ".fast_verify"):
			return verifyFixture(true), nil
		case strings.HasSuffix(target, ".run_repo_finalize"):
			return finalizeFixture, nil
		default:
			return nil, fmt.Errorf("unexpected call target %q", target)
		}
	})
}

// VC1 and VC2: Build clones before git_init, exposes origin/tracked files, and reaches PR creation.
func TestBuildClone_VC1_VC2_CloneBeforeGitInitAndCreatePR(t *testing.T) {
	remote := makeLocalRemote(t)
	workspaceRoot := t.TempDir()
	t.Setenv("SWE_WORKSPACE_ROOT", workspaceRoot)
	deps, calls := cloneBuildDeps(t, "tracked.txt")

	out, err := Build(context.Background(), deps, map[string]any{
		"goal": "test clone", "repo_url": remote,
	})
	if err != nil {
		t.Fatalf("Build error: %v", err)
	}

	repoPath := filepath.Join(workspaceRoot, "remote")
	if got := gitOutput(t, repoPath, "remote", "get-url", "origin"); got != remote {
		t.Fatalf("origin = %q, want %q", got, remote)
	}
	if got, err := os.ReadFile(filepath.Join(repoPath, "tracked.txt")); err != nil || string(got) != "from remote\n" {
		t.Fatalf("tracked file = %q, %v", got, err)
	}
	if targets := calls.targets(); len(targets) == 0 || targets[0] != "swe-fast.run_git_init" {
		t.Fatalf("first call = %v, want run_git_init after clone", targets)
	}
	if got := asMap(t, out)["pr_url"]; got != "https://example.test/pr/52" {
		t.Fatalf("pr_url = %v", got)
	}
}

// VC3: a repeat Build removes local commits, dirt, and stale worktrees and resets to origin/main.
func TestBuildClone_VC3_ExistingCloneReset(t *testing.T) {
	remote := makeLocalRemote(t)
	workspaceRoot := t.TempDir()
	t.Setenv("SWE_WORKSPACE_ROOT", workspaceRoot)
	deps, _ := cloneBuildDeps(t, "tracked.txt")
	input := map[string]any{"goal": "test reset", "repo_url": remote}
	if _, err := Build(context.Background(), deps, input); err != nil {
		t.Fatalf("first Build error: %v", err)
	}

	repoPath := filepath.Join(workspaceRoot, "remote")
	if err := os.WriteFile(filepath.Join(repoPath, "tracked.txt"), []byte("dirty\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(repoPath, "stray.txt"), []byte("local commit\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitOutput(t, repoPath, "add", "tracked.txt", "stray.txt")
	gitCommit(t, repoPath, "stray local commit")
	if err := os.WriteFile(filepath.Join(repoPath, "tracked.txt"), []byte("dirty after commit\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.MkdirAll(filepath.Join(repoPath, ".worktrees"), 0o755); err != nil {
		t.Fatal(err)
	}

	if _, err := Build(context.Background(), deps, input); err != nil {
		t.Fatalf("second Build error: %v", err)
	}
	if _, err := os.Stat(filepath.Join(repoPath, ".worktrees")); !os.IsNotExist(err) {
		t.Fatalf("stale .worktrees was not removed: %v", err)
	}
	if _, err := os.Stat(filepath.Join(repoPath, "stray.txt")); !os.IsNotExist(err) {
		t.Fatalf("stray commit file was not removed: %v", err)
	}
	got, err := os.ReadFile(filepath.Join(repoPath, "tracked.txt"))
	if err != nil || string(got) != "from remote\n" {
		t.Fatalf("tracked file = %q, %v", got, err)
	}
	if head, origin := gitOutput(t, repoPath, "rev-parse", "HEAD"), gitOutput(t, repoPath, "rev-parse", "origin/main"); head != origin {
		t.Fatalf("HEAD = %s, origin/main = %s", head, origin)
	}
}

// VC5: a failed clone returns git's stderr and does not invoke git_init.
func TestBuildClone_VC5_CloneFailure(t *testing.T) {
	workspaceRoot := t.TempDir()
	t.Setenv("SWE_WORKSPACE_ROOT", workspaceRoot)
	missing := filepath.Join(t.TempDir(), "does-not-exist.git")
	deps, calls := localOnlyBuildDeps()

	_, err := Build(context.Background(), deps, map[string]any{
		"goal": "test failure", "repo_url": missing,
	})
	if err == nil || !strings.Contains(err.Error(), "git clone failed (exit") || !strings.Contains(err.Error(), "does not exist") {
		t.Fatalf("error = %v, want clone exit and git stderr", err)
	}
	if calls.count() != 0 {
		t.Fatalf("pipeline calls = %v, want none", calls.targets())
	}
}

// VC6: repo_path without repo_url is created when absent and existing content is preserved.
func TestBuildClone_VC6_LocalOnlyPath(t *testing.T) {
	root := t.TempDir()
	existing := filepath.Join(root, "existing")
	if err := os.MkdirAll(existing, 0o755); err != nil {
		t.Fatal(err)
	}
	marker := filepath.Join(existing, "keep.txt")
	if err := os.WriteFile(marker, []byte("keep me\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	deps, _ := localOnlyBuildDeps()
	if _, err := Build(context.Background(), deps, map[string]any{"goal": "local", "repo_path": existing}); err != nil {
		t.Fatalf("existing Build error: %v", err)
	}
	if got, err := os.ReadFile(marker); err != nil || string(got) != "keep me\n" {
		t.Fatalf("existing marker = %q, %v", got, err)
	}

	missing := filepath.Join(root, "nested", "missing")
	if _, err := Build(context.Background(), deps, map[string]any{"goal": "local", "repo_path": missing}); err != nil {
		t.Fatalf("missing Build error: %v", err)
	}
	if info, err := os.Stat(missing); err != nil || !info.IsDir() {
		t.Fatalf("missing path was not created as a directory: %v", err)
	}
	if _, err := os.Stat(filepath.Join(existing, ".git")); !os.IsNotExist(err) {
		t.Fatalf("local-only path unexpectedly became a git repo: %v", err)
	}
}

// VC-D: two remotes with the same basename must not share the first derived clone.
func TestBuildClone_VCD_DerivedBasenameCollisionReclonesRequestedRemote(t *testing.T) {
	firstRemote := makeLocalRemote(t)
	requestedRemote := makeLocalRemote(t)
	if filepath.Base(firstRemote) != filepath.Base(requestedRemote) {
		t.Fatalf("test setup did not create a basename collision: %q, %q", firstRemote, requestedRemote)
	}

	workspaceRoot := t.TempDir()
	t.Setenv("SWE_WORKSPACE_ROOT", workspaceRoot)
	deps, _ := cloneBuildDeps(t, "tracked.txt")
	if _, err := Build(context.Background(), deps, map[string]any{
		"goal": "first org", "repo_url": firstRemote,
	}); err != nil {
		t.Fatalf("first Build error: %v", err)
	}

	repoPath := filepath.Join(workspaceRoot, "remote")
	otherOrgMarker := filepath.Join(repoPath, "other-org-only.txt")
	if err := os.WriteFile(otherOrgMarker, []byte("must not survive\n"), 0o644); err != nil {
		t.Fatal(err)
	}

	if _, err := Build(context.Background(), deps, map[string]any{
		"goal": "requested org", "repo_url": requestedRemote,
	}); err != nil {
		t.Fatalf("second Build error: %v", err)
	}
	if got := gitOutput(t, repoPath, "remote", "get-url", "origin"); got != requestedRemote {
		t.Fatalf("origin = %q, want %q", got, requestedRemote)
	}
	if _, err := os.Stat(otherOrgMarker); !os.IsNotExist(err) {
		t.Fatalf("file from the other org survived re-clone: %v", err)
	}
}

// VC-E: a caller-supplied git repository is not fetched, checked out, reset, or deleted.
func TestBuildClone_VCE_CallerSuppliedGitRepoIsNotMutated(t *testing.T) {
	originRemote := makeLocalRemote(t)
	requestedRemote := makeLocalRemote(t)
	checkout := filepath.Join(t.TempDir(), "caller-checkout")
	gitOutput(t, "", "clone", originRemote, checkout)
	gitOutput(t, checkout, "checkout", "-b", "local-only")
	localCommitFile := filepath.Join(checkout, "local-commit.txt")
	if err := os.WriteFile(localCommitFile, []byte("local-only commit\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	gitOutput(t, checkout, "add", "local-commit.txt")
	gitCommit(t, checkout, "local-only commit")
	untrackedFile := filepath.Join(checkout, "untracked.txt")
	if err := os.WriteFile(untrackedFile, []byte("untracked work\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	headBefore := gitOutput(t, checkout, "rev-parse", "HEAD")

	deps, _ := cloneBuildDeps(t, "tracked.txt")
	if _, err := Build(context.Background(), deps, map[string]any{
		"goal": "caller checkout", "repo_path": checkout, "repo_url": requestedRemote,
	}); err != nil {
		t.Fatalf("Build error: %v", err)
	}

	if got := gitOutput(t, checkout, "remote", "get-url", "origin"); got != originRemote {
		t.Fatalf("origin = %q, want caller's %q", got, originRemote)
	}
	if got := gitOutput(t, checkout, "branch", "--show-current"); got != "local-only" {
		t.Fatalf("branch = %q, want local-only", got)
	}
	if got := gitOutput(t, checkout, "rev-parse", "HEAD"); got != headBefore {
		t.Fatalf("HEAD = %q, want unchanged %q", got, headBefore)
	}
	if got, err := os.ReadFile(localCommitFile); err != nil || string(got) != "local-only commit\n" {
		t.Fatalf("local commit file = %q, %v", got, err)
	}
	if got, err := os.ReadFile(untrackedFile); err != nil || string(got) != "untracked work\n" {
		t.Fatalf("untracked file = %q, %v", got, err)
	}
}

// VC-J: leftovers in a derived workspace that is not a repo do not block the clone.
func TestBuildClone_VCJ_StaleDerivedWorkspaceWithoutGit(t *testing.T) {
	remote := makeLocalRemote(t)
	workspaceRoot := t.TempDir()
	stale := filepath.Join(workspaceRoot, "remote")
	if err := os.MkdirAll(stale, 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(stale, "leftover.txt"),
		[]byte("from a build that died\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	t.Setenv("SWE_WORKSPACE_ROOT", workspaceRoot)
	deps, _ := cloneBuildDeps(t, "tracked.txt")

	if _, err := Build(context.Background(), deps, map[string]any{
		"goal": "test stale workspace", "repo_url": remote,
	}); err != nil {
		t.Fatalf("Build error: %v", err)
	}

	if _, err := os.Stat(filepath.Join(stale, "leftover.txt")); !os.IsNotExist(err) {
		t.Fatalf("leftover survived the re-clone: %v", err)
	}
	if got := gitOutput(t, stale, "remote", "get-url", "origin"); got != remote {
		t.Fatalf("origin = %q, want %q", got, remote)
	}
}

// VC-K: a caller's non-empty directory is never deleted to make room for a clone.
func TestBuildClone_VCK_PopulatedCallerPathIsNeverCleared(t *testing.T) {
	remote := makeLocalRemote(t)
	theirs := filepath.Join(t.TempDir(), "theirs")
	if err := os.MkdirAll(theirs, 0o755); err != nil {
		t.Fatal(err)
	}
	keep := filepath.Join(theirs, "keep.txt")
	if err := os.WriteFile(keep, []byte("caller's work\n"), 0o644); err != nil {
		t.Fatal(err)
	}
	deps, _ := cloneBuildDeps(t, "")

	_, err := Build(context.Background(), deps, map[string]any{
		"goal": "test caller path", "repo_path": theirs, "repo_url": remote,
	})
	if err == nil || !strings.Contains(err.Error(), "git clone failed (exit") {
		t.Fatalf("err = %v, want a git clone failure", err)
	}
	if got, readErr := os.ReadFile(keep); readErr != nil || string(got) != "caller's work\n" {
		t.Fatalf("caller file = %q, %v", got, readErr)
	}
}
