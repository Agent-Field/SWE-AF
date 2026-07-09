package hitl

import (
	"context"
	"encoding/json"
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// AskUserBudget is the per-build cap on ask-user pauses, shared across every
// wrapped reasoner call site. Ports wrapper.py::AskUserBudget. The Go port
// defaults a nil budget to Remaining=2 (design/breakdown T2.4).
type AskUserBudget struct {
	Remaining int `json:"remaining"`
}

// DefaultAskUserBudget is the fallback budget when a caller passes none.
const DefaultAskUserBudget = 2

// DefaultAskUserMaxIterations bounds re-invocation per call site independent of
// the shared budget. Ports wrapper.py::run_with_ask_user's max_iterations=3.
const DefaultAskUserMaxIterations = 3

// PriorUserResponse is one entry appended to prior_user_responses between
// re-invocations. Ports wrapper.py::PriorUserResponse.
type PriorUserResponse struct {
	Question string         `json:"question"`
	Status   string         `json:"status"`
	Values   map[string]any `json:"values"`
	Feedback *string        `json:"feedback"`
}

// ReasonerInvoke re-invokes a reasoner with the (possibly grown) kwargs and
// returns its parsed output as a model_dump-equivalent map. The wrapper inspects
// result["ask_user_form"] to decide whether to pause and re-invoke.
type ReasonerInvoke func(ctx context.Context, kwargs map[string]any) (map[string]any, error)

// RunWithAskUserParams configures the ask-user loop. Budget is a pointer so the
// shared per-build cap mutates across call sites (matching Python's mutated
// AskUserBudget). A nil Budget defaults to Remaining=DefaultAskUserBudget.
type RunWithAskUserParams struct {
	App            App
	Pauser         Pauser
	Hax            *HaxClient
	Budget         *AskUserBudget
	MaxIterations  int // default DefaultAskUserMaxIterations
	ExpiresInHours float64
	NodeID         string
	ExecutionID    string
	UserID         string
	WebhookURL     string
	Metadata       map[string]any
	NoteLabel      string
}

// RunWithAskUser applies the ask-user pause/resume loop to a reasoner. Ports
// wrapper.py::run_with_ask_user.
//
// It invokes the reasoner; while the parsed result carries a non-null
// ask_user_form it pauses for human input, appends the answers to
// prior_user_responses, and re-invokes — bounded by the shared budget and by
// MaxIterations. When hax is disabled (nil client), the budget is exhausted, or
// the iteration cap is hit, it returns the current result with ask_user_form
// stripped so downstream code never sees an unfulfilled ask.
func RunWithAskUser(
	ctx context.Context,
	invoke ReasonerInvoke,
	kwargs map[string]any,
	p RunWithAskUserParams,
) (map[string]any, error) {
	label := p.NoteLabel
	if label == "" {
		label = "reasoner"
	}
	maxIter := p.MaxIterations
	if maxIter <= 0 {
		maxIter = DefaultAskUserMaxIterations
	}
	budget := p.Budget
	if budget == nil {
		budget = &AskUserBudget{Remaining: DefaultAskUserBudget}
	}

	if kwargs == nil {
		kwargs = map[string]any{}
	}
	if _, ok := kwargs["prior_user_responses"]; !ok {
		kwargs["prior_user_responses"] = []any{}
	}

	var result map[string]any
	for iteration := 0; iteration <= maxIter; iteration++ {
		var err error
		result, err = invoke(ctx, kwargs)
		if err != nil {
			return nil, err
		}

		spec, err := extractAskUserForm(result)
		if err != nil {
			return nil, err
		}
		if spec == nil {
			return result, nil
		}

		if p.Hax == nil {
			noteSafe(ctx, p.App, fmt.Sprintf(
				"%s: LLM emitted ask_user_form but HAX is disabled — ignoring and proceeding with current decision", label),
				"ask_user", "skipped", "hax_disabled")
			return clearAskUserForm(result), nil
		}
		if budget.Remaining <= 0 {
			noteSafe(ctx, p.App, fmt.Sprintf(
				"%s: ask_user budget exhausted (remaining=0) — ignoring further asks and proceeding", label),
				"ask_user", "skipped", "budget_exhausted")
			return clearAskUserForm(result), nil
		}
		if iteration >= maxIter {
			noteSafe(ctx, p.App, fmt.Sprintf(
				"%s: ask_user max_iterations (%d) reached without converging — proceeding", label, maxIter),
				"ask_user", "skipped", "max_iterations")
			return clearAskUserForm(result), nil
		}

		budget.Remaining--
		noteSafe(ctx, p.App, fmt.Sprintf(
			"%s: pausing for ask_user_via_form (iteration %d, budget_remaining=%d)", label, iteration, budget.Remaining),
			"ask_user", "pause", label)

		response := RequestUserInputAndPause(ctx, p.App, p.Pauser, p.Hax, *spec, RequestUserInputParams{
			NodeID:         p.NodeID,
			ExecutionID:    p.ExecutionID,
			ExpiresInHours: p.ExpiresInHours,
			UserID:         p.UserID,
			WebhookURL:     p.WebhookURL,
			Metadata:       p.Metadata,
		})

		prior := appendPriorResponse(kwargs["prior_user_responses"], PriorUserResponse{
			Question: spec.Title,
			Status:   response.Status,
			Values:   response.Values,
			Feedback: response.Feedback,
		})
		kwargs["prior_user_responses"] = prior
	}

	return clearAskUserForm(result), nil
}

// extractAskUserForm pulls a populated ask_user_form off a reasoner output.
// Ports wrapper.py::_extract_ask_user_form: nil/absent -> no form; a struct or
// map is normalized into an AskUserForm.
func extractAskUserForm(result map[string]any) (*schemas.AskUserForm, error) {
	if result == nil {
		return nil, nil
	}
	raw, ok := result["ask_user_form"]
	if !ok || raw == nil {
		return nil, nil
	}
	if form, ok := raw.(*schemas.AskUserForm); ok {
		if form == nil {
			return nil, nil
		}
		return form, nil
	}
	if form, ok := raw.(schemas.AskUserForm); ok {
		return &form, nil
	}
	b, err := json.Marshal(raw)
	if err != nil {
		return nil, fmt.Errorf("ask_user_form: marshal: %w", err)
	}
	var form schemas.AskUserForm
	if err := json.Unmarshal(b, &form); err != nil {
		return nil, fmt.Errorf("ask_user_form: validate: %w", err)
	}
	return &form, nil
}

// clearAskUserForm sets ask_user_form to null on the result (when present),
// mirroring wrapper.py::_clear_ask_user_form. The key is kept (value null) so
// the output shape matches Python's model_dump.
func clearAskUserForm(result map[string]any) map[string]any {
	if result == nil {
		return result
	}
	if _, ok := result["ask_user_form"]; ok {
		result["ask_user_form"] = nil
	}
	return result
}

// appendPriorResponse appends one PriorUserResponse (as a map) to whatever list
// currently lives under prior_user_responses, tolerating the several shapes it
// can arrive as ([]any from JSON, []map[string]any, or nil).
func appendPriorResponse(existing any, entry PriorUserResponse) []any {
	var out []any
	switch v := existing.(type) {
	case []any:
		out = append(out, v...)
	case []map[string]any:
		for _, m := range v {
			out = append(out, m)
		}
	}
	values := entry.Values
	if values == nil {
		values = map[string]any{}
	}
	out = append(out, map[string]any{
		"question": entry.Question,
		"status":   entry.Status,
		"values":   values,
		"feedback": entry.Feedback,
	})
	return out
}
