// Package coding is the INNER loop of the three-nested-loop architecture: it
// runs a single issue through coder -> reviewer (or coder -> QA + reviewer ->
// synthesizer) up to max_coding_iterations, returning an IssueResult with the
// final outcome and full iteration history.
//
// It is a 1:1 behavioural port of swe_af/execution/coding_loop.py. Two paths:
//   - DEFAULT (most issues): coder -> reviewer (2 role calls). Reviewer is the
//     sole gatekeeper.
//   - FLAGGED (complex/risky): coder -> QA + reviewer (concurrent) -> synthesizer
//     (4 role calls). Selected when the sprint planner sets
//     guidance.needs_deeper_qa = true.
//
// All AI-agent invocations go through the injected CallFn seam (a closure over
// agent.Call + envelope.UnwrapCallResult supplied by the DAG executor), so the
// loop stays testable with a scripted call function and the exact keyword-arg
// key names Python passes to each role are preserved verbatim.
package coding

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/fatal"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
	"golang.org/x/sync/errgroup"
)

// CallFn dispatches to an AI agent (coder, reviewer, QA, synthesizer) by target
// (e.g. "swe-planner.run_coder") with the same keyword args Python passes. The
// DAG executor supplies a closure over agent.Call + envelope.UnwrapCallResult;
// tests supply a scripted function. A returned *fatal.FatalHarnessError is
// propagated (never swallowed into a fallback).
type CallFn func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error)

// MemoryFn is the shared-memory seam (in-process cross-issue learning). action
// is "get" or "set"; value is nil for "get". A nil MemoryFn disables learning
// (mirrors the Python `memory_fn is None` guard).
type MemoryFn func(action, key string, value any) any

// NoteFn is the fire-and-forget observability seam (app.Note-equivalent). A nil
// NoteFn is a no-op.
type NoteFn func(msg string, tags []string)

