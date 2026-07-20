package dag

import (
	"context"
	"errors"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/coding"
	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/dagutil"
	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
	"golang.org/x/sync/errgroup"
)

// executeSingleIssue executes a single issue with the Issue Advisor adaptation
// loop (the middle loop). On coding-loop failure the Issue Advisor decides how
// to adapt: retry with modified ACs, retry with a different approach, accept
// with debt, split, or escalate to the replanner. Ports _execute_single_issue.
//
// The returned error is non-nil only for a propagated fatal harness error or
// context cancellation (which executeLevel converts to a FAILED_UNRECOVERABLE
// IssueResult, matching asyncio.gather(return_exceptions=True)).
func executeSingleIssue(
	ctx context.Context,
	issue map[string]any,
	dagState *schemas.DAGState,
	executeFn ExecuteFn,
	cfg *config.ExecutionConfig,
	callFn coding.CallFn,
	nodeID string,
	note noteFunc,
	memoryFn coding.MemoryFn,
) (schemas.IssueResult, error) {
	issueName := asStr(issue["name"])
	originalIssue := copyIssue(issue)
	currentIssue := copyIssue(issue)
	adaptations := []schemas.IssueAdaptation{}
	debtItems := []map[string]any{}

	var lastResult schemas.IssueResult
	haveLast := false

	maxAdvisor := 0
	if cfg.EnableIssueAdvisor {
		maxAdvisor = cfg.MaxAdvisorInvocations
	}

	advisorRound := 0
	for advisorRound = 0; advisorRound <= maxAdvisor; advisorRound++ {
		// --- Run the coding loop (or execute_fn) ---
		var result schemas.IssueResult
		var err error
		switch {
		case executeFn == nil && callFn != nil:
			result, err = coding.RunCodingLoop(ctx, currentIssue, dagState, callFn, nodeID, cfg, note, memoryFn)
		case executeFn != nil:
			result, err = runExecuteFn(ctx, executeFn, currentIssue, dagState, cfg, callFn, nodeID, issueName)
		default:
			return schemas.IssueResult{}, errors.New("No execute_fn or call_fn — cannot execute issue")
		}
		if err != nil {
			return schemas.IssueResult{}, err // fatal / cancellation — propagate
		}

		lastResult = result
		haveLast = true

		// Success — return with any accumulated adaptations/debt.
		if result.Outcome == schemas.IssueOutcomeCompleted || result.Outcome == schemas.IssueOutcomeCompletedWithDebt {
			result.Adaptations = adaptations
			result.DebtItems = debtItems
			result.FinalAcceptanceCriteria = schemas.StrList(asStringSlice(currentIssue["acceptance_criteria"]))
			return result, nil
		}

		// Advisor budget exhausted or disabled — return raw failure.
		if advisorRound >= maxAdvisor || callFn == nil {
			break
		}

		// --- Invoke the Issue Advisor ---
		if note != nil {
			note(fmt.Sprintf("Issue Advisor invocation %d/%d for %s", advisorRound+1, maxAdvisor, issueName),
				[]string{"issue_advisor", "invoke", issueName})
		}

		// Build the advisor kwargs synchronously (snapshotting the shared
		// dagState reads) BEFORE spawning the timeout goroutine. On a timeout
		// the inner goroutine leaks (Go cannot cancel it like asyncio.wait_for
		// cancels the coroutine); evaluating the state reads here keeps that
		// leaked goroutine from racing with RunDAG's post-barrier appends to
		// dagState.completed_issues / failed_issues.
		advisorKwargs := map[string]any{
			"issue":             currentIssue,
			"original_issue":    originalIssue,
			"failure_result":    dumpToMap(result),
			"iteration_history": result.IterationHistory,
			"dag_state_summary": map[string]any{
				"completed_issues":     dumpToMaps(dagState.CompletedIssues),
				"failed_issues":        dumpToMaps(dagState.FailedIssues),
				"prd_summary":          dagState.PRDSummary,
				"architecture_summary": dagState.ArchitectureSummary,
				"prd_path":             dagState.PRDPath,
				"architecture_path":    dagState.ArchitecturePath,
				"issues_dir":           dagState.IssuesDir,
				"artifacts_dir":        dagState.ArtifactsDir,
				"repo_path":            dagState.RepoPath,
			},
			"advisor_invocation":      advisorRound + 1,
			"max_advisor_invocations": maxAdvisor,
			"previous_adaptations":    dumpToMaps(adaptations),
			"worktree_path":           mapGetStr(currentIssue, "worktree_path", dagState.RepoPath),
			"model":                   cfg.IssueAdvisorModel(),
			"ai_provider":             cfg.AIProvider(),
			"workspace_manifest":      dagState.WorkspaceManifest,
		}
		advisorDecision, aerr := callWithTimeout(ctx, cfg.AgentTimeoutSeconds,
			fmt.Sprintf("issue_advisor:%s:%d", issueName, advisorRound+1),
			func(c context.Context) (map[string]any, error) {
				return callFn(c, nodeID+".run_issue_advisor", advisorKwargs)
			})
		if aerr != nil {
			var fhe *fatal.FatalHarnessError
			if errors.As(aerr, &fhe) || errors.Is(aerr, context.Canceled) {
				return schemas.IssueResult{}, aerr // raise
			}
			if note != nil {
				note(fmt.Sprintf("Issue Advisor failed for %s: %v", issueName, aerr),
					[]string{"issue_advisor", "error", issueName})
			}
			break // advisor failed — return last coding loop result
		}

		action := mapGetStr(advisorDecision, "action", "accept_with_debt")
		if note != nil {
			note(fmt.Sprintf("Issue Advisor decision for %s: %s", issueName, action),
				[]string{"issue_advisor", "decision", issueName})
		}

		switch action {
		case string(schemas.AdvisorActionRetryModified):
			adaptation := schemas.IssueAdaptation{
				AdaptationType:             schemas.AdvisorActionRetryModified,
				OriginalAcceptanceCriteria: asStringSlice(currentIssue["acceptance_criteria"]),
				ModifiedAcceptanceCriteria: asStringSlice(advisorDecision["modified_acceptance_criteria"]),
				DroppedCriteria:            asStringSlice(advisorDecision["dropped_criteria"]),
				FailureDiagnosis:           mapGetStr(advisorDecision, "failure_diagnosis", ""),
				Rationale:                  mapGetStr(advisorDecision, "rationale", ""),
				DownstreamImpact:           mapGetStr(advisorDecision, "downstream_impact", ""),
				Severity:                   "medium", // pydantic default preserved
			}
			adaptations = append(adaptations, adaptation)

			for _, dropped := range asStringSlice(advisorDecision["dropped_criteria"]) {
				debtItems = append(debtItems, map[string]any{
					"type":          "dropped_acceptance_criterion",
					"criterion":     dropped,
					"issue_name":    issueName,
					"justification": mapGetStr(advisorDecision, "modification_justification", ""),
					"severity":      "medium",
				})
			}

			modified := asStringSlice(advisorDecision["modified_acceptance_criteria"])
			if modified != nil {
				currentIssue["acceptance_criteria"] = modified
			} else {
				currentIssue["acceptance_criteria"] = asStringSlice(currentIssue["acceptance_criteria"])
			}
			continue // re-enter coding loop

		case string(schemas.AdvisorActionRetryApproach):
			adaptation := schemas.IssueAdaptation{
				AdaptationType:   schemas.AdvisorActionRetryApproach,
				FailureDiagnosis: mapGetStr(advisorDecision, "failure_diagnosis", ""),
				Rationale:        mapGetStr(advisorDecision, "rationale", ""),
				NewApproach:      mapGetStr(advisorDecision, "new_approach", ""),
				DownstreamImpact: mapGetStr(advisorDecision, "downstream_impact", ""),
				Severity:         "medium", // pydantic default preserved
			}
			adaptations = append(adaptations, adaptation)

			currentIssue = copyIssue(currentIssue)
			currentIssue["retry_context"] = mapGetStr(advisorDecision, "new_approach", "")
			currentIssue["approach_changes"] = advisorDecision["approach_changes"]
			currentIssue["previous_error"] = result.ErrorMessage
			currentIssue["retry_diagnosis"] = mapGetStr(advisorDecision, "failure_diagnosis", "")
			continue // re-enter coding loop

		case string(schemas.AdvisorActionAcceptWithDebt):
			adaptation := schemas.IssueAdaptation{
				AdaptationType:       schemas.AdvisorActionAcceptWithDebt,
				FailureDiagnosis:     mapGetStr(advisorDecision, "failure_diagnosis", ""),
				Rationale:            mapGetStr(advisorDecision, "rationale", ""),
				MissingFunctionality: asStringSlice(advisorDecision["missing_functionality"]),
				Severity:             mapGetStr(advisorDecision, "debt_severity", "medium"),
				DownstreamImpact:     mapGetStr(advisorDecision, "downstream_impact", ""),
			}
			adaptations = append(adaptations, adaptation)

			for _, missing := range asStringSlice(advisorDecision["missing_functionality"]) {
				debtItems = append(debtItems, map[string]any{
					"type":        "missing_functionality",
					"description": missing,
					"issue_name":  issueName,
					"severity":    mapGetStr(advisorDecision, "debt_severity", "medium"),
				})
			}

			return schemas.IssueResult{
				IssueName:               issueName,
				Outcome:                 schemas.IssueOutcomeCompletedWithDebt,
				ResultSummary:           mapGetStr(advisorDecision, "summary", result.ResultSummary),
				FilesChanged:            result.FilesChanged,
				BranchName:              result.BranchName,
				Attempts:                result.Attempts,
				AdvisorInvocations:      advisorRound + 1,
				Adaptations:             adaptations,
				DebtItems:               debtItems,
				FinalAcceptanceCriteria: schemas.StrList(asStringSlice(currentIssue["acceptance_criteria"])),
				IterationHistory:        result.IterationHistory,
			}, nil

		case string(schemas.AdvisorActionSplit):
			subIssues := []schemas.SplitIssueSpec{}
			for _, s := range asMapSlice(advisorDecision["sub_issues"]) {
				spec, _ := mapToStruct[schemas.SplitIssueSpec](s)
				subIssues = append(subIssues, spec)
			}
			return schemas.IssueResult{
				IssueName:          issueName,
				Outcome:            schemas.IssueOutcomeFailedNeedsSplit,
				ResultSummary:      mapGetStr(advisorDecision, "split_rationale", ""),
				ErrorMessage:       fmt.Sprintf("Issue advisor recommended splitting into %d sub-issues", len(subIssues)),
				FilesChanged:       result.FilesChanged,
				BranchName:         result.BranchName,
				Attempts:           result.Attempts,
				AdvisorInvocations: advisorRound + 1,
				Adaptations:        adaptations,
				DebtItems:          debtItems,
				SplitRequest:       &subIssues,
				IterationHistory:   result.IterationHistory,
			}, nil

		case string(schemas.AdvisorActionEscalateToReplan):
			return schemas.IssueResult{
				IssueName:          issueName,
				Outcome:            schemas.IssueOutcomeFailedEscalated,
				ResultSummary:      mapGetStr(advisorDecision, "summary", ""),
				ErrorMessage:       mapGetStr(advisorDecision, "escalation_reason", result.ErrorMessage),
				ErrorContext:       result.ErrorContext,
				FilesChanged:       result.FilesChanged,
				BranchName:         result.BranchName,
				Attempts:           result.Attempts,
				AdvisorInvocations: advisorRound + 1,
				Adaptations:        adaptations,
				DebtItems:          debtItems,
				EscalationContext:  mapGetStr(advisorDecision, "suggested_restructuring", ""),
				IterationHistory:   result.IterationHistory,
			}, nil
		default:
			// Unknown action (e.g. an out-of-enum string): fall through and, with
			// no continue/return, let the loop advance like an unhandled decision.
			// Python's if/elif chain has no else, so an unrecognised action simply
			// re-enters the loop; mirror that by continuing.
			continue
		}
	}

	// All advisor rounds exhausted — return last failure result with adaptations.
	if haveLast {
		inv := advisorRound + 1
		if inv > maxAdvisor {
			inv = maxAdvisor
		}
		lastResult.AdvisorInvocations = inv
		lastResult.Adaptations = adaptations
		lastResult.DebtItems = debtItems
		return lastResult, nil
	}

	return schemas.IssueResult{
		IssueName:    issueName,
		Outcome:      schemas.IssueOutcomeFailedUnrecoverable,
		ErrorMessage: "No execution attempted",
		Attempts:     1,
	}, nil
}

