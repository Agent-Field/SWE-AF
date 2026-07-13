package main

import (
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
)

// git.go holds the real git/gh helpers the gitops and coder mock roles use. The
// Go port drives every git/workspace operation through the (LLM) CLI agent, so
// the mock must perform the actual git work a real coding agent would — just
// deterministically.

const (
	mockUserName  = "SWE-AF Mock"
	mockUserEmail = "swe-af-mock@example.com"
)

// git runs `git -C dir <args...>` and returns trimmed stdout. Identity flags are
// injected so commit/merge work in freshly cloned repos with no configured user.
func git(dir string, args ...string) (string, error) {
	full := []string{"-C", dir,
		"-c", "user.name=" + mockUserName,
		"-c", "user.email=" + mockUserEmail,
		"-c", "commit.gpgsign=false",
	}
	full = append(full, args...)
	cmd := exec.Command("git", full...)
	cmd.Env = os.Environ()
	out, err := cmd.CombinedOutput()
	s := strings.TrimSpace(string(out))
	if err != nil {
		return s, fmt.Errorf("git %s: %w: %s", strings.Join(args, " "), err, s)
	}
	return s, nil
}

// gitOK runs git ignoring any error (for best-effort cleanup steps).
func gitOK(dir string, args ...string) string {
	out, _ := git(dir, args...)
	return out
}

// headSHA returns the current HEAD commit SHA in dir.
func headSHA(dir string) string {
	sha, _ := git(dir, "rev-parse", "HEAD")
	return sha
}

// currentBranch returns the current branch name in dir.
func currentBranch(dir string) string {
	b, _ := git(dir, "rev-parse", "--abbrev-ref", "HEAD")
	return b
}

// ensureGitignore appends any of the given patterns not already present to
// <dir>/.gitignore, returning whether the file changed.
func ensureGitignore(dir string, patterns []string) bool {
	path := filepath.Join(dir, ".gitignore")
	existing := ""
	if b, err := os.ReadFile(path); err == nil {
		existing = string(b)
	}
	have := map[string]bool{}
	for _, line := range strings.Split(existing, "\n") {
		have[strings.TrimSpace(line)] = true
	}
	var add []string
	for _, p := range patterns {
		if !have[p] {
			add = append(add, p)
		}
	}
	if len(add) == 0 {
		return false
	}
	content := existing
	if content != "" && !strings.HasSuffix(content, "\n") {
		content += "\n"
	}
	content += strings.Join(add, "\n") + "\n"
	if err := os.WriteFile(path, []byte(content), 0o644); err != nil {
		return false
	}
	return true
}

// mkdirAll creates dir (and parents).
func mkdirAll(dir string) error {
	return os.MkdirAll(dir, 0o755)
}

// writeFile writes content to path (relative to dir), creating parent dirs.
func writeFile(dir, rel, content string) error {
	full := filepath.Join(dir, rel)
	if err := os.MkdirAll(filepath.Dir(full), 0o755); err != nil {
		return err
	}
	return os.WriteFile(full, []byte(content), 0o644)
}

// gh runs `gh <args...>` in dir and returns trimmed combined output.
func gh(dir string, args ...string) (string, error) {
	cmd := exec.Command("gh", args...)
	cmd.Dir = dir
	cmd.Env = os.Environ()
	out, err := cmd.CombinedOutput()
	s := strings.TrimSpace(string(out))
	if err != nil {
		return s, fmt.Errorf("gh %s: %w: %s", strings.Join(args, " "), err, s)
	}
	return s, nil
}

// ghPRCreate creates a NON-draft PR (matching the Go port's github_pr prompt,
// which forbids --draft — draft PRs are unsupported on private free-tier repos)
// and returns (url, number). If a PR already exists for the head branch it is
// reused instead of failing.
func ghPRCreate(dir, base, head, title, body string) (string, int, error) {
	out, err := gh(dir, "pr", "create", "--base", base, "--head", head, "--title", title, "--body", body)
	url := rePRURL.FindString(out)
	if err != nil && url == "" {
		// A PR may already exist for this branch — reuse it.
		if view, verr := gh(dir, "pr", "view", head, "--json", "url,number", "-q", ".url"); verr == nil {
			url = rePRURL.FindString(view)
			if url == "" {
				url = strings.TrimSpace(view)
			}
		}
	}
	if url == "" {
		return "", 0, err
	}
	num := 0
	if m := rePRNumber.FindStringSubmatch(url); len(m) == 2 {
		fmt.Sscanf(m[1], "%d", &num)
	}
	return url, num, nil
}

// tokenizedRemote rewrites an https github remote URL to embed the GH_TOKEN so a
// push authenticates without a credential helper. Non-https URLs are returned
// unchanged.
func tokenizedRemote(url string) string {
	token := strings.TrimSpace(os.Getenv("GH_TOKEN"))
	if token == "" {
		return url
	}
	const p = "https://github.com/"
	if strings.HasPrefix(url, p) {
		return "https://x-access-token:" + token + "@github.com/" + strings.TrimPrefix(url, p)
	}
	return url
}
