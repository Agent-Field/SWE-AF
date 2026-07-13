package main

import (
	"fmt"
	"path/filepath"
	"regexp"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// roles.go builds the schema-valid output for each reasoner role and performs
// the real git/gh side effects the gitops and coder roles require. Every value
// returned here is a real typed struct (from internal/schemas or a local mirror
// of a product-internal type) so the JSON always matches the harness dest type.

// ---- local mirrors of product-internal (unexported) role output types ----

type sprintOut struct {
	Issues    []schemas.PlannedIssue `json:"issues"`
	Rationale string                 `json:"rationale"`
}

type wsSetupOut struct {
	Workspaces []schemas.WorkspaceInfo `json:"workspaces"`
	Success    bool                    `json:"success"`
}

type wsCleanOut struct {
	Success bool     `json:"success"`
	Cleaned []string `json:"cleaned"`
}

type fixGenOut struct {
	FixIssues []map[string]any `json:"fix_issues"`
	DebtItems []map[string]any `json:"debt_items"`
	Summary   string           `json:"summary"`
}

type issueWriterOut struct {
	IssueName     string `json:"issue_name"`
	IssueFilePath string `json:"issue_file_path"`
	Success       bool   `json:"success"`
}

func ptrBool(b bool) *bool { return &b }
func ptrInt(i int) *int    { return &i }

// ---------------------------------------------------------------------------
// Planning roles
// ---------------------------------------------------------------------------

func rolePM(sc Scenario) any {
	logInvocation("pm", "", "prd", nil)
	return &schemas.PRD{
		ValidatedDescription: sc.Goal,
		AcceptanceCriteria: []string{
			"Each of the four helper modules exists and exposes its named callable.",
			"Each module has a corresponding unit test.",
			"The package is organized under mockpkg/ with tests under tests/.",
		},
		MustHave:    []string{"Four helper modules", "Unit tests"},
		NiceToHave:  []string{"A package README"},
		OutOfScope:  []string{"Packaging/distribution"},
		Assumptions: []string{"Python 3 project"},
		Risks:       []string{"None significant"},
		AskUserForm: nil,
	}
}

func roleScout() any {
	logInvocation("scout", "", "no_services", nil)
	return &schemas.ScoutResult{
		DetectedServices:  []schemas.ServiceCredentialSpec{},
		ScopedCredentials: map[string]string{},
		SkippedServices:   []string{},
		Summary:           "No external services required for this build.",
		AskUserForm:       nil,
	}
}

func roleArchitect(sc Scenario) any {
	logInvocation("architect", "", "architecture", nil)
	comps := make([]schemas.ArchitectureComponent, 0, len(sc.Issues))
	for _, is := range sc.Issues {
		comps = append(comps, schemas.ArchitectureComponent{
			Name:           is.Name,
			Responsibility: is.Title,
			TouchesFiles:   is.FilesToCreate,
			DependsOn:      is.DependsOn,
		})
	}
	return &schemas.Architecture{
		Summary:    "Four independent helper modules, each in its own file with a unit test.",
		Components: comps,
		Interfaces: []string{"module-level callables named after each helper"},
		Decisions: []schemas.ArchitectureDecision{
			{Decision: "One module per helper", Rationale: "Maximizes parallelism and avoids merge conflicts."},
		},
		FileChangesOverview: "Add mockpkg/<name>.py and tests/test_<name>.py per helper.",
	}
}

func roleTechLead() any {
	logInvocation("tech_lead", "", "approved", nil)
	return &schemas.ReviewResult{
		Approved:             true,
		Feedback:             "Architecture is appropriately scoped.",
		ScopeIssues:          []string{},
		ComplexityAssessment: "appropriate",
		Summary:              "Approved.",
	}
}

func roleSprintPlanner(sc Scenario) any {
	logInvocation("sprint_planner", "", fmt.Sprintf("%d_issues", len(sc.Issues)), nil)
	issues := make([]schemas.PlannedIssue, 0, len(sc.Issues))
	for i, is := range sc.Issues {
		seq := i + 1
		g := schemas.IssueGuidance{
			NeedsNewTests:     true,
			EstimatedScope:    "small",
			TouchesInterfaces: false,
			NeedsDeeperQA:     is.NeedsDeeperQA,
			TestingGuidance:   is.TestingGuidance,
			ReviewFocus:       is.ReviewFocus,
			RiskRationale:     is.RiskRationale,
		}
		issues = append(issues, schemas.PlannedIssue{
			Name:                is.Name,
			Title:               is.Title,
			Description:         is.Description,
			AcceptanceCriteria:  is.AcceptanceCriteria,
			DependsOn:           is.DependsOn,
			Provides:            is.Provides,
			EstimatedComplexity: is.EstimatedComplexity,
			FilesToCreate:       is.FilesToCreate,
			FilesToModify:       []string{},
			TestingStrategy:     is.TestingStrategy,
			SequenceNumber:      ptrInt(seq),
			Guidance:            &g,
			TargetRepo:          "",
		})
	}
	return &sprintOut{Issues: issues, Rationale: "Four independent helpers split across two dependency levels."}
}

func roleIssueWriter(prompt string) any {
	issue := issueNameFrom(prompt)
	logInvocation("issue_writer", issue, "written", nil)
	return &issueWriterOut{IssueName: issue, IssueFilePath: "issue-" + issue + ".md", Success: true}
}

// ---------------------------------------------------------------------------
// Coding roles (coder does real git work in its worktree cwd)
// ---------------------------------------------------------------------------

var reSafe = regexp.MustCompile(`[^a-zA-Z0-9_]+`)

func safeName(s string) string {
	s = reSafe.ReplaceAllString(s, "_")
	s = strings.Trim(s, "_")
	if s == "" {
		s = "mod"
	}
	return s
}

func coderFileContent(issue, path string, attempt int) string {
	base := safeName(strings.TrimSuffix(filepath.Base(path), filepath.Ext(path)))
	if strings.Contains(filepath.Base(path), "test_") {
		mod := strings.TrimPrefix(base, "test_")
		return fmt.Sprintf("# coder attempt %d\n\n\ndef %s():\n    assert %q == %q\n", attempt, base, mod, mod)
	}
	return fmt.Sprintf("# coder attempt %d\n\n\ndef %s():\n    \"\"\"Auto-generated helper for issue %s.\"\"\"\n    return %q\n",
		attempt, base, issue, base)
}

func roleCoder(prompt, cwd string, sc Scenario) any {
	issue := issueNameFrom(prompt)
	files := filesToCreateFrom(prompt)
	if len(files) == 0 {
		files = []string{"mockpkg/" + safeName(issue) + ".py", "tests/test_" + safeName(issue) + ".py"}
	}
	attempt := nextCount("coder|" + issue)
	hasFeedback := strings.Contains(prompt, "Feedback from Previous Iteration")

	changed := make([]string, 0, len(files))
	for _, f := range files {
		if err := writeFile(cwd, f, coderFileContent(issue, f, attempt)); err == nil {
			changed = append(changed, f)
		}
	}
	if len(changed) > 0 {
		gitOK(cwd, append([]string{"add", "--"}, changed...)...)
	}
	gitOK(cwd, "commit", "--allow-empty", "-m",
		fmt.Sprintf("issue/%s: implement %s (attempt %d)", issue, issue, attempt))

	logInvocation("coder", issue, "committed", map[string]any{
		"attempt": attempt, "files": changed, "feedback": hasFeedback,
	})
	return &schemas.CoderResult{
		FilesChanged:      changed,
		Summary:           fmt.Sprintf("Implemented %s: %d file(s) (attempt %d).", issue, len(changed), attempt),
		Complete:          true,
		TestsPassed:       ptrBool(true),
		TestSummary:       "1 passed in 0.01s",
		CodebaseLearnings: []string{"Tests live under tests/."},
		AgentRetro:        map[string]any{"worked": "kept the change minimal"},
		RepoName:          "",
	}
}

func roleQA(prompt string) any {
	issue := issueNameFrom(prompt)
	logInvocation("qa", issue, "passed", nil)
	return &schemas.QAResult{
		Passed:       true,
		Summary:      "Tests adequately cover the acceptance criteria and pass.",
		TestFailures: []map[string]any{},
		CoverageGaps: []string{},
	}
}

func roleReviewer(prompt string, sc Scenario) any {
	issue := issueNameFrom(prompt)
	attempt := nextCount("reviewer|" + issue)
	verdict := sc.reviewerVerdict(issue, attempt)

	approved, blocking, summary := true, false, "Approved: acceptance criteria satisfied."
	switch verdict {
	case "block":
		approved, blocking = false, true
		summary = "Blocking: a required acceptance criterion is not yet satisfied."
	case "fix":
		approved, blocking = false, false
		summary = "Changes requested: address the noted gap, then resubmit."
	}
	logInvocation("code_reviewer", issue, verdict, map[string]any{
		"attempt": attempt, "approved": approved, "blocking": blocking,
	})
	return &schemas.CodeReviewResult{
		Approved:  approved,
		Summary:   summary,
		Blocking:  blocking,
		DebtItems: []map[string]any{},
	}
}

// ---------------------------------------------------------------------------
// Advisor roles
// ---------------------------------------------------------------------------

func roleIssueAdvisor(prompt string, sc Scenario) any {
	issue := issueNameFrom(prompt)
	action := "accept_with_debt"
	if a, ok := sc.Advisor[issue]; ok {
		action = a
	}
	// Only take the scripted (retry) action on the first advisor round for this
	// issue; a second round accepts-with-debt so the loop always terminates.
	if nextCount("issue_advisor|"+issue) > 1 {
		action = "accept_with_debt"
	}

	dec := &schemas.IssueAdvisorDecision{
		Action:           schemas.AdvisorAction(action),
		FailureDiagnosis: "First attempt did not satisfy every acceptance criterion.",
		FailureCategory:  "logic",
		Rationale:        "Relaxing to the core criterion lets the issue complete.",
		Confidence:       0.8,
		DownstreamImpact: "None.",
		Summary:          "Advisor decision: " + action,
	}
	if action == "retry_modified" {
		dec.ModifiedAcceptanceCriteria = []string{"Module " + issue + " exposes a callable returning its own name"}
		dec.DroppedCriteria = []string{}
		dec.ModificationJustification = "Deferred the secondary test-coverage criterion."
	}
	if action == "accept_with_debt" {
		dec.MissingFunctionality = []string{}
		dec.DebtSeverity = "low"
	}
	logInvocation("issue_advisor", issue, action, nil)
	return dec
}

func roleRetryAdvisor(prompt string) any {
	logInvocation("retry_advisor", issueNameFrom(prompt), "no_retry", nil)
	return &schemas.RetryAdvice{
		ShouldRetry:     false,
		Diagnosis:       "Non-retryable in the mock scenario.",
		Strategy:        "",
		ModifiedContext: "",
		Confidence:      0.5,
	}
}

func roleReplanner() any {
	logInvocation("replanner", "", "continue", nil)
	return &schemas.ReplanDecision{
		Action:            schemas.ReplanActionContinue,
		Rationale:         "Proceed with the remaining plan unchanged.",
		UpdatedIssues:     []map[string]any{},
		RemovedIssueNames: []string{},
		SkippedIssueNames: []string{},
		NewIssues:         []map[string]any{},
		Summary:           "Continue.",
	}
}

// ---------------------------------------------------------------------------
// Verifier + fix generator (outer verify loop)
// ---------------------------------------------------------------------------

func roleVerifier(sc Scenario) any {
	n := nextCount("verifier|")
	passed := n >= sc.Verifier.FailUntil
	if passed {
		logInvocation("verifier", "", "passed", map[string]any{"call": n})
		return &schemas.VerificationResult{
			Passed: true,
			CriteriaResults: []schemas.CriterionResult{
				{Criterion: "All acceptance criteria", Passed: true, Evidence: "Modules and tests present.", IssueName: ""},
			},
			Summary:        "All acceptance criteria satisfied.",
			SuggestedFixes: []string{},
		}
	}
	logInvocation("verifier", "", "failed", map[string]any{"call": n})
	return &schemas.VerificationResult{
		Passed: false,
		CriteriaResults: []schemas.CriterionResult{
			{Criterion: "Package exposes an aggregate entrypoint", Passed: false, Evidence: "No aggregate module found.", IssueName: ""},
		},
		Summary:        "One acceptance criterion is not yet satisfied.",
		SuggestedFixes: []string{"Add an aggregate wiring module."},
	}
}

func roleFixGenerator() any {
	logInvocation("fix_generator", "", "1_fix_issue", nil)
	return &fixGenOut{
		FixIssues: []map[string]any{
			{
				"name":                 "fix-wiring",
				"title":                "Add aggregate wiring module",
				"description":          "Add mockpkg/wiring.py exposing an aggregate entrypoint.",
				"acceptance_criteria":  []string{"Package exposes an aggregate entrypoint"},
				"depends_on":           []string{},
				"provides":             []string{"fix-wiring"},
				"files_to_create":      []string{"mockpkg/wiring.py"},
				"files_to_modify":      []string{},
				"estimated_complexity": "small",
				"testing_strategy":     "Import check only.",
			},
		},
		DebtItems: []map[string]any{},
		Summary:   "Generated one fix issue for the missing wiring.",
	}
}

// ---------------------------------------------------------------------------
// Gitops roles (real git/gh)
// ---------------------------------------------------------------------------

func roleGitInit(prompt, cwd string) any {
	buildID := firstGroup(reBuildID, prompt)
	goal := firstGroup(reGoal, prompt)
	orig := currentBranch(cwd)
	initialSHA := headSHA(cwd)

	integration := "feature/" + slug(goal)
	if buildID != "" {
		integration = "feature/" + buildID + "-" + slug(goal)
	}
	if _, err := git(cwd, "checkout", "-b", integration); err != nil {
		// Branch may already exist on a re-run — switch to it.
		gitOK(cwd, "checkout", integration)
	}

	_ = mkdirAll(filepath.Join(cwd, ".worktrees"))
	changed := ensureGitignore(cwd, []string{
		".artifacts/", ".worktrees/", ".env", ".DS_Store",
		".agentfield_output.json", ".agentfield_schema.json", ".agentfield-out-*",
		"__pycache__/", "*.pyc",
	})
	if changed {
		gitOK(cwd, "add", "--", ".gitignore")
		gitOK(cwd, "commit", "-m", "chore: add .gitignore for pipeline artifacts")
	}

	remoteURL := ""
	if u, err := git(cwd, "remote", "get-url", "origin"); err == nil {
		remoteURL = u
	}
	logInvocation("git_init", "", "existing", map[string]any{"integration_branch": integration})
	return &schemas.GitInitResult{
		Mode:                "existing",
		OriginalBranch:      orig,
		IntegrationBranch:   integration,
		InitialCommitSHA:    initialSHA,
		Success:             true,
		ErrorMessage:        "",
		RemoteURL:           remoteURL,
		RemoteDefaultBranch: orig,
		RepoName:            "",
	}
}

func roleWorkspaceSetup(prompt, cwd string) any {
	integration := firstGroup(reIntegration, prompt)
	worktreesDir := firstGroup(reWorktreesDir, prompt)
	buildID := firstGroup(reBuildID, prompt)
	issues := setupIssuesFrom(prompt)

	if worktreesDir == "" {
		worktreesDir = filepath.Join(cwd, ".worktrees")
	}

	ws := make([]schemas.WorkspaceInfo, 0, len(issues))
	for _, is := range issues {
		suffix := is.Seq + "-" + is.Name
		if buildID != "" {
			suffix = buildID + "-" + is.Seq + "-" + is.Name
		}
		branch := "issue/" + suffix
		wtPath := filepath.Join(worktreesDir, "issue-"+suffix)

		// Clean any stale worktree/branch from a previous run before creating.
		gitOK(cwd, "worktree", "remove", "--force", wtPath)
		gitOK(cwd, "branch", "-D", branch)

		if _, err := git(cwd, "worktree", "add", wtPath, "-b", branch, integration); err != nil {
			// Fall back to attaching to an existing branch.
			gitOK(cwd, "worktree", "add", wtPath, branch)
		}
		ws = append(ws, schemas.WorkspaceInfo{IssueName: is.Name, BranchName: branch, WorktreePath: wtPath})
	}
	logInvocation("workspace_setup", "", fmt.Sprintf("%d_worktrees", len(ws)), nil)
	return &wsSetupOut{Workspaces: ws, Success: true}
}

func roleMerger(prompt, cwd string) any {
	integration := firstGroup(reIntegration, prompt)
	branches := mergeBranchesFrom(prompt)

	if integration != "" {
		gitOK(cwd, "checkout", integration)
	}
	preSHA := headSHA(cwd)
	merged := make([]string, 0, len(branches))
	failed := make([]string, 0)
	for _, b := range branches {
		if _, err := git(cwd, "merge", b.Branch, "--no-ff", "-m", "Merge "+b.Branch); err != nil {
			gitOK(cwd, "merge", "--abort")
			failed = append(failed, b.Branch)
			continue
		}
		merged = append(merged, b.Branch)
	}
	logInvocation("merger", "", fmt.Sprintf("merged_%d_failed_%d", len(merged), len(failed)), nil)
	return &schemas.MergeResult{
		Success:                  len(failed) == 0,
		MergedBranches:           merged,
		FailedBranches:           failed,
		ConflictResolutions:      []map[string]any{},
		MergeCommitSHA:           headSHA(cwd),
		PreMergeSHA:              preSHA,
		NeedsIntegrationTest:     false,
		IntegrationTestRationale: "Clean merges of independent modules; no cross-feature interaction.",
		Summary:                  fmt.Sprintf("Merged %d branch(es) into %s.", len(merged), integration),
		RepoName:                 "",
	}
}

func roleIntegrationTester() any {
	logInvocation("integration_tester", "", "passed", nil)
	return &schemas.IntegrationTestResult{
		Passed:         true,
		TestsWritten:   []string{},
		TestsRun:       0,
		TestsPassed:    0,
		TestsFailed:    0,
		FailureDetails: []map[string]any{},
		Summary:        "No cross-feature interactions to test; merged modules are independent.",
	}
}

func roleRepoFinalize() any {
	logInvocation("repo_finalize", "", "clean", nil)
	return &schemas.RepoFinalizeResult{
		Success:          true,
		FilesRemoved:     []string{},
		GitignoreUpdated: false,
		Summary:          "Repository already clean; nothing to remove.",
	}
}

func roleWorkspaceCleanup(prompt, cwd string) any {
	targets := cleanupTargetsFrom(prompt)
	cleaned := make([]string, 0, len(targets))
	for _, t := range targets {
		gitOK(cwd, "worktree", "remove", t.Worktree, "--force")
		gitOK(cwd, "branch", "-D", t.Branch)
		cleaned = append(cleaned, t.Worktree)
	}
	gitOK(cwd, "worktree", "prune")
	logInvocation("workspace_cleanup", "", fmt.Sprintf("cleaned_%d", len(cleaned)), nil)
	return &wsCleanOut{Success: true, Cleaned: cleaned}
}

func roleGitHubPR(prompt, cwd string, sc Scenario) any {
	integration := firstGroup(reIntegration, prompt)
	base := firstGroup(reBaseBranch, prompt)
	if base == "" {
		base = "main"
	}

	// Push the integration branch. Prefer the configured origin (gh credential
	// helper); fall back to a tokenized URL so the mock is self-sufficient.
	pushErr := ""
	if _, err := git(cwd, "push", "origin", integration); err != nil {
		remoteURL, _ := git(cwd, "remote", "get-url", "origin")
		if _, err2 := git(cwd, "push", tokenizedRemote(remoteURL), integration+":"+integration); err2 != nil {
			pushErr = err2.Error()
		}
	}
	if pushErr != "" {
		logInvocation("github_pr", "", "push_failed", map[string]any{"error": pushErr})
		return &schemas.GitHubPRResult{Success: false, ErrorMessage: "push failed: " + pushErr}
	}

	title := "Add mock helper package"
	if sc.Goal != "" {
		title = truncate(sc.Goal, 68)
	}
	body := prBody(sc)
	url, num, err := ghPRCreate(cwd, base, integration, title, body)
	if err != nil {
		logInvocation("github_pr", "", "pr_failed", map[string]any{"error": err.Error()})
		return &schemas.GitHubPRResult{Success: false, ErrorMessage: err.Error()}
	}
	logInvocation("github_pr", "", "created", map[string]any{"pr_url": url, "pr_number": num})
	return &schemas.GitHubPRResult{Success: true, PRURL: url, PRNumber: num, ErrorMessage: ""}
}

func prBody(sc Scenario) string {
	var b strings.Builder
	b.WriteString("## Summary\n")
	b.WriteString("- Autonomous SWE-AF build (Go port) — deterministic E2E mock run.\n")
	b.WriteString("- Adds four independent helper modules with unit tests.\n\n")
	b.WriteString("## Changes\n")
	for _, is := range sc.Issues {
		b.WriteString(fmt.Sprintf("- `%s`: %s\n", is.Name, is.Title))
	}
	b.WriteString("\n## Test plan\n- Per-module unit tests under `tests/`.\n\n")
	b.WriteString("🤖 Built with [AgentField SWE-AF](https://github.com/Agent-Field/SWE-AF)\n")
	b.WriteString("🔌 Powered by [AgentField](https://github.com/Agent-Field/agentfield)\n")
	return b.String()
}

var rePRURL = regexp.MustCompile(`https://github\.com/\S+/pull/\d+`)

func truncate(s string, n int) string {
	if len(s) <= n {
		return s
	}
	return s[:n]
}

// ---------------------------------------------------------------------------
// CI / resolve / fast roles (safe fallbacks — not exercised by the default
// swe-planner.build scenario, but present so any invocation stays schema-valid)
// ---------------------------------------------------------------------------

func roleCIFixer(prompt string) any {
	logInvocation("ci_fixer", "", "no_fix", nil)
	return &schemas.CIFixResult{
		Fixed:               false,
		FilesChanged:        []string{},
		Summary:             "No CI failures to fix in the mock scenario.",
		RejectedWorkarounds: []string{},
		ErrorMessage:        "",
	}
}

func rolePRResolver(prompt string) any {
	logInvocation("pr_resolver", "", "no_op", nil)
	return &schemas.PRResolveResult{
		Fixed:               true,
		MergeResolved:       false,
		FilesChanged:        []string{},
		CommitSHAs:          []string{},
		Pushed:              false,
		AddressedComments:   []schemas.AddressedComment{},
		Summary:             "Nothing to resolve in the mock scenario.",
		RejectedWorkarounds: []string{},
		ErrorMessage:        "",
	}
}

func roleFastPlanner(sc Scenario) any {
	logInvocation("fast_planner", "", "tasks", nil)
	tasks := make([]schemas.FastTask, 0, len(sc.Issues))
	for _, is := range sc.Issues {
		tasks = append(tasks, schemas.FastTask{
			Name:               is.Name,
			Title:              is.Title,
			Description:        is.Description,
			AcceptanceCriteria: is.AcceptanceCriteria,
			FilesToCreate:      is.FilesToCreate,
			FilesToModify:      []string{},
			EstimatedMinutes:   5,
		})
	}
	return &schemas.FastPlanResult{Tasks: tasks, Rationale: "Flat task list for fast mode.", FallbackUsed: false}
}