// runExecuteFn runs the external execute_fn path with retry logic, wrapping
// execute_fn errors into IssueResult for the advisor loop. Ports _run_execute_fn.
func runExecuteFn(
	ctx context.Context,
	executeFn ExecuteFn,
	issue map[string]any,
	dagState *schemas.DAGState,
	cfg *config.ExecutionConfig,
	callFn coding.CallFn,
	nodeID, issueName string,
) (schemas.IssueResult, error) {
	lastError := ""
	lastContext := ""
	issueWithContext := issue

	for attempt := 1; attempt < cfg.MaxRetriesPerIssue+2; attempt++ {
		result, err := executeFn(ctx, issueWithContext, dagState)
		if err == nil {
			if result != nil {
				return schemas.IssueResult{
					IssueName:     issueName,
					Outcome:       schemas.IssueOutcome(mapGetStr(result, "outcome", "completed")),
					ResultSummary: mapGetStr(result, "result_summary", ""),
					ErrorMessage:  mapGetStr(result, "error_message", ""),
					ErrorContext:  mapGetStr(result, "error_context", ""),
					Attempts:      attempt,
					FilesChanged:  asStringSlice(result["files_changed"]),
					BranchName:    mapGetStr(result, "branch_name", ""),
				}, nil
			}
			// Non-dict / empty result — the str(result)[:500] "other" branch.
			return schemas.IssueResult{
				IssueName:     issueName,
				Outcome:       schemas.IssueOutcomeCompleted,
				ResultSummary: "",
				Attempts:      attempt,
			}, nil
		}

		var fhe *fatal.FatalHarnessError
		if errors.As(err, &fhe) || errors.Is(err, context.Canceled) {
			return schemas.IssueResult{}, err // raise
		}
		lastError = err.Error()
		lastContext = err.Error()

		if attempt <= cfg.MaxRetriesPerIssue && callFn != nil {
			advice, aerr := callFn(ctx, nodeID+".run_retry_advisor", map[string]any{
				"issue":                issueWithContext,
				"error_message":        lastError,
				"error_context":        lastContext,
				"attempt_number":       attempt,
				"repo_path":            dagState.RepoPath,
				"prd_summary":          dagState.PRDSummary,
				"architecture_summary": dagState.ArchitectureSummary,
				"prd_path":             dagState.PRDPath,
				"architecture_path":    dagState.ArchitecturePath,
				"artifacts_dir":        dagState.ArtifactsDir,
				"model":                cfg.RetryAdvisorModel(),
				"ai_provider":          cfg.AIProvider(),
				"workspace_manifest":   dagState.WorkspaceManifest,
			})
			if aerr != nil {
				var f *fatal.FatalHarnessError
				if errors.As(aerr, &f) || errors.Is(aerr, context.Canceled) {
					return schemas.IssueResult{}, aerr // raise fatal / cancellation
				}
				continue // except Exception: continue
			}
			if !asBool(advice["should_retry"]) {
				break
			}
			issueWithContext = copyIssue(issue)
			issueWithContext["retry_context"] = mapGetStr(advice, "modified_context", "")
			issueWithContext["previous_error"] = lastError
			issueWithContext["retry_diagnosis"] = mapGetStr(advice, "diagnosis", "")
			continue
		} else if attempt <= cfg.MaxRetriesPerIssue {
			continue
		}
	}

	return schemas.IssueResult{
		IssueName:    issueName,
		Outcome:      schemas.IssueOutcomeFailedUnrecoverable,
		ErrorMessage: lastError,
		ErrorContext: lastContext,
		Attempts:     cfg.MaxRetriesPerIssue + 1,
	}, nil
}

