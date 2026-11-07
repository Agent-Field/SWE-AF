// This file (build_issue.go) is the entry WorkAF dispatches: one GitHub issue,
// as a WorkAF job already holds it, turned into a reviewed build that ends in a
// pull request on that repository. It is a thin translation onto build — the
// issue becomes the goal, the base branch becomes the PR base, and plan review
// is always on, because a job a person filed is a job a person approves before
// any code is written.
package orch

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strconv"
	"strings"
)

// BuildIssueInput is build_issue's whole input. KEEP IT SMALL: it is exactly
// what a WorkAF job carries, and the engine dispatches against these names
// (HITL-CONTRACT.md, SWE-AF section).
type BuildIssueInput struct {
	Title       string `json:"title"`
	Body        string `json:"body"`
	RepoURL     string `json:"repo_url"`
	RepoPath    string `json:"repo_path"`
	BaseBranch  string `json:"base_branch"`
	IssueNumber issueNumber `json:"issue_number"`
	IssueURL    string `json:"issue_url"`
	// PlanReview defaults to true; false skips the plan pause.
	PlanReview *bool `json:"plan_review"`
	// Config is passed to build as its BuildConfig (advanced; usually empty).
	Config map[string]any `json:"config"`
}

// issueNumber takes 83 or "83" (or "#83"): the engine substitutes
// {issue_number} into a pipeline input as text, and a person writes either.
type issueNumber int

func (n *issueNumber) UnmarshalJSON(b []byte) error {
	s := strings.TrimSpace(string(b))
	if s == "null" || s == `""` {
		*n = 0
		return nil
	}
	s = strings.TrimPrefix(strings.Trim(s, `"`), "#")
	v, err := strconv.Atoi(strings.TrimSpace(s))
	if err != nil || v < 0 {
		return fmt.Errorf("issue_number must be a whole number, got %s", string(b))
	}
	*n = issueNumber(v)
	return nil
}

// BuildIssueSchema is the input schema the node advertises for build_issue.
const BuildIssueSchema = `{"type":"object","additionalProperties":false,"required":["title"],"properties":{` +
	`"title":{"type":"string","description":"The issue title"},` +
	`"body":{"type":"string","description":"The issue body, markdown"},` +
	`"repo_url":{"type":"string","description":"Clone URL of the repository (or give repo_path)"},` +
	`"repo_path":{"type":"string","description":"An existing checkout on the node's machine (or give repo_url)"},` +
	`"base_branch":{"type":"string","description":"Branch the pull request targets; default the repository's default branch"},` +
	`"issue_number":{"type":["integer","string"],"description":"The issue number (83 or \"83\"); the pull request closes it"},` +
	`"issue_url":{"type":"string","description":"Link to the issue"},` +
	`"plan_review":{"type":"boolean","description":"Pause for a person to approve the plan (default true)"},` +
	`"config":{"type":"object","description":"Advanced: SWE-AF BuildConfig overrides"}}}`

// BuildIssueDescription is what discovery shows for build_issue.
const BuildIssueDescription = "Work one GitHub issue to a pull request: plans it, pauses for a person " +
	"to approve the plan (request changes re-plans), codes it on the codeaf harness, reviews, " +
	"verifies and opens a pull request that closes the issue. Give it the issue title and body, " +
	"repo_url or repo_path, and optionally base_branch, issue_number and issue_url."

// BuildIssue translates the issue onto build.
func BuildIssue(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := decodeBuildIssue(input)
	if err != nil {
		return nil, err
	}
	return Build(ctx, deps, in.buildInput())
}

func decodeBuildIssue(input map[string]any) (BuildIssueInput, error) {
	var in BuildIssueInput
	raw, err := json.Marshal(input)
	if err != nil {
		return in, err
	}
	if err := json.Unmarshal(raw, &in); err != nil {
		return in, fmt.Errorf("build_issue: %w", err)
	}
	in.Title = strings.TrimSpace(in.Title)
	in.RepoURL = strings.TrimSpace(in.RepoURL)
	in.RepoPath = strings.TrimSpace(in.RepoPath)
	in.BaseBranch = strings.TrimSpace(in.BaseBranch)
	if in.Title == "" {
		return in, errors.New("build_issue: title is required")
	}
	if in.RepoURL == "" && in.RepoPath == "" {
		return in, errors.New("build_issue: give repo_url or repo_path")
	}
	return in, nil
}

// reviewTitle is the short name of the work: "#12: Add a Len() method".
func (in BuildIssueInput) reviewTitle() string {
	if in.IssueNumber > 0 {
		return fmt.Sprintf("#%d: %s", in.IssueNumber, in.Title)
	}
	return in.Title
}

// goal is the issue as build's goal. The pull-request writer reads the goal,
// so the closing reference lives here.
func (in BuildIssueInput) goal() string {
	var b strings.Builder
	if in.IssueNumber > 0 {
		fmt.Fprintf(&b, "Resolve GitHub issue #%d: %s\n", in.IssueNumber, in.Title)
	} else {
		fmt.Fprintf(&b, "Resolve this GitHub issue: %s\n", in.Title)
	}
	// HTML comments in an issue body are for machines (seed markers,
	// templates); no agent should read them and no plan should repeat them.
	if body := strings.TrimSpace(htmlComment.ReplaceAllString(in.Body, "")); body != "" {
		b.WriteString("\n" + body + "\n")
	}
	if in.IssueNumber > 0 {
		fmt.Fprintf(&b, "\nThe pull request description must include \"Closes #%d\".\n", in.IssueNumber)
	}
	return strings.TrimSpace(b.String())
}

func (in BuildIssueInput) additionalContext() string {
	var parts []string
	if in.IssueURL != "" {
		parts = append(parts, "Issue: "+in.IssueURL)
	}
	if in.BaseBranch != "" {
		parts = append(parts, "Base branch: "+in.BaseBranch)
	}
	parts = append(parts, "Keep the change as small as the issue asks for; do not refactor unrelated code.")
	return strings.Join(parts, "\n")
}

func (in BuildIssueInput) buildInput() map[string]any {
	cfg := map[string]any{}
	for k, v := range in.Config {
		cfg[k] = v
	}
	if in.BaseBranch != "" {
		if _, set := cfg["github_pr_base"]; !set {
			cfg["github_pr_base"] = in.BaseBranch
		}
	}
	review := true
	if in.PlanReview != nil {
		review = *in.PlanReview
	}
	out := map[string]any{
		"goal":               in.goal(),
		"additional_context": in.additionalContext(),
		"config":             cfg,
		"plan_review":        review,
		"review_title":       in.reviewTitle(),
	}
	if in.RepoURL != "" {
		out["repo_url"] = in.RepoURL
	}
	if in.RepoPath != "" {
		out["repo_path"] = in.RepoPath
	}
	return out
}
