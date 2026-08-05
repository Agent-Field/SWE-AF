// Package issue is the issue-level build entry point (sub-harness surface) — a
// 1:1 behavioural port of swe_af/issue/*. A main coding harness (Claude Code,
// Codex, OpenCode) delegates one fully-scoped issue; no planning agents run.
// Deterministic git setup creates an isolated worktree + issue/<build_id>-<slug>
// branch, the existing coding loop implements the issue, an optional single
// verifier pass checks the acceptance criteria, and the branch is returned as
// the deliverable. PR/CI stay off by default — the caller owns merge and CI.
package issue

import (
	"bytes"
	"encoding/json"
	"fmt"
	"regexp"
	"strings"
)

// Spec ports issue/schemas.py::IssueSpec — the caller-provided, fully-scoped
// issue (extra="forbid", title and description required and non-empty).
type Spec struct {
	Title               string   `json:"title"`
	Description         string   `json:"description"`
	Name                string   `json:"name"`
	AcceptanceCriteria  []string `json:"acceptance_criteria"`
	FilesToCreate       []string `json:"files_to_create"`
	FilesToModify       []string `json:"files_to_modify"`
	TestingStrategy     string   `json:"testing_strategy"`
	EstimatedComplexity string   `json:"estimated_complexity"`
	NeedsDeeperQA       bool     `json:"needs_deeper_qa"`
}

// ParseSpec strict-decodes the issue map (mirrors IssueSpec.model_validate).
func ParseSpec(raw map[string]any) (*Spec, error) {
	spec := Spec{EstimatedComplexity: "small"}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil, fmt.Errorf("issue: marshal spec: %w", err)
	}
	dec := json.NewDecoder(bytes.NewReader(b))
	dec.DisallowUnknownFields()
	if err := dec.Decode(&spec); err != nil {
		return nil, fmt.Errorf("issue: invalid issue spec: %w", err)
	}
	if strings.TrimSpace(spec.Title) == "" {
		return nil, fmt.Errorf("issue: title must be a non-empty string")
	}
	if strings.TrimSpace(spec.Description) == "" {
		return nil, fmt.Errorf("issue: description must be a non-empty string")
	}
	return &spec, nil
}

var (
	slugNonAlnum = regexp.MustCompile(`[^0-9a-zA-Z]+`)
	slugDashes   = regexp.MustCompile(`-+`)
)

// Slugify ports issue/schemas.py::slugify — a kebab-case slug safe for git
// branch names and file paths (max 40 chars, "issue" fallback).
func Slugify(text string) string {
	slug := slugNonAlnum.ReplaceAllString(strings.ToLower(strings.TrimSpace(text)), "-")
	slug = strings.Trim(slug, "-")
	slug = slugDashes.ReplaceAllString(slug, "-")
	if len(slug) > 40 {
		slug = slug[:40]
	}
	slug = strings.Trim(slug, "-")
	if slug == "" {
		return "issue"
	}
	return slug
}

// ToPlannedIssue maps the spec onto the internal PlannedIssue dict shape
// consumed by the coding loop (ports IssueSpec.to_planned_issue).
func (s *Spec) ToPlannedIssue(additionalContext string) map[string]any {
	description := s.Description
	if additionalContext != "" {
		description = fmt.Sprintf(
			"%s\n\n## Additional context from the caller\n\n%s",
			description, additionalContext,
		)
	}
	name := s.Name
	if name == "" {
		name = s.Title
	}
	return map[string]any{
		"name":                 Slugify(name),
		"title":                s.Title,
		"description":          description,
		"acceptance_criteria":  toAnySlice(s.AcceptanceCriteria),
		"depends_on":           []any{},
		"provides":             []any{},
		"estimated_complexity": s.EstimatedComplexity,
		"files_to_create":      toAnySlice(s.FilesToCreate),
		"files_to_modify":      toAnySlice(s.FilesToModify),
		"testing_strategy":     s.TestingStrategy,
		"sequence_number":      1,
		"guidance": map[string]any{
			"needs_new_tests":    true,
			"estimated_scope":    s.EstimatedComplexity,
			"touches_interfaces": false,
			"needs_deeper_qa":    s.NeedsDeeperQA,
			"testing_guidance":   s.TestingStrategy,
			"review_focus":       "",
			"risk_rationale":     "",
		},
		"target_repo": "",
	}
}

func toAnySlice(in []string) []any {
	out := make([]any, len(in))
	for i, v := range in {
		out[i] = v
	}
	return out
}