// executeLevel executes all issues in a level with bounded concurrency
// (config.max_concurrent_issues; 0 = unlimited), returning a LevelResult with
// issues classified into completed/failed/skipped. Ports _execute_level.
//
// Each issue closure translates its own error into a FAILED_UNRECOVERABLE
// IssueResult and returns nil to the group, so one issue's failure never
// cancels its siblings mid-level (mirroring asyncio.gather(return_exceptions=True)).
func executeLevel(
	ctx context.Context,
	activeIssues []map[string]any,
	executeFn ExecuteFn,
	dagState *schemas.DAGState,
	cfg *config.ExecutionConfig,
	levelIndex int,
	callFn coding.CallFn,
	nodeID string,
	note noteFunc,
	memoryFn coding.MemoryFn,
) schemas.LevelResult {
	maxConcurrent := cfg.MaxConcurrentIssues

	if maxConcurrent > 0 && len(activeIssues) > maxConcurrent && note != nil {
		note(fmt.Sprintf("Concurrency limiter: %d issues, max %d parallel", len(activeIssues), maxConcurrent),
			[]string{"execution", "concurrency_limit"})
	}

	type outcome struct {
		res schemas.IssueResult
		err error
	}
	outcomes := make([]outcome, len(activeIssues))

	g, gctx := errgroup.WithContext(ctx)
	if maxConcurrent > 0 {
		g.SetLimit(maxConcurrent)
	}
	for i := range activeIssues {
		i := i
		issue := activeIssues[i]
		g.Go(func() error {
			res, err := executeSingleIssue(gctx, issue, dagState, executeFn, cfg, callFn, nodeID, note, memoryFn)
			outcomes[i] = outcome{res, err}
			return nil
		})
	}
	_ = g.Wait()

	levelResult := schemas.LevelResult{LevelIndex: levelIndex}
	for i, oc := range outcomes {
		if oc.err != nil {
			// gather(return_exceptions=True) wraps exceptions into failures.
			levelResult.Failed = append(levelResult.Failed, schemas.IssueResult{
				IssueName:    asStr(activeIssues[i]["name"]),
				Outcome:      schemas.IssueOutcomeFailedUnrecoverable,
				ErrorMessage: oc.err.Error(),
				ErrorContext: oc.err.Error(),
				Attempts:     1, // pydantic default
			})
			continue
		}
		res := oc.res
		// Backfill repo_name from issue's target_repo if the coder didn't set it.
		if res.RepoName == "" {
			res.RepoName = mapGetStr(activeIssues[i], "target_repo", "")
		}
		switch res.Outcome {
		case schemas.IssueOutcomeCompleted, schemas.IssueOutcomeCompletedWithDebt:
			levelResult.Completed = append(levelResult.Completed, res)
		case schemas.IssueOutcomeSkipped:
			levelResult.Skipped = append(levelResult.Skipped, res)
		default:
			levelResult.Failed = append(levelResult.Failed, res)
		}
	}

	return levelResult
}