// RunCodingLoop runs the coding loop for a single issue and returns its
// IssueResult. It is the Go port of coding_loop.run_coding_loop.
//
// The returned error is non-nil ONLY for propagated failures that Python would
// re-raise rather than turn into a failed IssueResult: a fatal, non-retryable
// harness error (*fatal.FatalHarnessError) or context cancellation. Every other
// terminal condition (approve, block, stuck, exhaustion, coder failure) is
// encoded in the IssueResult with err == nil.
func RunCodingLoop(
	ctx context.Context,
	issue map[string]any,
	dagState *schemas.DAGState,
	callFn CallFn,
	nodeID string,
	cfg *config.ExecutionConfig,
	noteFn NoteFn,
	memoryFn MemoryFn,
) (schemas.IssueResult, error) {
	// note is a nil-safe wrapper so call sites need no guard (mirrors the
	// pervasive `if note_fn:` checks in Python).
	note := func(msg string, tags []string) {
		if noteFn != nil {
			noteFn(msg, tags)
		}
	}

	issueName := mapGetStr(issue, "name", "unknown")

	worktreePath := dagState.RepoPath // issue.get("worktree_path", dag_state.repo_path)
	if v, ok := issue["worktree_path"]; ok {
		worktreePath = anyToStr(v)
	}
	branchName := mapGetStr(issue, "branch_name", "")
	maxIterations := cfg.MaxCodingIterations
	timeout := cfg.AgentTimeoutSeconds
	permissionMode := cfg.PermissionMode

	// Multi-repo context (nil for single-repo builds).
	targetRepo := mapGetStr(issue, "target_repo", "")
	wsManifestDict := dagState.WorkspaceManifest // map[string]any | nil

	// Warn if a multi-repo issue is missing worktree_path (falling back to
	// primary repo).
	if isTruthy(wsManifestDict) && !isTruthy(issue["worktree_path"]) {
		note(
			fmt.Sprintf(
				"WARNING: issue '%s' has no worktree_path in multi-repo mode. "+
					"Falling back to primary repo: %s. target_repo='%s'",
				issueName, dagState.RepoPath, targetRepo,
			),
			[]string{"coding_loop", "warning", "multi_repo_fallback"},
		)
	}

	// Extract guidance — determines execution path.
	guidance, _ := issue["guidance"].(map[string]any) // issue.get("guidance") or {}
	needsDeeperQA := mapGetBool(guidance, "needs_deeper_qa", false)

	// Slim project context — paths only, agents read files if needed.
	projectContext := map[string]any{
		"prd_path":          dagState.PRDPath,
		"architecture_path": dagState.ArchitecturePath,
		"artifacts_dir":     dagState.ArtifactsDir,
		"issues_dir":        dagState.IssuesDir,
		"repo_path":         dagState.RepoPath,
	}

	pathLabel := "DEFAULT (reviewer only)"
	if needsDeeperQA {
		pathLabel = "FLAGGED (QA+reviewer+synth)"
	}
	note(
		fmt.Sprintf("Coding loop starting: %s [%s] (max %d iterations)", issueName, pathLabel, maxIterations),
		[]string{"coding_loop", "start", issueName},
	)

	feedback := ""
	iterationHistory := []map[string]any{}
	filesChanged := []string{}
	startIteration := 1
	isFirstSuccess := len(dagState.CompletedIssues) == 0

	// Resume from iteration checkpoint if available.
	if existingState := loadIterationState(dagState.ArtifactsDir, issueName, dagState.BuildID); existingState != nil {
		startIteration = toInt(existingState["iteration"]) + 1
		feedback = mapGetStr(existingState, "feedback", "")
		if fc := toStringSlice(existingState["files_changed"]); fc != nil {
			filesChanged = fc
		}
		if ih := toMapSlice(existingState["iteration_history"]); ih != nil {
			iterationHistory = ih
		}
		note(
			fmt.Sprintf("Resuming %s from iteration %d", issueName, startIteration),
			[]string{"coding_loop", "resume", issueName},
		)
	}

	// reviewResult persists past the loop for the exhaustion check (Python:
	// `review_result if 'review_result' in dir() else None`).
	var reviewResult map[string]any

	for iteration := startIteration; iteration <= maxIterations; iteration++ {
		// Honor cancellation between iterations.
		select {
		case <-ctx.Done():
			return schemas.IssueResult{}, ctx.Err()
		default:
		}

		iterationID := newIterationID()

		note(
			fmt.Sprintf("Coding loop iteration %d/%d: %s", iteration, maxIterations, issueName),
			[]string{"coding_loop", "iteration", issueName},
		)

		// --- Read shared memory context ---
		memoryContext := readMemoryContext(memoryFn, issue)

		// --- 1. CODER ---
		coderResult, cerr := callWithTimeout(ctx, timeout, fmt.Sprintf("coder:%s:iter%d", issueName, iteration),
			func(c context.Context) (map[string]any, error) {
				return callFn(c, nodeID+".run_coder", map[string]any{
					"issue":              issue,
					"worktree_path":      worktreePath,
					"feedback":           feedback,
					"iteration":          iteration,
					"iteration_id":       iterationID,
					"project_context":    projectContext,
					"memory_context":     memoryContext,
					"model":              cfg.CoderModel(),
					"permission_mode":    permissionMode,
					"ai_provider":        cfg.AIProvider(),
					"workspace_manifest": wsManifestDict,
					"target_repo":        targetRepo,
				})
			})
		if cerr != nil {
			var fhe *fatal.FatalHarnessError
			if errors.As(cerr, &fhe) || errors.Is(cerr, context.Canceled) {
				return schemas.IssueResult{}, cerr // propagate (FatalHarnessError / cancellation)
			}
			note(
				fmt.Sprintf("Coder agent failed: %s iter %d: %v", issueName, iteration, cerr),
				[]string{"coding_loop", "coder_error", issueName},
			)
			return schemas.IssueResult{
				IssueName:        issueName,
				Outcome:          schemas.IssueOutcomeFailedUnrecoverable,
				ErrorMessage:     fmt.Sprintf("Coder agent failed on iteration %d: %v", iteration, cerr),
				ErrorContext:     fmt.Sprintf("%v", cerr),
				FilesChanged:     filesChanged,
				BranchName:       branchName,
				Attempts:         iteration,
				IterationHistory: iterationHistory,
			}, nil
		}

		// Track files changed across iterations.
		for _, f := range toStringSlice(coderResult["files_changed"]) {
			if !contains(filesChanged, f) {
				filesChanged = append(filesChanged, f)
			}
		}

		saveArtifact(dagState.ArtifactsDir, iterationID, "coder", coderResult)

		// --- 2. PATH BRANCH ---
		var action, summary string
		var qaResult, synthesisResult map[string]any
		stuck := false

		if needsDeeperQA {
			// FLAGGED PATH: QA + reviewer parallel -> synthesizer.
			a, s, rr, qr, sr, perr := runFlaggedPath(ctx, callFn, nodeID, worktreePath, coderResult,
				issue, iteration, iterationID, iterationHistory, projectContext, memoryContext,
				cfg, timeout, issueName, note, wsManifestDict, targetRepo)
			if perr != nil {
				return schemas.IssueResult{}, perr // fatal — propagate
			}
			action, summary, reviewResult, qaResult, synthesisResult = a, s, rr, qr, sr
			saveArtifact(dagState.ArtifactsDir, iterationID, "qa", qaResult)
			saveArtifact(dagState.ArtifactsDir, iterationID, "review", reviewResult)
			saveArtifact(dagState.ArtifactsDir, iterationID, "synthesis", synthesisResult)

			stuck = mapGetBool(synthesisResult, "stuck", false)
		} else {
			// DEFAULT PATH: reviewer only.
			a, s, rr, perr := runDefaultPath(ctx, callFn, nodeID, worktreePath, coderResult,
				issue, iterationID, projectContext, memoryContext, cfg, timeout, issueName, note,
				wsManifestDict, targetRepo)
			if perr != nil {
				return schemas.IssueResult{}, perr // fatal — propagate
			}
			action, summary, reviewResult = a, s, rr
			qaResult = nil
			synthesisResult = nil
			saveArtifact(dagState.ArtifactsDir, iterationID, "review", reviewResult)

			stuck = false
		}

		// Record iteration for history.
		var qaPassed any
		if isTruthy(qaResult) {
			qaPassed = qaResult["passed"] // .get("passed", None)
		}
		iterationHistory = append(iterationHistory, map[string]any{
			"iteration":       iteration,
			"action":          action,
			"summary":         summary,
			"qa_passed":       qaPassed,
			"review_approved": isTruthy(reviewResult) && mapGetBool(reviewResult, "approved", false),
			"review_blocking": isTruthy(reviewResult) && mapGetBool(reviewResult, "blocking", false),
			"path":            pathName(needsDeeperQA),
		})

		note(
			fmt.Sprintf("Decision: %s — %s", action, truncate(summary, 100)),
			[]string{"coding_loop", "decision", issueName},
		)

		// Save iteration-level checkpoint.
		saveIterationState(dagState.ArtifactsDir, issueName, map[string]any{
			"iteration":         iteration,
			"feedback":          summary,
			"files_changed":     filesChanged,
			"iteration_history": iterationHistory,
		}, dagState.BuildID)

		// --- 3. WRITE TO MEMORY ---
		if action == "approve" {
			writeMemoryOnApprove(memoryFn, issue, coderResult, isFirstSuccess, note)
		} else if action == "fix" {
			writeMemoryOnFailure(memoryFn, issue, summary, reviewResult, note)
		}

		// --- 4. BRANCH ON ACTION ---
		if action == "approve" {
			note(
				fmt.Sprintf("Coding loop APPROVED: %s after %d iteration(s)", issueName, iteration),
				[]string{"coding_loop", "complete", issueName},
			)
			return schemas.IssueResult{
				IssueName:        issueName,
				Outcome:          schemas.IssueOutcomeCompleted,
				ResultSummary:    summary,
				FilesChanged:     filesChanged,
				BranchName:       branchName,
				Attempts:         iteration,
				IterationHistory: iterationHistory,
				RepoName:         mapGetStr(coderResult, "repo_name", ""),
			}, nil
		}

		if action == "block" {
			note(
				fmt.Sprintf("Coding loop BLOCKED: %s — %s", issueName, summary),
				[]string{"coding_loop", "blocked", issueName},
			)
			writeMemoryOnFailure(memoryFn, issue, summary, reviewResult, note)
			return schemas.IssueResult{
				IssueName:        issueName,
				Outcome:          schemas.IssueOutcomeFailedUnrecoverable,
				ErrorMessage:     summary,
				FilesChanged:     filesChanged,
				BranchName:       branchName,
				Attempts:         iteration,
				IterationHistory: iterationHistory,
			}, nil
		}

		// action == "fix" — build rich feedback for the coder.
		if action == "fix" {
			feedbackParts := []string{summary}
			if isTruthy(qaResult) {
				testFailures := toMapSlice(qaResult["test_failures"])
				if len(testFailures) > 0 {
					feedbackParts = append(feedbackParts, "\n### Specific Test Failures")
					for _, f := range testFailures {
						feedbackParts = append(feedbackParts, fmt.Sprintf(
							"- `%s` in `%s`: %s",
							mapGetStr(f, "test_name", "?"), mapGetStr(f, "file", "?"), mapGetStr(f, "error", ""),
						))
					}
				}
			}
			if isTruthy(reviewResult) {
				var blockingDebt []map[string]any
				for _, d := range toMapSlice(reviewResult["debt_items"]) {
					if mapGetStr(d, "severity", "") == "blocking" {
						blockingDebt = append(blockingDebt, d)
					}
				}
				if len(blockingDebt) > 0 {
					feedbackParts = append(feedbackParts, "\n### Blocking Review Issues")
					for _, d := range blockingDebt {
						feedbackParts = append(feedbackParts, fmt.Sprintf(
							"- [%s] %s: %s",
							mapGetStr(d, "severity", ""), mapGetStr(d, "title", "?"), mapGetStr(d, "description", ""),
						))
					}
				}
			}
			feedback = strings.Join(feedbackParts, "\n")
		} else {
			feedback = summary
		}

		// Stuck detection — default path uses history-based detection since it
		// has no synthesizer to set the stuck flag.
		if !stuck && !needsDeeperQA {
			stuck = DetectStuckLoop(iterationHistory, 3)
		}

		if stuck {
			lastBlocking := isTruthy(reviewResult) && mapGetBool(reviewResult, "blocking", false)
			if !lastBlocking && len(filesChanged) > 0 {
				// Non-blocking stuck loop with code changes -> accept with debt.
				note(
					fmt.Sprintf(
						"Coding loop STUCK (non-blocking): %s — accepting with debt after %d iterations",
						issueName, iteration,
					),
					[]string{"coding_loop", "stuck", "accept_debt", issueName},
				)
				return schemas.IssueResult{
					IssueName:        issueName,
					Outcome:          schemas.IssueOutcomeCompletedWithDebt,
					ResultSummary:    fmt.Sprintf("Accepted with debt (stuck loop, non-blocking): %s", summary),
					FilesChanged:     filesChanged,
					BranchName:       branchName,
					Attempts:         iteration,
					IterationHistory: iterationHistory,
				}, nil
			}
			note(
				fmt.Sprintf("Coding loop STUCK: %s — breaking after %d iterations", issueName, iteration),
				[]string{"coding_loop", "stuck", issueName},
			)
			writeMemoryOnFailure(memoryFn, issue, summary, reviewResult, note)
			return schemas.IssueResult{
				IssueName:        issueName,
				Outcome:          schemas.IssueOutcomeFailedUnrecoverable,
				ErrorMessage:     fmt.Sprintf("Stuck loop detected: %s", summary),
				FilesChanged:     filesChanged,
				BranchName:       branchName,
				Attempts:         iteration,
				IterationHistory: iterationHistory,
			}, nil
		}
	}

	// Loop exhausted without approval — check if we can accept with debt.
	lastReview := reviewResult // nil if the loop body never ran
	lastBlocking := isTruthy(lastReview) && mapGetBool(lastReview, "blocking", false)

	if !lastBlocking && len(filesChanged) > 0 {
		// Reviewer was never blocking and coder produced changes — accept with
		// debt rather than failing entirely.
		note(
			fmt.Sprintf(
				"Coding loop exhausted (non-blocking): %s — accepting with debt after %d iterations",
				issueName, maxIterations,
			),
			[]string{"coding_loop", "exhausted", "accept_debt", issueName},
		)
		return schemas.IssueResult{
			IssueName: issueName,
			Outcome:   schemas.IssueOutcomeCompletedWithDebt,
			ResultSummary: fmt.Sprintf(
				"Accepted with debt after %d iterations (reviewer non-blocking, code changes present)",
				maxIterations,
			),
			FilesChanged:     filesChanged,
			BranchName:       branchName,
			Attempts:         maxIterations,
			IterationHistory: iterationHistory,
		}, nil
	}

	// Truly unrecoverable — reviewer was blocking or no code was produced.
	note(
		fmt.Sprintf("Coding loop exhausted: %s after %d iterations", issueName, maxIterations),
		[]string{"coding_loop", "exhausted", issueName},
	)

	writeMemoryOnFailure(memoryFn, issue, "Loop exhausted", lastReview, note)

	return schemas.IssueResult{
		IssueName:        issueName,
		Outcome:          schemas.IssueOutcomeFailedUnrecoverable,
		ErrorMessage:     fmt.Sprintf("Coding loop exhausted after %d iterations without approval", maxIterations),
		FilesChanged:     filesChanged,
		BranchName:       branchName,
		Attempts:         maxIterations,
		IterationHistory: iterationHistory,
	}, nil
}

