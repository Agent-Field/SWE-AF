package fast

// verifier.go ports swe_af/fast/verifier.py::fast_verify — a single verification
// pass with NO fix cycles. It adapts the fast task_results into the
// completed/failed/skipped split run_verifier expects, delegates to run_verifier
// via CallFn, and maps the result into a FastVerificationResult. Any error from
// the verification agent yields a safe fallback (passed=false) whose summary
// contains "Verification agent failed". Intentionally references no fix-cycle
// machinery (generate_fix_issues / max_verify_fix_cycles / fix_cycles).

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

type verifierInput struct {
	PRD            map[string]any   `json:"prd"`
	RepoPath       string           `json:"repo_path"`
	TaskResults    []map[string]any `json:"task_results"`
	VerifierModel  string           `json:"verifier_model"`
	PermissionMode string           `json:"permission_mode"`
	AIProvider     string           `json:"ai_provider"`
	ArtifactsDir   string           `json:"artifacts_dir"`
}

// UnmarshalJSON seeds the Python parameter defaults (verifier_model="sonnet",
// ai_provider="claude").
func (v *verifierInput) UnmarshalJSON(data []byte) error {
	*v = verifierInput{VerifierModel: "sonnet", AIProvider: "claude"}
	type alias verifierInput
	return json.Unmarshal(data, (*alias)(v))
}

// FastVerify ports fast_verify — one verification pass against the built repo.
func FastVerify(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bind[verifierInput](input)
	if err != nil {
		return nil, err
	}

	verification, verifyErr := runFastVerify(ctx, deps, in)
	if verifyErr != nil {
		fallback := &schemas.FastVerificationResult{
			Passed:          false,
			Summary:         fmt.Sprintf("Verification agent failed: %s", verifyErr),
			CriteriaResults: []map[string]any{},
			SuggestedFixes:  []string{},
		}
		return fallback, nil
	}
	return verification, nil
}

// runFastVerify performs the delegating call and result mapping, returning an
// error for any failure so FastVerify can apply the single fallback (mirroring
// the Python try/except around the whole body).
func runFastVerify(ctx context.Context, deps *Deps, in verifierInput) (*schemas.FastVerificationResult, error) {
	// Split task_results into completed/failed for run_verifier's interface.
	completedIssues := []map[string]any{}
	failedIssues := []map[string]any{}
	for _, tr := range in.TaskResults {
		entry := map[string]any{
			"issue_name":     getString(tr, "task_name", ""),
			"result_summary": getString(tr, "summary", ""),
		}
		if getString(tr, "outcome", "") == "completed" {
			completedIssues = append(completedIssues, entry)
		} else {
			failedIssues = append(failedIssues, entry)
		}
	}

	result, err := deps.Call(ctx, deps.nodeID()+".run_verifier", map[string]any{
		"prd":              in.PRD,
		"repo_path":        in.RepoPath,
		"artifacts_dir":    in.ArtifactsDir,
		"completed_issues": completedIssues,
		"failed_issues":    failedIssues,
		"skipped_issues":   []any{},
		"model":            in.VerifierModel,
		"permission_mode":  in.PermissionMode,
		"ai_provider":      in.AIProvider,
	})
	if err != nil {
		return nil, err
	}

	return &schemas.FastVerificationResult{
		Passed:          mapBool(result, "passed"),
		Summary:         getString(result, "summary", ""),
		CriteriaResults: mapSlice(result["criteria_results"]),
		SuggestedFixes:  stringSlice(result["suggested_fixes"]),
	}, nil
}

// mapSlice coerces a JSON list into []map[string]any, always non-nil so it
// marshals to [] (matching Pydantic's list[dict] default). Non-object elements
// are skipped.
func mapSlice(v any) []map[string]any {
	out := []map[string]any{}
	if items, ok := v.([]any); ok {
		for _, it := range items {
			if m, ok := it.(map[string]any); ok {
				out = append(out, m)
			}
		}
	}
	return out
}
