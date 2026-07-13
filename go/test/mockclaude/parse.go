package main

import (
	"regexp"
	"strings"
)

// parse.go extracts the facts each mock role needs from the prompt text the
// harness sends. The user (task) prompt arrives on stdin and carries the
// "CRITICAL OUTPUT REQUIREMENTS" suffix naming the output file; the role's task
// prompt (also on stdin) carries the issue name, file list, branch names, etc.

var (
	reOutputPath   = regexp.MustCompile("([^\\s\"'`]+\\.agentfield_output\\.json)")
	reName         = regexp.MustCompile(`\*\*Name\*\*:\s*(.+)`)
	reFilesCreate  = regexp.MustCompile(`(?s)\*\*Files to create\*\*:\s*\[(.*?)\]`)
	reIntegration  = regexp.MustCompile("\\*\\*Integration branch\\*\\*:\\s*`([^`]+)`")
	reWorktreesDir = regexp.MustCompile("\\*\\*Worktrees directory\\*\\*:\\s*`([^`]+)`")
	reBuildID      = regexp.MustCompile("\\*\\*Build ID\\*\\*:\\s*`([^`]+)`")
	reRepoPath     = regexp.MustCompile("\\*\\*Repository path\\*\\*:\\s*`([^`]+)`")
	reBaseBranch   = regexp.MustCompile("\\*\\*Base branch \\(PR target\\)\\*\\*:\\s*`([^`]+)`")
	reGoal         = regexp.MustCompile(`\*\*Project goal\*\*:\s*(.+)`)
	reSetupIssue   = regexp.MustCompile("issue_name=`([^`]+)`,\\s*seq=`([^`]+)`")
	reMergeBranch  = regexp.MustCompile(`\*\*([^*]+)\*\*\s*\(issue:\s*([^)]+)\)`)
	reCleanBranch  = regexp.MustCompile("Branch:\\s*`([^`]+)`\\s*(?:→|->)\\s*Worktree:\\s*`([^`]+)`")
	rePRNumber     = regexp.MustCompile(`/pull/(\d+)`)
)

// firstGroup returns the first capture group of the first match, trimmed.
func firstGroup(re *regexp.Regexp, s string) string {
	m := re.FindStringSubmatch(s)
	if len(m) < 2 {
		return ""
	}
	return strings.TrimSpace(m[1])
}

// outputPathFrom extracts the .agentfield_output.json path the harness told the
// agent to write.
func outputPathFrom(prompt string) string {
	return firstGroup(reOutputPath, prompt)
}

// issueNameFrom extracts the "- **Name**: <name>" value (coder/reviewer/qa).
func issueNameFrom(prompt string) string {
	return firstGroup(reName, prompt)
}

// filesToCreateFrom parses "- **Files to create**: ['a', 'b']" (a Python list
// repr) into a []string. Returns nil when absent.
func filesToCreateFrom(prompt string) []string {
	inner := firstGroup(reFilesCreate, prompt)
	if inner == "" {
		return nil
	}
	var out []string
	for _, part := range strings.Split(inner, ",") {
		p := strings.TrimSpace(part)
		p = strings.Trim(p, "'\"")
		p = strings.TrimSpace(p)
		if p != "" {
			out = append(out, p)
		}
	}
	return out
}

// setupIssue is one issue parsed from the workspace_setup task prompt.
type setupIssue struct {
	Name string
	Seq  string
}

// setupIssuesFrom parses the "issue_name=`x`, seq=`NN`" lines.
func setupIssuesFrom(prompt string) []setupIssue {
	var out []setupIssue
	for _, m := range reSetupIssue.FindAllStringSubmatch(prompt, -1) {
		out = append(out, setupIssue{Name: strings.TrimSpace(m[1]), Seq: strings.TrimSpace(m[2])})
	}
	return out
}

// mergeBranch is one branch parsed from the merger task prompt.
type mergeBranch struct {
	Branch string
	Issue  string
}

// mergeBranchesFrom parses "**<branch>** (issue: <issue>)" lines from the
// "### Branches to Merge" section.
func mergeBranchesFrom(prompt string) []mergeBranch {
	var out []mergeBranch
	// Restrict to the section after "Branches to Merge" to avoid matching
	// unrelated bold text elsewhere in the prompt.
	body := prompt
	if i := strings.Index(prompt, "Branches to Merge"); i >= 0 {
		body = prompt[i:]
	}
	for _, m := range reMergeBranch.FindAllStringSubmatch(body, -1) {
		branch := strings.TrimSpace(m[1])
		issue := strings.TrimSpace(m[2])
		// Skip section headers accidentally matched.
		if branch == "" || strings.Contains(branch, "\n") {
			continue
		}
		out = append(out, mergeBranch{Branch: branch, Issue: issue})
	}
	return out
}

// cleanupTarget is one worktree/branch parsed from the workspace_cleanup prompt.
type cleanupTarget struct {
	Branch   string
	Worktree string
}

// cleanupTargetsFrom parses "Branch: `x` → Worktree: `y`" lines.
func cleanupTargetsFrom(prompt string) []cleanupTarget {
	var out []cleanupTarget
	for _, m := range reCleanBranch.FindAllStringSubmatch(prompt, -1) {
		out = append(out, cleanupTarget{Branch: strings.TrimSpace(m[1]), Worktree: strings.TrimSpace(m[2])})
	}
	return out
}

// slug produces a lowercase hyphenated slug (max 40 chars) from a goal string,
// matching what the git_init agent is asked to produce.
func slug(s string) string {
	var b strings.Builder
	prevHyphen := false
	for _, r := range strings.ToLower(s) {
		switch {
		case r >= 'a' && r <= 'z', r >= '0' && r <= '9':
			b.WriteRune(r)
			prevHyphen = false
		default:
			if !prevHyphen && b.Len() > 0 {
				b.WriteByte('-')
				prevHyphen = true
			}
		}
	}
	out := strings.Trim(b.String(), "-")
	if len(out) > 40 {
		out = strings.Trim(out[:40], "-")
	}
	if out == "" {
		out = "build"
	}
	return out
}