// runDefaultPath ports _run_default_path: reviewer only (2 role calls including
// the coder). Returns (action, summary, review_result). A fatal harness error is
// returned as err (propagated); any other reviewer failure degrades to an
// approving fallback so the loop continues.
func runDefaultPath(
	ctx context.Context,
	callFn CallFn,
	nodeID, worktreePath string,
	coderResult, issue map[string]any,
	iterationID string,
	projectContext, memoryContext map[string]any,
	cfg *config.ExecutionConfig,
	timeout int,
	issueName string,
	note NoteFn,
	workspaceManifest map[string]any,
	targetRepo string,
) (action, summary string, reviewResult map[string]any, err error) {
	permissionMode := cfg.PermissionMode

	reviewResult, rerr := callWithTimeout(ctx, timeout, fmt.Sprintf("review:%s:default", issueName),
		func(c context.Context) (map[string]any, error) {
			return callFn(c, nodeID+".run_code_reviewer", map[string]any{
				"worktree_path":      worktreePath,
				"coder_result":       coderResult,
				"issue":              issue,
				"iteration_id":       iterationID,
				"project_context":    projectContext,
				"qa_ran":             false,
				"memory_context":     memoryContext,
				"model":              cfg.CodeReviewerModel(),
				"permission_mode":    permissionMode,
				"ai_provider":        cfg.AIProvider(),
				"workspace_manifest": workspaceManifest,
				"target_repo":        targetRepo,
			})
		})
	if rerr != nil {
		var fhe *fatal.FatalHarnessError
		if errors.As(rerr, &fhe) || errors.Is(rerr, context.Canceled) {
			return "", "", nil, rerr // raise
		}
		note(
			fmt.Sprintf("Reviewer failed: %s: %v", issueName, rerr),
			[]string{"coding_loop", "review_error", issueName},
		)
		reviewResult = map[string]any{"approved": true, "blocking": false, "summary": fmt.Sprintf("Review unavailable: %v", rerr)}
	}

	note(
		fmt.Sprintf("Reviewer: approved=%v, blocking=%v", mapGetOr(reviewResult, "approved", nil), mapGetOr(reviewResult, "blocking", nil)),
		[]string{"coding_loop", "feedback", issueName},
	)

	// Reviewer is sole gatekeeper on default path.
	approved := mapGetBool(reviewResult, "approved", false)
	blocking := mapGetBool(reviewResult, "blocking", false)
	summary = mapGetStr(reviewResult, "summary", "")

	if approved && !blocking {
		action = "approve"
	} else if blocking {
		action = "block"
	} else {
		action = "fix"
	}

	return action, summary, reviewResult, nil
}

