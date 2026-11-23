// This file (labels.go) gives every reasoner a person can see as a step a
// short display label, carried as a "label:<text>" reasoner tag on
// registration — the control plane returns tags with the node, so the WorkAF
// engine reads the label instead of turning "run_architect" into "Run
// architect". The control plane has no per-call label, so a reasoner that runs
// once per planned change (FanOut) is labelled by the engine as
// "<label> · <the change's title>", the title being the call's input
// issue.title. HITL-CONTRACT.md's SWE-AF section holds the same table.
package node

import "github.com/Agent-Field/agentfield/sdk/go/agent"

// LabelTagPrefix starts the tag that carries a reasoner's display label.
const LabelTagPrefix = "label:"

// DisplayLabel is one reasoner's words for a person.
type DisplayLabel struct {
	Label  string
	FanOut bool // runs once per planned change: add " · <issue.title>"
}

// DisplayLabels is every reasoner's display label. KEEP IN STEP WITH the
// contract's table; TestEveryReasonerHasADisplayLabel fails on a new reasoner
// that has none.
var DisplayLabels = map[string]DisplayLabel{
	// Entry points.
	"build_issue":     {Label: "Work the issue"},
	"build":           {Label: "Build"},
	"implement_issue": {Label: "Implement issue"},
	"resolve":         {Label: "Update PR"},
	"resume_build":    {Label: "Resume build"},
	// Planning.
	"plan":                  {Label: "Plan"},
	"run_product_manager":   {Label: "Scope the work"},
	"run_environment_scout": {Label: "Check credentials"},
	"run_architect":         {Label: "Design"},
	"run_tech_lead":         {Label: "Review plan"},
	"run_sprint_planner":    {Label: "Split into tasks"},
	"run_issue_writer":      {Label: "Write task", FanOut: true},
	// Repository.
	"run_git_init":          {Label: "Set up repo"},
	"run_workspace_setup":   {Label: "Set up workspace", FanOut: true},
	"run_workspace_cleanup": {Label: "Clean up workspace"},
	"run_merger":            {Label: "Merge tasks"},
	"run_repo_finalize":     {Label: "Tidy repo"},
	// Coding, per planned change.
	"execute":            {Label: "Build tasks"},
	"run_coder":          {Label: "Code", FanOut: true},
	"run_qa":             {Label: "Test", FanOut: true},
	"run_code_reviewer":  {Label: "Review code", FanOut: true},
	"run_qa_synthesizer": {Label: "Weigh review", FanOut: true},
	"run_retry_advisor":  {Label: "Decide retry", FanOut: true},
	"run_issue_advisor":  {Label: "Rethink task", FanOut: true},
	"run_replanner":      {Label: "Replan"},
	// Checking and shipping.
	"run_integration_tester": {Label: "Test together"},
	"run_verifier":           {Label: "Check it works"},
	"generate_fix_issues":    {Label: "Plan fixes"},
	"run_github_pr":          {Label: "Open PR"},
	"run_ci_watcher":         {Label: "Watch CI"},
	"run_ci_fixer":           {Label: "Fix CI"},
	"run_pr_resolver":        {Label: "Update PR"},
	// Engines and utilities.
	"pro_execute":          {Label: "Code", FanOut: true},
	"get_workspace_handle": {Label: "Workspace link"},
	// swe-fast.
	"fast_plan_tasks":    {Label: "Plan"},
	"fast_execute_tasks": {Label: "Code"},
	"fast_verify":        {Label: "Check it works"},
}

// withDisplayLabel adds the label tag to whatever tags opts already set
// (WithReasonerTags replaces, so the merged set is applied last).
func withDisplayLabel(name string, opts []agent.ReasonerOption) []agent.ReasonerOption {
	l, ok := DisplayLabels[name]
	if !ok {
		return opts
	}
	var r agent.Reasoner
	for _, opt := range opts {
		opt(&r)
	}
	tags := append(append([]string(nil), r.Tags...), LabelTagPrefix+l.Label)
	if l.FanOut {
		tags = append(tags, "label_fanout:issue.title")
	}
	return append(opts, agent.WithReasonerTags(tags...))
}