// skipDownstream marks all issues downstream of failures as skipped. Ports
// _skip_downstream.
func skipDownstream(dagState *schemas.DAGState, failed []schemas.IssueResult) {
	for _, failure := range failed {
		downstream := dagutil.FindDownstream(failure.IssueName, dagState.AllIssues)
		for name := range downstream {
			if !contains(dagState.SkippedIssues, name) {
				dagState.SkippedIssues = append(dagState.SkippedIssues, name)
			}
		}
	}
}

// enrichDownstreamWithFailureNotes adds failure_notes to downstream issues so
// coder agents know what's missing when the replanner decides CONTINUE. Ports
// _enrich_downstream_with_failure_notes.
func enrichDownstreamWithFailureNotes(dagState *schemas.DAGState, failed []schemas.IssueResult) {
	for _, failure := range failed {
		downstream := dagutil.FindDownstream(failure.IssueName, dagState.AllIssues)
		for i, issue := range dagState.AllIssues {
			if downstream[asStr(issue["name"])] {
				notes := append([]any{}, asAnySlice(issue["failure_notes"])...)
				notes = append(notes, fmt.Sprintf(
					"WARNING: Upstream issue '%s' failed. Error: %s. It was supposed to provide: %s. "+
						"You may need to implement workarounds or stubs for missing functionality.",
					failure.IssueName, failure.ErrorMessage, pyStrList(asStringSlice(issue["depends_on"]))))
				updated := copyIssue(issue)
				updated["failure_notes"] = notes
				dagState.AllIssues[i] = updated
			}
		}
	}
}