// runFlaggedPath ports _run_flagged_path: QA + reviewer concurrently -> synthesizer
// (4 role calls). Returns (action, summary, review, qa, synthesis). Mirroring
// Python's asyncio.gather(return_exceptions=True), a QA or reviewer failure (even
// a fatal one) degrades to a fallback here; only a synthesizer FatalHarnessError
// is propagated via err.
func runFlaggedPath(
	ctx context.Context,
	callFn CallFn,
	nodeID, worktreePath string,
	coderResult, issue map[string]any,
	iteration int,
	iterationID string,
	iterationHistory []map[string]any,
	projectContext, memoryContext map[string]any,
	cfg *config.ExecutionConfig,
	timeout int,
	issueName string,
	note NoteFn,
	workspaceManifest map[string]any,
	targetRepo string,
) (action, summary string, reviewResult, qaResult, synthesisResult map[string]any, err error) {
	permissionMode := cfg.PermissionMode

	// QA + reviewer in parallel. Each goroutine stores its own (result, error)
	// and returns nil to the group, so g.Wait() is a pure barrier and one
	// failure never cancels the other — matching gather(return_exceptions=True).
	var qaErr, reviewErr error
	g, gctx := errgroup.WithContext(ctx)
	g.Go(func() error {
		qaResult, qaErr = callWithTimeout(gctx, timeout, fmt.Sprintf("qa:%s:iter%d", issueName, iteration),
			func(c context.Context) (map[string]any, error) {
				return callFn(c, nodeID+".run_qa", map[string]any{
					"worktree_path":      worktreePath,
					"coder_result":       coderResult,
					"issue":              issue,
					"iteration_id":       iterationID,
					"project_context":    projectContext,
					"model":              cfg.QAModel(),
					"permission_mode":    permissionMode,
					"ai_provider":        cfg.AIProvider(),
					"workspace_manifest": workspaceManifest,
					"target_repo":        targetRepo,
				})
			})
		return nil
	})
	g.Go(func() error {
		reviewResult, reviewErr = callWithTimeout(gctx, timeout, fmt.Sprintf("review:%s:iter%d", issueName, iteration),
			func(c context.Context) (map[string]any, error) {
				return callFn(c, nodeID+".run_code_reviewer", map[string]any{
					"worktree_path":      worktreePath,
					"coder_result":       coderResult,
					"issue":              issue,
					"iteration_id":       iterationID,
					"project_context":    projectContext,
					"qa_ran":             true,
					"memory_context":     memoryContext,
					"model":              cfg.CodeReviewerModel(),
					"permission_mode":    permissionMode,
					"ai_provider":        cfg.AIProvider(),
					"workspace_manifest": workspaceManifest,
					"target_repo":        targetRepo,
				})
			})
		return nil
	})
	_ = g.Wait()

	if qaErr != nil {
		note(
			fmt.Sprintf("QA agent failed: %s: %v", issueName, qaErr),
			[]string{"coding_loop", "qa_error", issueName},
		)
		qaResult = map[string]any{"passed": false, "summary": fmt.Sprintf("QA agent failed: %v", qaErr)}
	}
	if reviewErr != nil {
		note(
			fmt.Sprintf("Review agent failed: %s: %v", issueName, reviewErr),
			[]string{"coding_loop", "review_error", issueName},
		)
		reviewResult = map[string]any{"approved": true, "blocking": false, "summary": fmt.Sprintf("Review unavailable: %v", reviewErr)}
	}

	note(
		fmt.Sprintf(
			"QA: passed=%v, Review: approved=%v, blocking=%v",
			mapGetOr(qaResult, "passed", nil), mapGetOr(reviewResult, "approved", nil), mapGetOr(reviewResult, "blocking", nil),
		),
		[]string{"coding_loop", "feedback", issueName},
	)

	// Synthesizer.
	synthesisResult, serr := callWithTimeout(ctx, timeout, fmt.Sprintf("synthesizer:%s:iter%d", issueName, iteration),
		func(c context.Context) (map[string]any, error) {
			return callFn(c, nodeID+".run_qa_synthesizer", map[string]any{
				"qa_result":         qaResult,
				"review_result":     reviewResult,
				"iteration_history": iterationHistory,
				"iteration_id":      iterationID,
				"worktree_path":     worktreePath,
				"issue_summary": map[string]any{
					"name":                mapGetOr(issue, "name", ""),
					"title":               mapGetOr(issue, "title", ""),
					"acceptance_criteria": mapGetOr(issue, "acceptance_criteria", []any{}),
				},
				"artifacts_dir":      mapGetOr(projectContext, "artifacts_dir", ""),
				"model":              cfg.QASynthesizerModel(),
				"permission_mode":    permissionMode,
				"ai_provider":        cfg.AIProvider(),
				"workspace_manifest": workspaceManifest,
				"target_repo":        targetRepo,
			})
		})
	if serr != nil {
		var fhe *fatal.FatalHarnessError
		if errors.As(serr, &fhe) || errors.Is(serr, context.Canceled) {
			return "", "", reviewResult, qaResult, nil, serr // propagate
		}
		note(
			fmt.Sprintf("Synthesizer failed: %s: %v — using fallback", issueName, serr),
			[]string{"coding_loop", "synthesizer_error", issueName},
		)
		qaPassed := mapGetBool(qaResult, "passed", false)
		reviewApproved := mapGetBool(reviewResult, "approved", false)
		reviewBlocking := mapGetBool(reviewResult, "blocking", false)
		switch {
		case qaPassed && reviewApproved && !reviewBlocking:
			synthesisResult = map[string]any{"action": "approve", "summary": "Auto-approved (synthesizer unavailable)"}
		case reviewBlocking:
			synthesisResult = map[string]any{"action": "block", "summary": fmt.Sprintf("Blocked by review (synthesizer unavailable): %s", mapGetStr(reviewResult, "summary", ""))}
		default:
			synthesisResult = map[string]any{"action": "fix", "summary": fmt.Sprintf("Auto-fix (synthesizer unavailable): QA=%s, Review=%s", mapGetStr(qaResult, "summary", ""), mapGetStr(reviewResult, "summary", ""))}
		}
	}

	action = mapGetStr(synthesisResult, "action", "fix")
	summary = mapGetStr(synthesisResult, "summary", "")

	return action, summary, reviewResult, qaResult, synthesisResult, nil
}

