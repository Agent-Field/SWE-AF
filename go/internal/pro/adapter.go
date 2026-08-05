package pro

// adapter.go bridges the planner's external-executor contract onto the pro
// engine's native task interface. pro_execute is registered (env-gated) on the
// planner node and matches the execute_fn_target calling convention exactly —
// (issue, repo_path) in, an issue-execution result out — so a build opts in
// with `config.execute_fn_target = "<node>.pro_execute"` and nothing else.

import (
	"context"
	"fmt"
	"os"
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/afx"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// CallFn dispatches to a reasoner by target with the same keyword args Python
// passes to app.call. Structurally identical to coding.CallFn / issue.CallFn,
// so the node's single agent.Call + envelope-unwrap closure satisfies all.
type CallFn func(ctx context.Context, target string, kwargs map[string]any) (map[string]any, error)

// Noter matches the SDK's *agent.Agent.Note method; tests supply a recorder.
type Noter interface {
	Note(ctx context.Context, message string, tags ...string)
}

// Handler is the registration signature, matching the roles/issue packages.
type Handler func(ctx context.Context, deps *Deps, input map[string]any) (any, error)

// Handlers returns the name→handler map for the env-gated pro surface.
func Handlers() map[string]Handler {
	return map[string]Handler{"pro_execute": ProExecute}
}

// Deps carries the seams pro_execute needs.
type Deps struct {
	Call CallFn
	Note Noter
	// EngineNode is the engine's control-plane node id (NodeID() in wiring);
	// dispatch goes to "<EngineNode>.code_task".
	EngineNode string
}

func (d *Deps) note(ctx context.Context, msg string, tags ...string) {
	if d != nil && d.Note != nil {
		d.Note.Note(ctx, msg, tags...)
	}
}

type proExecuteInput struct {
	Issue    map[string]any `json:"issue"`
	RepoPath string         `json:"repo_path"`
}

// ProExecute routes one fully-scoped issue to the pro engine and maps the
// engine's terminal result onto the shape runExecuteFn consumes (outcome /
// result_summary / error_message / error_context). Engine-reported failures
// come back as a failed outcome — not an error — so the advisor loop owns the
// retry/split/escalate decision, same as the built-in coding loop.
func ProExecute(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := afx.Bind[proExecuteInput](input)
	if err != nil {
		return nil, err
	}
	if len(in.Issue) == 0 {
		return nil, fmt.Errorf("pro_execute: issue is required")
	}
	if in.RepoPath == "" {
		return nil, fmt.Errorf("pro_execute: repo_path is required")
	}

	kwargs := map[string]any{
		"goal": ComposeGoal(in.Issue),
		"dir":  in.RepoPath,
	}
	// Optional env-driven dispatch overrides: cost ceiling, sub-agent model
	// pools and reasoning-effort variant. Unset keeps the engine's defaults.
	for env, kw := range map[string]string{
		EnvMaxCost:    "max_cost",
		EnvModelsHigh: "high",
		EnvModelsLow:  "low",
		EnvVariant:    "variant",
	} {
		if v := os.Getenv(env); v != "" {
			kwargs[kw] = v
		}
	}

	name, _ := in.Issue["name"].(string)
	deps.note(ctx, fmt.Sprintf("pro engine: dispatching issue %q", name), "pro")
	res, err := deps.Call(ctx, deps.EngineNode+".code_task", kwargs)
	if err != nil {
		// Transport/engine-crash errors go to runExecuteFn's retry path.
		return nil, err
	}
	out := mapEngineResult(res)
	deps.note(ctx, fmt.Sprintf("pro engine: issue %q → %s", name, out["outcome"]), "pro")
	return out, nil
}

// ComposeGoal flattens an issue dict (PlannedIssue / IssueSpec shape) into the
// single goal text the engine plans from. Sections are omitted when absent so
// a minimal {title, description} issue still reads naturally.
func ComposeGoal(issue map[string]any) string {
	var b strings.Builder
	title, _ := issue["title"].(string)
	if title == "" {
		title, _ = issue["name"].(string)
	}
	if title != "" {
		b.WriteString(title)
		b.WriteString("\n\n")
	}
	if d, _ := issue["description"].(string); d != "" {
		b.WriteString(d)
		b.WriteString("\n")
	}
	if ac := asStrings(issue["acceptance_criteria"]); len(ac) > 0 {
		b.WriteString("\nAcceptance criteria:\n")
		for _, c := range ac {
			b.WriteString("- ")
			b.WriteString(c)
			b.WriteString("\n")
		}
	}
	if fc := asStrings(issue["files_to_create"]); len(fc) > 0 {
		b.WriteString("\nFiles to create: ")
		b.WriteString(strings.Join(fc, ", "))
		b.WriteString("\n")
	}
	if fm := asStrings(issue["files_to_modify"]); len(fm) > 0 {
		b.WriteString("Files to modify: ")
		b.WriteString(strings.Join(fm, ", "))
		b.WriteString("\n")
	}
	if ts, _ := issue["testing_strategy"].(string); ts != "" {
		b.WriteString("\nTesting strategy: ")
		b.WriteString(ts)
		b.WriteString("\n")
	}
	return strings.TrimRight(b.String(), "\n")
}

// mapEngineResult maps the engine's terminal result {status, reason, cost_usd,
// cycle, run_id, ...} onto the external-executor result shape.
//
// Status mapping: "pass" completes; "budget-exhausted" is unrecoverable
// (retrying would burn the same budget again); everything else — fail,
// escalated, crashed, unknown — is retryable so SWE-AF's own advisor loop
// decides what happens next (the engine's internal escalation is not this
// DAG's escalation).
func mapEngineResult(res map[string]any) map[string]any {
	status, _ := res["status"].(string)
	reason, _ := res["reason"].(string)

	var outcome schemas.IssueOutcome
	switch status {
	case "pass":
		outcome = schemas.IssueOutcomeCompleted
	case "budget-exhausted":
		outcome = schemas.IssueOutcomeFailedUnrecoverable
	default:
		outcome = schemas.IssueOutcomeFailedRetryable
	}

	summary := fmt.Sprintf("pro engine: status=%s", status)
	if runID, _ := res["run_id"].(string); runID != "" {
		summary += " run=" + runID
	}
	if cost, ok := res["cost_usd"].(float64); ok {
		summary += fmt.Sprintf(" cost=$%.2f", cost)
	}

	out := map[string]any{
		"outcome":        string(outcome),
		"result_summary": summary,
	}
	if outcome != schemas.IssueOutcomeCompleted {
		msg := "pro engine reported status " + status
		if reason != "" {
			msg += ": " + reason
		}
		out["error_message"] = msg
		out["error_context"] = summary
	}
	return out
}

// asStrings coerces a JSON-decoded list ([]any of strings) into []string,
// tolerating an already-typed []string.
func asStrings(v any) []string {
	switch t := v.(type) {
	case []string:
		return t
	case []any:
		out := make([]string, 0, len(t))
		for _, e := range t {
			if s, ok := e.(string); ok && s != "" {
				out = append(out, s)
			}
		}
		return out
	}
	return nil
}
