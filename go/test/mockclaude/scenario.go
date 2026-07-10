package main

import (
	"encoding/json"
	"os"
)

// scenario.go defines the behavior config the mock replays. A scenario is baked
// in (defaultScenario) and optionally overridden by a JSON file named by
// SWE_MOCK_SCENARIO. The default exercises every control-loop path the E2E test
// asserts on.

// IssueSpec is one issue the sprint_planner mock emits (and the coder later
// implements). The dependency graph across the four default issues produces two
// Kahn levels with a parallel pair on each.
type IssueSpec struct {
	Name                string   `json:"name"`
	Title               string   `json:"title"`
	Description         string   `json:"description"`
	AcceptanceCriteria  []string `json:"acceptance_criteria"`
	DependsOn           []string `json:"depends_on"`
	Provides            []string `json:"provides"`
	FilesToCreate       []string `json:"files_to_create"`
	EstimatedComplexity string   `json:"estimated_complexity"`
	TestingStrategy     string   `json:"testing_strategy"`
	NeedsDeeperQA       bool     `json:"needs_deeper_qa"`
	TestingGuidance     string   `json:"testing_guidance"`
	ReviewFocus         string   `json:"review_focus"`
	RiskRationale       string   `json:"risk_rationale"`
}

// ReviewerRule scripts a code_reviewer's per-attempt verdicts for one issue.
// Verdicts is indexed by (attempt-1); attempt N beyond the list defaults to
// "approve". Each verdict is one of: "approve", "fix", "block".
type ReviewerRule struct {
	Verdicts []string `json:"verdicts"`
}

// VerifierRule scripts the final verifier. It returns passed=false while the
// (1-based) call number is < FailUntil, then passed=true — driving the outer
// verify -> fix -> re-verify loop exactly once when FailUntil == 2.
type VerifierRule struct {
	FailUntil int `json:"fail_until"`
}

// Scenario is the full behavior config.
type Scenario struct {
	Goal     string                  `json:"goal"`
	Issues   []IssueSpec             `json:"issues"`
	Reviewer map[string]ReviewerRule `json:"reviewer"`
	Verifier VerifierRule            `json:"verifier"`
	Advisor  map[string]string       `json:"advisor"` // issue -> first-round advisor action
}

// defaultScenario is the baked-in behavior. Four issues, two levels:
//
//	level 0: alpha, beta   (parallel; beta flagged needs_deeper_qa -> 4-call path)
//	level 1: gamma, delta  (parallel; gamma depends on alpha, delta on beta)
//
// Control-loop coverage:
//   - alpha: reviewer fix -> approve (inner-loop iteration)
//   - gamma: reviewer block -> issue_advisor retry_modified -> approve (middle loop)
//   - verifier: fail once -> generate_fix_issues -> re-verify pass (outer loop)
//   - CI: repo has no workflows -> real ci watcher returns no_checks
func defaultScenario() Scenario {
	mkIssue := func(name, title, desc string, deps []string, deeper bool) IssueSpec {
		return IssueSpec{
			Name:                name,
			Title:               title,
			Description:         desc,
			AcceptanceCriteria:  []string{"Module " + name + " exposes a callable returning its own name", "A unit test covers " + name},
			DependsOn:           deps,
			Provides:            []string{name},
			FilesToCreate:       []string{"mockpkg/" + name + ".py", "tests/test_" + name + ".py"},
			EstimatedComplexity: "small",
			TestingStrategy:     "One unit test per acceptance criterion in tests/test_" + name + ".py",
			NeedsDeeperQA:       deeper,
			TestingGuidance:     "Write exactly one focused unit test for " + name + ".",
			ReviewFocus:         "Correctness of the " + name + " module and its test.",
			RiskRationale:       "Self-contained utility module.",
		}
	}
	return Scenario{
		Goal: "Create a small Python utility package with four independent helper modules and unit tests.",
		Issues: []IssueSpec{
			mkIssue("alpha", "Add alpha helper", "Create the alpha helper module and its unit test.", nil, false),
			mkIssue("beta", "Add beta helper", "Create the beta helper module and its unit test.", nil, true),
			mkIssue("gamma", "Add gamma helper", "Create the gamma helper module (builds on alpha).", []string{"alpha"}, false),
			mkIssue("delta", "Add delta helper", "Create the delta helper module (builds on beta).", []string{"beta"}, false),
		},
		Reviewer: map[string]ReviewerRule{
			"alpha": {Verdicts: []string{"fix", "approve"}},
			"gamma": {Verdicts: []string{"block", "approve"}},
		},
		Verifier: VerifierRule{FailUntil: 2},
		Advisor:  map[string]string{"gamma": "retry_modified"},
	}
}

// loadScenario returns the scenario from SWE_MOCK_SCENARIO (a JSON path) when
// set and readable, else the baked-in default.
func loadScenario() Scenario {
	def := defaultScenario()
	path := os.Getenv("SWE_MOCK_SCENARIO")
	if path == "" {
		return def
	}
	b, err := os.ReadFile(path)
	if err != nil {
		return def
	}
	var s Scenario
	if err := json.Unmarshal(b, &s); err != nil {
		return def
	}
	// Fill any omitted sections from the default so a partial scenario still
	// yields a runnable build.
	if len(s.Issues) == 0 {
		s.Issues = def.Issues
	}
	if s.Reviewer == nil {
		s.Reviewer = def.Reviewer
	}
	if s.Advisor == nil {
		s.Advisor = def.Advisor
	}
	if s.Verifier.FailUntil == 0 {
		s.Verifier.FailUntil = def.Verifier.FailUntil
	}
	if s.Goal == "" {
		s.Goal = def.Goal
	}
	return s
}

// reviewerVerdict returns the scripted verdict for issue at the given 1-based
// attempt, defaulting to "approve".
func (s Scenario) reviewerVerdict(issue string, attempt int) string {
	rule, ok := s.Reviewer[issue]
	if !ok || attempt < 1 || attempt > len(rule.Verdicts) {
		return "approve"
	}
	return rule.Verdicts[attempt-1]
}

// issueByName returns the IssueSpec for name, or nil.
func (s Scenario) issueByName(name string) *IssueSpec {
	for i := range s.Issues {
		if s.Issues[i].Name == name {
			return &s.Issues[i]
		}
	}
	return nil
}