// invokeReplannerViaCall invokes the replanner via call_fn (app.call). Ports
// _invoke_replanner_via_call. A call_fn error propagates out of RunDAG.
func invokeReplannerViaCall(
	ctx context.Context,
	dagState *schemas.DAGState,
	unrecoverable []schemas.IssueResult,
	cfg *config.ExecutionConfig,
	callFn coding.CallFn,
	nodeID string,
	note noteFunc,
) (schemas.ReplanDecision, error) {
	if note != nil {
		var failedNames []string
		for _, f := range unrecoverable {
			failedNames = append(failedNames, f.IssueName)
		}
		note(fmt.Sprintf("Replanning triggered (attempt %d/%d): failed issues = %s",
			dagState.ReplanCount+1, cfg.MaxReplans, pyStrList(failedNames)),
			[]string{"execution", "replan", "start"})
	}

	// Pass escalation context from Issue Advisor if available.
	escalationNotes := []map[string]any{}
	for _, f := range unrecoverable {
		if f.EscalationContext != "" {
			escalationNotes = append(escalationNotes, map[string]any{
				"issue_name":         f.IssueName,
				"escalation_context": f.EscalationContext,
				"adaptations":        dumpToMaps(f.Adaptations),
			})
		}
	}

	decisionDict, err := callFn(ctx, nodeID+".run_replanner", map[string]any{
		"dag_state":        dumpToMap(dagState),
		"failed_issues":    dumpToMaps(unrecoverable),
		"replan_model":     cfg.ReplanModel(),
		"ai_provider":      cfg.AIProvider(),
		"escalation_notes": escalationNotes,
	})
	if err != nil {
		return schemas.ReplanDecision{}, err
	}
	return mapToStruct[schemas.ReplanDecision](decisionDict)
}