// DetectStuckLoop ports _detect_stuck_loop: true if the last `window` iterations
// are all non-blocking "fix" cycles (the default-path non-convergence signal).
func DetectStuckLoop(iterationHistory []map[string]any, window int) bool {
	if len(iterationHistory) < window {
		return false
	}
	recent := iterationHistory[len(iterationHistory)-window:]
	for _, entry := range recent {
		action, _ := entry["action"].(string)
		if action != "fix" || mapGetBool(entry, "review_blocking", false) {
			return false
		}
	}
	return true
}

// callWithTimeout ports _call_with_timeout: run fn under an
// asyncio.wait_for-equivalent deadline. A deadline hit yields the exact
// "Agent call '<label>' timed out after <n>s" error (a non-fatal, generic error,
// matching Python's TimeoutError which the callers turn into a failure/fallback);
// parent-context cancellation propagates as ctx.Err() (matching CancelledError).
func callWithTimeout(ctx context.Context, timeout int, label string, fn func(ctx context.Context) (map[string]any, error)) (map[string]any, error) {
	cctx, cancel := context.WithTimeout(ctx, time.Duration(timeout)*time.Second)
	defer cancel()

	type result struct {
		m   map[string]any
		err error
	}
	ch := make(chan result, 1)
	go func() {
		m, err := fn(cctx)
		ch <- result{m, err}
	}()

	select {
	case <-cctx.Done():
		if ctx.Err() != nil {
			// Parent cancelled — propagate cancellation, not a timeout.
			return nil, ctx.Err()
		}
		return nil, fmt.Errorf("Agent call '%s' timed out after %ds", label, timeout)
	case r := <-ch:
		return r.m, r.err
	}
}

// pathName returns the iteration-history "path" label.
func pathName(needsDeeperQA bool) string {
	if needsDeeperQA {
		return "flagged"
	}
	return "default"
}
