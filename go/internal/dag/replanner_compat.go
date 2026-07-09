package dag

import (
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/config"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// invokeReplannerDirect is the backward-compat replanner path used when no
// call_fn is supplied (Python's _invoke_replanner_direct → _replanner_compat.
// invoke_replanner). It ports the observable behaviour of that path.
//
// Deviation from the Python source (documented, justified): the Python
// _replanner_compat.invoke_replanner invokes the replanner LLM directly through
// the module-global `router.harness` reasoner. The Go DAG package has no
// reference to an *agent.Agent (every reasoner is call_fn-routed by design), and
// this direct path is only reachable when call_fn is nil — a configuration that
// exists solely for legacy/unit contexts where the replanner is never actually
// invoked with real failures. When it *is* reached, the Python code's own
// failure fallback is an automatic ABORT ("Replanner agent failed to produce a
// valid decision. Aborting."). Since Go cannot run the harness here, this
// function returns exactly that documented fallback decision, matching the
// observable end state (replan_count/history handling is done by the caller via
// apply_replan for the ABORT action).
func invokeReplannerDirect(
	dagState *schemas.DAGState,
	failedIssues []schemas.IssueResult,
	cfg *config.ExecutionConfig,
	note noteFunc,
) schemas.ReplanDecision {
	if note != nil {
		var failedNames []string
		for _, f := range failedIssues {
			failedNames = append(failedNames, f.IssueName)
		}
		note(fmt.Sprintf("Replanning triggered (attempt %d/%d): failed issues = %s",
			dagState.ReplanCount+1, cfg.MaxReplans, pyStrList(failedNames)),
			[]string{"execution", "replan", "start"})
	}

	fallback := schemas.ReplanDecision{
		Action:    schemas.ReplanActionAbort,
		Rationale: "Replanner agent failed to produce a valid decision. Aborting.",
		Summary:   "Replanner failure — automatic abort.",
	}
	if note != nil {
		note("Replanner failed to produce valid output — falling back to ABORT",
			[]string{"execution", "replan", "fallback"})
	}
	return fallback
}
