package fast

// planner.go ports swe_af/fast/planner.py::fast_plan_tasks — single-pass flat
// task decomposition using one structured-output harness call. On any harness
// failure OR an unparseable response, a deterministic single-task fallback plan
// (named "implement-goal", fallback_used=true) is returned. Intentionally does
// NOT reference any planning-pipeline role (run_architect/run_tech_lead/…) so
// the fast node never loads them.

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/harnessx"
	prompts "github.com/Agent-Field/SWE-AF/go/internal/prompts/advisor"
	"github.com/Agent-Field/SWE-AF/go/internal/runtimex"
	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

type plannerInput struct {
	Goal              string `json:"goal"`
	RepoPath          string `json:"repo_path"`
	MaxTasks          int    `json:"max_tasks"`
	PMModel           string `json:"pm_model"`
	PermissionMode    string `json:"permission_mode"`
	AIProvider        string `json:"ai_provider"`
	AdditionalContext string `json:"additional_context"`
	ArtifactsDir      string `json:"artifacts_dir"`
}

// UnmarshalJSON seeds the Python parameter defaults (max_tasks=10,
// pm_model="haiku", ai_provider="claude").
func (p *plannerInput) UnmarshalJSON(data []byte) error {
	*p = plannerInput{MaxTasks: 10, PMModel: "haiku", AIProvider: "claude"}
	type alias plannerInput
	return json.Unmarshal(data, (*alias)(p))
}

// fallbackPlan ports _fallback_plan: a single-task plan used when the LLM call
// fails or returns an unparseable result.
func fallbackPlan(goal string) *schemas.FastPlanResult {
	return &schemas.FastPlanResult{
		Tasks: []schemas.FastTask{
			{
				Name:               "implement-goal",
				Title:              "Implement goal",
				Description:        goal,
				AcceptanceCriteria: []string{"Goal is implemented successfully."},
				FilesToCreate:      []string{},
				FilesToModify:      []string{},
				EstimatedMinutes:   5,
			},
		},
		Rationale:    "Fallback plan: LLM did not return a parseable result.",
		FallbackUsed: true,
	}
}

// FastPlanTasks ports fast_plan_tasks — decompose a build goal into a flat
// ordered task list via a single structured-output harness call.
func FastPlanTasks(ctx context.Context, deps *Deps, input map[string]any) (any, error) {
	in, err := bind[plannerInput](input)
	if err != nil {
		return nil, err
	}

	deps.note(ctx, fmt.Sprintf("fast_plan_tasks: starting decomposition for goal=%s max_tasks=%d",
		pyRepr(in.Goal), in.MaxTasks), "fast_planner", "start")

	taskPrompt := prompts.FastPlannerTaskPrompt(prompts.FastPlannerTaskOptions{
		Goal:              in.Goal,
		RepoPath:          in.RepoPath,
		MaxTasks:          in.MaxTasks,
		AdditionalContext: in.AdditionalContext,
	})

	provider, err := runtimex.RuntimeToHarnessAdapter(in.AIProvider)
	if err != nil {
		return nil, err
	}

	opts := harnessx.RoleOptions{
		Provider:       provider,
		Model:          in.PMModel,
		MaxTurns:       3,
		PermissionMode: in.PermissionMode,
		SystemPrompt:   prompts.FastPlannerSystemPrompt,
		Cwd:            in.RepoPath,
	}.ToOptions()

	// The Python planner catches EVERY exception (including fatal API errors)
	// and returns the fallback — the fast planner is deliberately lenient, so
	// unlike the full-pipeline roles it does NOT propagate *FatalHarnessError.
	parsed, result, hErr := harnessx.Run[schemas.FastPlanResult](ctx, deps.Harness, taskPrompt, opts)
	if hErr != nil {
		deps.note(ctx, fmt.Sprintf("fast_plan_tasks: LLM call failed (%s); returning fallback plan", hErr),
			"fast_planner", "fallback", "error")
		return fallbackPlan(in.Goal), nil
	}

	// A nil parsed result (harness could not parse valid JSON) → fallback.
	if result == nil || result.Parsed == nil {
		deps.note(ctx, "fast_plan_tasks: parsed response is None; returning fallback plan",
			"fast_planner", "fallback")
		return fallbackPlan(in.Goal), nil
	}

	plan := *parsed

	// fallback_used is planner-side state, not an LLM self-assessment: anything
	// that parsed cleanly here did NOT use the fallback, so force it False even
	// if the model invented true (codex strict-schema strips the default).
	if plan.FallbackUsed {
		plan.FallbackUsed = false
	}

	// Truncate to max_tasks.
	if len(plan.Tasks) > in.MaxTasks {
		plan.Tasks = plan.Tasks[:in.MaxTasks]
	}

	deps.note(ctx, fmt.Sprintf("fast_plan_tasks: produced %d task(s)", len(plan.Tasks)),
		"fast_planner", "done")
	return &plan, nil
}
