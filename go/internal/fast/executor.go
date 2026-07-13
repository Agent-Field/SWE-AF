package fast

// executor.go ports swe_af/fast/executor.py::fast_execute_tasks — strictly
// sequential single-coder-pass execution. One run_coder call per task (via
// CallFn), each bounded by a per-task timeout (default 300s). On per-task
// timeout the outcome is "timeout" and execution continues to the next task; on
// per-task failure the outcome is "failed" and execution continues. There is no
// QA, no code-reviewer, no synthesizer, no replanning and no worktrees.

import (
	"context"
	"encoding/json"
	"fmt"
	"time"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

type executorInput struct {
	Tasks              []map[string]any `json:"tasks"`
	RepoPath           string           `json:"repo_path"`
	CoderModel         string           `json:"coder_model"`
	PermissionMode     string           `json:"permission_mode"`
	AIProvider         string           `json:"ai_provider"`
	TaskTimeoutSeconds int              `json:"task_timeout_seconds"`
	ArtifactsDir       string           `json:"artifacts_dir"`
	AgentMaxTurns      int              `json:"agent_max_turns"`
}

// UnmarshalJSON seeds the Python parameter defaults (coder_model="haiku",
// ai_provider="claude", task_timeout_seconds=300, agent_max_turns=50).
func (e *executorInput) UnmarshalJSON(data []byte) error {
	*e = executorInput{CoderModel: "haiku", AIProvider: "claude", TaskTimeoutSeconds: 300, AgentMaxTurns: 50}
	type alias executorInput
	return json.Unmarshal(data, (*alias)(e))
}

// FastExecuteTasks ports fast_execute_tasks — sequential single-coder execution
// over the flat task list.
func FastExecuteTasks(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bind[executorInput](input)
	if err != nil {
		return nil, err
	}

	node := deps.nodeID()
	timeout := time.Duration(in.TaskTimeoutSeconds) * time.Second

	taskResults := []schemas.FastTaskResult{}

	for _, taskDict := range in.Tasks {
		taskName := getString(taskDict, "name", "unknown")
		deps.note(ctx, fmt.Sprintf("Fast executor: starting task %s", taskName),
			"fast_executor", "task_start")

		// Construct the issue dict compatible with run_coder's expectations.
		issue := map[string]any{
			"name":                taskName,
			"title":               getString(taskDict, "title", taskName),
			"description":         getString(taskDict, "description", ""),
			"acceptance_criteria": getList(taskDict, "acceptance_criteria"),
			"files_to_create":     getList(taskDict, "files_to_create"),
			"files_to_modify":     getList(taskDict, "files_to_modify"),
			"testing_strategy":    "",
		}

		projectContext := map[string]any{
			"artifacts_dir": in.ArtifactsDir,
			"repo_path":     in.RepoPath,
		}

		kwargs := map[string]any{
			"issue":           issue,
			"worktree_path":   in.RepoPath, // no worktrees — coder works in repo_path
			"iteration":       1,
			"iteration_id":    taskName,
			"project_context": projectContext,
			"model":           in.CoderModel,
			"permission_mode": in.PermissionMode,
			"ai_provider":     in.AIProvider,
		}

		coderResult, callErr, timedOut := callWithTimeout(ctx, deps.Call, node+".run_coder", kwargs, timeout)
		switch {
		case timedOut:
			deps.note(ctx, fmt.Sprintf("Fast executor: task %s timed out after %ds", taskName, in.TaskTimeoutSeconds),
				"fast_executor", "timeout")
			taskResults = append(taskResults, schemas.FastTaskResult{
				TaskName:     taskName,
				Outcome:      "timeout",
				FilesChanged: []string{},
				Error:        fmt.Sprintf("Timed out after %ds", in.TaskTimeoutSeconds),
			})
		case callErr != nil:
			if ctx.Err() != nil {
				// Parent cancellation (e.g. build-level timeout) — propagate,
				// mirroring how asyncio.CancelledError escapes the loop.
				return nil, callErr
			}
			deps.note(ctx, fmt.Sprintf("Fast executor: task %s failed: %s", taskName, callErr),
				"fast_executor", "error")
			taskResults = append(taskResults, schemas.FastTaskResult{
				TaskName:     taskName,
				Outcome:      "failed",
				FilesChanged: []string{},
				Error:        callErr.Error(),
			})
		default:
			outcome := "failed"
			if mapBool(coderResult, "complete") {
				outcome = "completed"
			}
			taskResults = append(taskResults, schemas.FastTaskResult{
				TaskName:     taskName,
				Outcome:      outcome,
				FilesChanged: stringSlice(coderResult["files_changed"]),
				Summary:      getString(coderResult, "summary", ""),
			})
			deps.note(ctx, fmt.Sprintf("Fast executor: task %s done, outcome=%s", taskName, outcome),
				"fast_executor", "task_done")
		}
	}

	completed := 0
	for _, r := range taskResults {
		if r.Outcome == "completed" {
			completed++
		}
	}
	failed := len(taskResults) - completed

	return &schemas.FastExecutionResult{
		TaskResults:    taskResults,
		CompletedCount: completed,
		FailedCount:    failed,
	}, nil
}

// callWithTimeout invokes callFn under a per-task deadline, mirroring the Python
// asyncio.wait_for(coro, timeout). It returns (result, err, timedOut): timedOut
// is true only when the task deadline (not parent cancellation) fired. A
// parent-cancelled context surfaces as a non-nil err with timedOut=false so the
// caller can propagate it.
func callWithTimeout(
	ctx context.Context,
	callFn CallFn,
	target string,
	kwargs map[string]any,
	timeout time.Duration,
) (map[string]any, error, bool) {
	tctx, cancel := context.WithTimeout(ctx, timeout)
	defer cancel()

	type res struct {
		m   map[string]any
		err error
	}
	ch := make(chan res, 1)
	go func() {
		m, err := callFn(tctx, target, kwargs)
		ch <- res{m, err}
	}()

	select {
	case <-tctx.Done():
		if ctx.Err() != nil {
			return nil, ctx.Err(), false // parent cancelled → propagate
		}
		return nil, nil, true // task-level timeout
	case r := <-ch:
		return r.m, r.err, false
	}
}

// getList mirrors dict.get(key, []) returning the raw list value (as []any) when
// present, else an empty list — preserving whatever element shapes the caller
// supplied (run_coder consumes these as arbitrary JSON).
func getList(m map[string]any, key string) any {
	if m != nil {
		if v, ok := m[key]; ok && v != nil {
			return v
		}
	}
	return []any{}
}