// writeIssueFilesForReplan writes issue-*.md files for new/updated issues from
// the replanner, running one issue_writer per issue in parallel. Ports
// _write_issue_files_for_replan. Never returns an error (gather swallows them).
func writeIssueFilesForReplan(
	ctx context.Context,
	decision schemas.ReplanDecision,
	dagState *schemas.DAGState,
	cfg *config.ExecutionConfig,
	callFn coding.CallFn,
	nodeID string,
	note noteFunc,
) {
	issuesToWrite := append([]map[string]any{}, decision.NewIssues...)
	for _, updated := range decision.UpdatedIssues {
		if mapGetStr(updated, "description", "") != "" {
			issuesToWrite = append(issuesToWrite, updated)
		}
	}
	if len(issuesToWrite) == 0 {
		return
	}

	// Assign sequence numbers to new issues (next-available after existing max).
	maxSeq := 0
	for _, i := range dagState.AllIssues {
		if s := asInt(i["sequence_number"]); s > maxSeq {
			maxSeq = s
		}
	}
	for _, issue := range issuesToWrite {
		if asInt(issue["sequence_number"]) == 0 {
			maxSeq++
			issue["sequence_number"] = maxSeq
		}
	}

	if note != nil {
		note(fmt.Sprintf("Writing issue files for %d issues: %s", len(issuesToWrite), pyStrList(issueNames(issuesToWrite))),
			[]string{"execution", "issue_writer", "start"})
	}

	results := make([]map[string]any, len(issuesToWrite))
	g, gctx := errgroup.WithContext(ctx)
	for i := range issuesToWrite {
		i := i
		newIssue := issuesToWrite[i]
		g.Go(func() error {
			res, _ := callFn(gctx, nodeID+".run_issue_writer", map[string]any{
				"issue":                newIssue,
				"prd_summary":          dagState.PRDSummary,
				"architecture_summary": dagState.ArchitectureSummary,
				"issues_dir":           dagState.IssuesDir,
				"repo_path":            dagState.RepoPath,
				"model":                cfg.IssueWriterModel(),
				"ai_provider":          cfg.AIProvider(),
			})
			results[i] = res // an error leaves results[i] nil (gather swallows)
			return nil
		})
	}
	_ = g.Wait()

	if note != nil {
		successes := 0
		for _, r := range results {
			if r != nil && asBool(r["success"]) {
				successes++
			}
		}
		note(fmt.Sprintf("Issue writer complete: %d/%d succeeded", successes, len(issuesToWrite)),
			[]string{"execution", "issue_writer", "complete"})
	}
}

// asAnySlice coerces a value to []any (for the failure_notes list-copy pattern).
func asAnySlice(v any) []any {
	switch t := v.(type) {
	case []any:
		return t
	case []string:
		out := make([]any, len(t))
		for i, s := range t {
			out[i] = s
		}
		return out
	default:
		return nil
	}
}
