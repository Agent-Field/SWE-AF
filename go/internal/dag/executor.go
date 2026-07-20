package dag

import (
	"context"
	"fmt"
	"sync"

	"github.com/Agent-Field/SWE-AF/go/internal/coding"
	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/dagutil"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// CallFn dispatches to a reasoner by target (e.g. "swe-planner-go.run_merger") with
// the same keyword args Python passes. Callers supply a closure over agent.Call
// + envelope.UnwrapCallResult (so results arrive already unwrapped); a returned
// *fatal.FatalHarnessError is honoured throughout. Alias of coding.CallFn so the
// same closure threads through the coding loop.
type CallFn = coding.CallFn

// NoteFn is the fire-and-forget observability seam (app.Note-equivalent). Nil is
// a no-op. Alias of coding.NoteFn.
type NoteFn = coding.NoteFn

// MemoryFn is the shared cross-issue learning seam. Alias of coding.MemoryFn.
type MemoryFn = coding.MemoryFn

// ExecuteFn is the optional external coder path (Python's execute_fn). It
// receives an issue dict and the DAG state and returns the reasoner's result
// dict (as app.call would). When nil, RunDAG uses the built-in coding loop.
type ExecuteFn func(ctx context.Context, issue map[string]any, dagState *schemas.DAGState) (map[string]any, error)

// runOptions collects the optional run_dag parameters that Python passes as
// keyword arguments. Defaults match the Python signature.
type runOptions struct {
	executeFn         ExecuteFn
	noteFn            NoteFn
	gitConfig         map[string]any
	resume            bool
	buildID           string
	workspaceManifest map[string]any
}

// Option configures a RunDAG invocation.
type Option func(*runOptions)

// WithExecuteFn sets the external coder path (Python execute_fn). When set, the
// built-in coding loop is not used.
func WithExecuteFn(fn ExecuteFn) Option { return func(o *runOptions) { o.executeFn = fn } }

// WithNoteFn sets the observability callback (Python note_fn=app.note).
func WithNoteFn(fn NoteFn) Option { return func(o *runOptions) { o.noteFn = fn } }

// WithGitConfig sets the git configuration from run_git_init (Python git_config).
func WithGitConfig(g map[string]any) Option { return func(o *runOptions) { o.gitConfig = g } }

// WithResume enables checkpoint resume (Python resume=True).
func WithResume(r bool) Option { return func(o *runOptions) { o.resume = r } }

// WithBuildID sets the per-build id used for branch namespace isolation.
func WithBuildID(id string) Option { return func(o *runOptions) { o.buildID = id } }

// WithWorkspaceManifest sets the multi-repo workspace manifest dict (Python
// workspace_manifest). None/nil keeps the single-repo path.
func WithWorkspaceManifest(m map[string]any) Option {
	return func(o *runOptions) { o.workspaceManifest = m }
}

// sharedMemory is the in-process cross-issue learning store, guarded by a mutex
// because issues in a level run concurrently. Ports the _shared_memory dict +
// _memory_fn closure.
type sharedMemory struct {
	mu sync.Mutex
	m  map[string]any
}

func (s *sharedMemory) fn(action, key string, value any) any {
	s.mu.Lock()
	defer s.mu.Unlock()
	switch action {
	case "get":
		return s.m[key]
	case "set":
		s.m[key] = value
	}
	return nil
}

// RunDAG executes a planned DAG with self-healing replanning and returns the
// final DAGState. It is the Go port of dag_executor.run_dag.
//
// planResult is the PlanResult dict from the planning pipeline. repoPath is the
// target repo. callFn is the reasoner-dispatch seam (nil falls back to the
// direct replanner path and disables reasoner-driven gates, matching Python's
// call_fn=None). nodeID is the agent node id used to build call targets.
// cfg is the execution config (nil → defaults). Optional behaviour (external
// coder, notes, git config, resume, build id, workspace manifest) is set via
// Option values.
//
// The returned error is non-nil when a directly-awaited reasoner call
// (worktree setup, single-repo merge, integration test, cleanup-on-await, or
// replanner) fails — matching Python, where those un-gathered awaits propagate.
// Per-issue failures (including fatal harness errors raised inside an issue's
// coding loop) are captured into IssueResults at the level barrier and never
// abort the whole run.
func RunDAG(
	ctx context.Context,
	planResult map[string]any,
	repoPath string,
	callFn CallFn,
	nodeID string,
	cfg *config.ExecutionConfig,
	opts ...Option,
) (*schemas.DAGState, error) {
	var o runOptions
	for _, opt := range opts {
		opt(&o)
	}
	note := o.noteFn

	if cfg == nil {
		def, err := config.LoadExecutionConfig(nil)
		if err != nil {
			return nil, err
		}
		cfg = def
	}
	if nodeID == "" {
		nodeID = "swe-planner-go"
	}

	dagState := initDAGState(planResult, repoPath, o.gitConfig, o.buildID)
	dagState.WorkspaceManifest = o.workspaceManifest
	dagState.MaxReplans = cfg.MaxReplans

	// Resume from checkpoint if requested.
	if o.resume {
		artifactsDir := mapGetStr(planResult, "artifacts_dir", "")
		if artifactsDir != "" {
			if loaded := loadCheckpoint(artifactsDir); loaded != nil {
				dagState = loaded
				if note != nil {
					note(fmt.Sprintf("Resumed from checkpoint: level=%d, completed=%d, failed=%d",
						dagState.CurrentLevel, len(dagState.CompletedIssues), len(dagState.FailedIssues)),
						[]string{"execution", "resume"})
				}
			}
		}
	}

	if note != nil {
		verb := "starting"
		if o.resume {
			verb = "resuming"
		}
		note(fmt.Sprintf("DAG execution %s: %d issues, %d levels", verb, len(dagState.AllIssues), len(dagState.Levels)),
			[]string{"execution", "start"})
	}

	// Save initial checkpoint.
	saveCheckpoint(dagState, note)

	// Per-repo git init for multi-repo builds.
	if o.workspaceManifest != nil && callFn != nil {
		if err := initAllRepos(ctx, dagState, callFn, nodeID, cfg.GitModel(), cfg.AIProvider(), "", o.buildID, note); err != nil {
			return nil, err
		}
	}

	// Shared memory store for cross-issue learning within this run.
	var memoryFn coding.MemoryFn
	if callFn != nil && cfg.EnableLearning {
		sm := &sharedMemory{m: map[string]any{}}
		memoryFn = sm.fn
	}

	issueByName := indexByName(dagState.AllIssues)

	// Cleanup goroutine plumbing (Python's asyncio.create_task + await).
	var cleanupErr error
	var cleanupDone chan struct{}
	startCleanup := func(branches []string, level int, completed []schemas.IssueResult) {
		cleanupErr = nil
		cleanupDone = make(chan struct{})
		go func() {
			cleanupErr = cleanupWorktrees(ctx, dagState, branches, callFn, nodeID, note,
				level, cfg.GitModel(), cfg.AIProvider(), cfg.DeterministicGit, completed)
			close(cleanupDone)
		}()
	}
	awaitCleanup := func() error {
		if cleanupDone == nil {
			return nil
		}
		<-cleanupDone
		cleanupDone = nil
		return cleanupErr
	}

mainLoop:
	for dagState.CurrentLevel < len(dagState.Levels) {
		// Honor cancellation at the level boundary.
		select {
		case <-ctx.Done():
			return dagState, ctx.Err()
		default:
		}

		levelNames := dagState.Levels[dagState.CurrentLevel]

		// Filter to active issues (not skipped, not already completed/failed).
		doneNames := map[string]bool{}
		for _, r := range dagState.CompletedIssues {
			doneNames[r.IssueName] = true
		}
		for _, r := range dagState.FailedIssues {
			doneNames[r.IssueName] = true
		}
		for _, n := range dagState.SkippedIssues {
			doneNames[n] = true
		}

		var activeIssues []map[string]any
		for _, name := range levelNames {
			if issue, ok := issueByName[name]; ok && !doneNames[name] {
				activeIssues = append(activeIssues, issue)
			}
		}

		if len(activeIssues) == 0 {
			dagState.CurrentLevel++
			continue
		}

		if note != nil {
			note(fmt.Sprintf("Executing level %d: %s", dagState.CurrentLevel, pyStrList(issueNames(activeIssues))),
				[]string{"execution", "level", "start"})
		}

		// --- WORKTREE SETUP (git workflow) ---
		if callFn != nil && dagState.GitIntegrationBranch != "" {
			enriched, err := setupWorktrees(ctx, dagState, activeIssues, callFn, nodeID, cfg, note, dagState.BuildID)
			if err != nil {
				return nil, err
			}
			activeIssues = enriched
			// Persist worktree_path/branch_name back to dag_state.all_issues so
			// checkpoints contain the enriched data (resume-safe).
			enrichedByName := indexByName(activeIssues)
			for i, issue := range dagState.AllIssues {
				if e, ok := enrichedByName[asStr(issue["name"])]; ok {
					if _, has := e["worktree_path"]; has {
						dagState.AllIssues[i] = e
					}
				}
			}
			issueByName = indexByName(dagState.AllIssues)
			// Re-resolve active issues to the (now enriched) stored dicts so the
			// executor operates on the persisted objects, matching Python where
			// active_issues holds the enriched dicts.
			activeIssues = reselect(activeIssues, issueByName)
		}

		// Track in-flight issues and checkpoint before execution.
		dagState.InFlightIssues = issueNames(activeIssues)
		saveCheckpoint(dagState, note)

		// Execute all issues in this level concurrently.
		levelResult := executeLevel(ctx, activeIssues, o.executeFn, dagState, cfg,
			dagState.CurrentLevel, callFn, nodeID, note, memoryFn)

		dagState.InFlightIssues = nil // level barrier reached
		saveCheckpoint(dagState, note)

		// Record results.
		dagState.CompletedIssues = append(dagState.CompletedIssues, levelResult.Completed...)
		dagState.FailedIssues = append(dagState.FailedIssues, levelResult.Failed...)
		for _, skipped := range levelResult.Skipped {
			if !contains(dagState.SkippedIssues, skipped.IssueName) {
				dagState.SkippedIssues = append(dagState.SkippedIssues, skipped.IssueName)
			}
		}

		if note != nil {
			var completedNames, failedNames []string
			for _, r := range levelResult.Completed {
				completedNames = append(completedNames, r.IssueName)
			}
			for _, r := range levelResult.Failed {
				failedNames = append(failedNames, r.IssueName)
			}
			note(fmt.Sprintf("Level %d complete: completed=%s, failed=%s",
				dagState.CurrentLevel, pyStrList(completedNames), pyStrList(failedNames)),
				[]string{"execution", "level", "complete"})
		}

		// --- LEVEL FAILURE ABORT CHECK ---
		totalInLevel := len(levelResult.Completed) + len(levelResult.Failed) + len(levelResult.Skipped)
		if totalInLevel > 0 && cfg.LevelFailureAbortThreshold > 0 {
			failureRatio := float64(len(levelResult.Failed)) / float64(totalInLevel)
			if failureRatio >= cfg.LevelFailureAbortThreshold && len(levelResult.Failed) > 1 {
				if note != nil {
					note(fmt.Sprintf("Level %d failure ratio %.0f%% >= threshold %.0f%% — "+
						"aborting DAG to prevent cascading failures",
						dagState.CurrentLevel, failureRatio*100, cfg.LevelFailureAbortThreshold*100),
						[]string{"execution", "abort", "level_failure_threshold"})
				}
				for _, futureLevel := range dagState.Levels[dagState.CurrentLevel+1:] {
					for _, name := range futureLevel {
						if !contains(dagState.SkippedIssues, name) {
							dagState.SkippedIssues = append(dagState.SkippedIssues, name)
						}
					}
				}
				dagState.CurrentLevel = len(dagState.Levels)
				saveCheckpoint(dagState, note)
				break
			}
		}

		// --- MERGE GATE (git workflow) ---
		fileConflicts := asMapSlice(planResult["file_conflicts"])
		cleanupStarted := false
		if callFn != nil && dagState.GitIntegrationBranch != "" {
			mergeResult, err := mergeLevelBranches(ctx, dagState, &levelResult, callFn, nodeID, cfg, issueByName, fileConflicts, note)
			if err != nil {
				return nil, err
			}
			if mergeResult != nil {
				if _, err := runIntegrationTests(ctx, dagState, mergeResult, &levelResult, callFn, nodeID, cfg, issueByName, note); err != nil {
					return nil, err
				}
			}

			// Start cleanup in the background (doesn't affect replan decisions).
			bid := dagState.BuildID
			var branchesToClean []string
			for _, i := range activeIssues {
				if bn := mapGetStr(i, "branch_name", ""); bn != "" {
					branchesToClean = append(branchesToClean, bn)
				} else {
					seq := zfill2(asInt(i["sequence_number"]))
					if bid != "" {
						branchesToClean = append(branchesToClean, "issue/"+bid+"-"+seq+"-"+asStr(i["name"]))
					} else {
						branchesToClean = append(branchesToClean, "issue/"+seq+"-"+asStr(i["name"]))
					}
				}
			}
			startCleanup(branchesToClean, dagState.CurrentLevel, levelResult.Completed)
			cleanupStarted = true
		}
		_ = cleanupStarted

		// --- DEBT GATE: process COMPLETED_WITH_DEBT results ---
		var debtResults []schemas.IssueResult
		for _, r := range levelResult.Completed {
			if r.Outcome == schemas.IssueOutcomeCompletedWithDebt {
				debtResults = append(debtResults, r)
			}
		}
		if len(debtResults) > 0 {
			for _, r := range debtResults {
				dagState.AccumulatedDebt = append(dagState.AccumulatedDebt, r.DebtItems...)
				for _, adapt := range r.Adaptations {
					dagState.AdaptationHistory = append(dagState.AdaptationHistory, dumpToMap(adapt))
				}
				downstream := dagutil.FindDownstream(r.IssueName, dagState.AllIssues)
				for i, iss := range dagState.AllIssues {
					if downstream[asStr(iss["name"])] {
						notes := append([]any{}, asAnySlice(iss["debt_notes"])...)
						debtDesc := joinDebtDescriptions(r.DebtItems)
						notes = append(notes, fmt.Sprintf("NOTE: Upstream '%s' completed with debt: %s", r.IssueName, debtDesc))
						updated := copyIssue(iss)
						updated["debt_notes"] = notes
						dagState.AllIssues[i] = updated
					}
				}
			}
			if note != nil {
				note(fmt.Sprintf("Debt gate: %d issues accepted with debt, total debt items: %d",
					len(debtResults), len(dagState.AccumulatedDebt)),
					[]string{"execution", "debt_gate"})
			}
		}

		// --- SPLIT GATE: handle FAILED_NEEDS_SPLIT results ---
		var splitResults []schemas.IssueResult
		for _, f := range levelResult.Failed {
			if f.Outcome == schemas.IssueOutcomeFailedNeedsSplit && f.SplitRequest != nil && len(*f.SplitRequest) > 0 {
				splitResults = append(splitResults, f)
			}
		}
		if len(splitResults) > 0 && callFn != nil {
			for _, sr := range splitResults {
				newIssues := make([]map[string]any, 0, len(*sr.SplitRequest))
				for _, sub := range *sr.SplitRequest {
					subDict := dumpToMap(sub)
					subDict["parent_issue_name"] = sr.IssueName
					newIssues = append(newIssues, subDict)
				}
				var splitNames []string
				for _, s := range *sr.SplitRequest {
					splitNames = append(splitNames, s.Name)
				}
				splitDecision := schemas.ReplanDecision{
					Action:            schemas.ReplanActionModifyDAG,
					Rationale:         fmt.Sprintf("Issue '%s' split into %d sub-issues by Issue Advisor", sr.IssueName, len(newIssues)),
					NewIssues:         newIssues,
					RemovedIssueNames: []string{sr.IssueName},
					Summary:           fmt.Sprintf("Split %s", sr.IssueName),
				}
				newState, err := dagutil.ApplyReplan(dagState, splitDecision)
				if err != nil {
					if note != nil {
						note(fmt.Sprintf("Split produced invalid DAG (cycle): %v", err),
							[]string{"execution", "split_gate", "error"})
					}
					continue
				}
				dagState = newState
				issueByName = indexByName(dagState.AllIssues)
				writeIssueFilesForReplan(ctx, splitDecision, dagState, cfg, callFn, nodeID, note)
				if note != nil {
					note(fmt.Sprintf("Split gate: %s → %s", sr.IssueName, pyStrList(splitNames)),
						[]string{"execution", "split_gate"})
				}
			}

			// Remove split results from the failed list (they've been handled).
			var remainingFailed []schemas.IssueResult
			for _, f := range levelResult.Failed {
				if f.Outcome != schemas.IssueOutcomeFailedNeedsSplit {
					remainingFailed = append(remainingFailed, f)
				}
			}
			levelResult.Failed = remainingFailed
			saveCheckpoint(dagState, note)
		}

		// --- REPLAN GATE: check for unrecoverable and escalated failures ---
		var unrecoverable []schemas.IssueResult
		for _, f := range levelResult.Failed {
			if f.Outcome == schemas.IssueOutcomeFailedUnrecoverable || f.Outcome == schemas.IssueOutcomeFailedEscalated {
				unrecoverable = append(unrecoverable, f)
			}
		}

		if len(unrecoverable) > 0 {
			if cfg.EnableReplanning && dagState.ReplanCount < cfg.MaxReplans {
				var decision schemas.ReplanDecision
				if callFn != nil {
					d, err := invokeReplannerViaCall(ctx, dagState, unrecoverable, cfg, callFn, nodeID, note)
					if err != nil {
						return nil, err
					}
					decision = d
				} else {
					decision = invokeReplannerDirect(dagState, unrecoverable, cfg, note)
				}

				switch {
				case decision.Action == schemas.ReplanActionAbort:
					dagState.ReplanCount++
					dagState.ReplanHistory = append(dagState.ReplanHistory, decision)
					if note != nil {
						note(fmt.Sprintf("Replanner decided to ABORT: %s", decision.Rationale),
							[]string{"execution", "abort"})
					}
					if err := awaitCleanup(); err != nil {
						return nil, err
					}
					break mainLoop

				case decision.Action == schemas.ReplanActionContinue:
					enrichDownstreamWithFailureNotes(dagState, unrecoverable)
					dagState.ReplanCount++
					dagState.ReplanHistory = append(dagState.ReplanHistory, decision)
					skipDownstream(dagState, unrecoverable)

				default:
					// MODIFY_DAG or REDUCE_SCOPE — apply the replan.
					if err := awaitCleanup(); err != nil {
						return nil, err
					}
					newState, err := dagutil.ApplyReplan(dagState, decision)
					if err != nil {
						if note != nil {
							note(fmt.Sprintf("Replan produced invalid DAG (cycle): %v", err),
								[]string{"execution", "replan", "error"})
						}
						skipDownstream(dagState, unrecoverable)
					} else {
						dagState = newState
						issueByName = indexByName(dagState.AllIssues)
						if len(decision.NewIssues) > 0 || len(decision.UpdatedIssues) > 0 {
							writeIssueFilesForReplan(ctx, decision, dagState, cfg, callFn, nodeID, note)
						}
						saveCheckpoint(dagState, note)
						continue // current_level was reset to 0 by apply_replan
					}
				}
			} else {
				// Replanning exhausted or disabled — skip downstream.
				skipDownstream(dagState, unrecoverable)
				if note != nil {
					note(fmt.Sprintf("No replanning available — skipping downstream: %s", pyStrList(dagState.SkippedIssues)),
						[]string{"execution", "skip"})
				}
			}
		}

		// Ensure cleanup is done before advancing to next level's worktree setup.
		if err := awaitCleanup(); err != nil {
			return nil, err
		}

		dagState.CurrentLevel++
	}

	// Final worktree sweep — catch anything the per-level cleanup missed.
	if callFn != nil && dagState.WorktreesDir != "" && dagState.GitIntegrationBranch != "" {
		bid := dagState.BuildID
		var allBranches []string
		for _, i := range dagState.AllIssues {
			seq := zfill2(asInt(i["sequence_number"]))
			if bid != "" {
				allBranches = append(allBranches, "issue/"+bid+"-"+seq+"-"+asStr(i["name"]))
			} else {
				allBranches = append(allBranches, "issue/"+seq+"-"+asStr(i["name"]))
			}
		}
		if len(allBranches) > 0 {
			if note != nil {
				note("Final cleanup sweep for any residual worktrees",
					[]string{"execution", "worktree_cleanup", "final_sweep"})
			}
			if err := cleanupWorktrees(ctx, dagState, allBranches, callFn, nodeID, note,
				dagState.CurrentLevel, cfg.GitModel(), cfg.AIProvider(), cfg.DeterministicGit, nil); err != nil {
				return nil, err
			}
		}
	}

	if note != nil {
		note(fmt.Sprintf("DAG execution complete: %d/%d completed, %d failed, %d skipped, %d replans",
			len(dagState.CompletedIssues), len(dagState.AllIssues), len(dagState.FailedIssues),
			len(dagState.SkippedIssues), dagState.ReplanCount),
			[]string{"execution", "complete"})
	}

	// Final checkpoint.
	saveCheckpoint(dagState, note)

	return dagState, nil
}

// indexByName builds a name -> issue-dict lookup (Python {i["name"]: i}).
func indexByName(issues []map[string]any) map[string]map[string]any {
	m := make(map[string]map[string]any, len(issues))
	for _, i := range issues {
		m[asStr(i["name"])] = i
	}
	return m
}

// reselect returns the stored issue dicts for the given active issues, so the
// executor operates on the persisted objects after enrichment (matching Python
// where active_issues holds the enriched dicts written into all_issues).
func reselect(activeIssues []map[string]any, byName map[string]map[string]any) []map[string]any {
	out := make([]map[string]any, 0, len(activeIssues))
	for _, i := range activeIssues {
		if stored, ok := byName[asStr(i["name"])]; ok {
			out = append(out, stored)
		} else {
			out = append(out, i)
		}
	}
	return out
}

// joinDebtDescriptions renders "; ".join(d.get("description", d.get("criterion",""))
// for d in debt_items).
func joinDebtDescriptions(debtItems []map[string]any) string {
	parts := make([]string, 0, len(debtItems))
	for _, d := range debtItems {
		if desc, has := d["description"]; has {
			parts = append(parts, asStr(desc))
		} else {
			parts = append(parts, mapGetStr(d, "criterion", ""))
		}
	}
	out := ""
	for i, p := range parts {
		if i > 0 {
			out += "; "
		}
		out += p
	}
	return out
}
