// Package hitl provides SWE-AF's human-in-the-loop substrate: the scoped
// credential store (credentials_store.go), the hax REST client, the ask-user
// form primitive, the re-invocation wrapper, and the environment-scout helpers.
//
// This file ports swe_af/hitl/ask_user.py: it turns an AskUserForm into the hax
// form-builder payload, drives the webhook-resumed pause flow via agent.Pause
// (design §4.6), and maps the resulting decision back into an AskUserResponse
// using the same table as the Python _parse_approval_result_to_response.
package hitl

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"sort"
	"strings"

	"github.com/Agent-Field/agentfield/sdk/go/agent"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// App is the minimal slice of *agent.Agent the HITL primitives need: the
// fire-and-forget note channel. Kept as an interface so tests can supply a
// silent stub (mirrors the Python tests that mock app.note).
type App interface {
	Note(ctx context.Context, message string, tags ...string)
}

// Pauser is the pause surface the ask-user flow drives. The AgentField Go SDK
// *agent.Agent satisfies it via its Pause method; tests supply a fake.
//
// Pause transitions the execution to "waiting" on the control plane and blocks
// until the approval webhook callback resolves it (or it expires) — the direct
// port of Python's app.pause() (design §4.6). There is no polling: the SDK
// registers the pending pause and the agent's /webhooks/approval route resolves
// it when the human responds.
type Pauser interface {
	Pause(ctx context.Context, opts agent.PauseOptions) (*agent.ApprovalResult, error)
}

// noteSafe fires a note when app is non-nil; a nil app is a no-op so the
// primitives stay usable in tests / contexts without an agent.
func noteSafe(ctx context.Context, app App, message string, tags ...string) {
	if app != nil {
		app.Note(ctx, message, tags...)
	}
}

// BuildHaxFormPayload translates an AskUserForm into the hax form-builder
// payload — a byte-for-byte port of build_form_builder + FormBuilder.to_payload
// (hax/form_builder.py) so the wire body matches the Python ask-user path.
//
// Top-level keys mirror FormBuilder._config: "title" always; "description" when
// set; "submitLabel" only when a non-default label is given. Each field mirrors
// the dict FormBuilder.<method> appends, with snake_case option keys converted
// to camelCase (default_value -> defaultValue, checkbox_label -> checkboxLabel,
// switch_label -> switchLabel).
//
// Errors mirror the Python ValueErrors verbatim (missing options / min-max).
func BuildHaxFormPayload(spec schemas.AskUserForm) (map[string]any, error) {
	fields := make([]map[string]any, 0, len(spec.Fields))
	for _, f := range spec.Fields {
		m, err := fieldToPayload(f)
		if err != nil {
			return nil, err
		}
		fields = append(fields, m)
	}

	payload := map[string]any{"title": spec.Title}
	if spec.Description != nil {
		payload["description"] = *spec.Description
	}
	if spec.SubmitLabel != "" && spec.SubmitLabel != "Submit" {
		payload["submitLabel"] = spec.SubmitLabel
	}
	payload["fields"] = fields
	return payload, nil
}

// fieldToPayload builds one field dict, mirroring _field_to_form_builder_call +
// the matching FormBuilder method (which sets the wire "type" string).
func fieldToPayload(f schemas.AskUserFormField) (map[string]any, error) {
	// common options shared by every widget, in the order the Python builder
	// assembles them (order is immaterial for JSON objects but kept for parity).
	base := func(includePlaceholder bool) map[string]any {
		m := map[string]any{"label": f.Label}
		if f.Description != nil {
			m["description"] = *f.Description
		}
		if f.Required {
			m["required"] = true
		}
		if includePlaceholder && f.Placeholder != nil {
			m["placeholder"] = *f.Placeholder
		}
		if f.DefaultValue != nil {
			m["defaultValue"] = f.DefaultValue
		}
		return m
	}

	switch f.Type {
	case schemas.FieldTypeInput:
		m := base(true)
		m["type"] = "input"
		m["id"] = f.ID
		return m, nil
	case schemas.FieldTypeTextarea:
		m := base(true)
		m["type"] = "textarea"
		m["id"] = f.ID
		return m, nil
	case schemas.FieldTypeNumber:
		m := base(true)
		m["type"] = "number"
		m["id"] = f.ID
		if f.Min != nil {
			m["min"] = *f.Min
		}
		if f.Max != nil {
			m["max"] = *f.Max
		}
		if f.Step != nil {
			m["step"] = *f.Step
		}
		return m, nil
	case schemas.FieldTypeSlider:
		if f.Min == nil || f.Max == nil {
			return nil, fmt.Errorf("slider field '%s' requires both min and max", f.ID)
		}
		m := base(true)
		m["type"] = "slider"
		m["id"] = f.ID
		m["min"] = *f.Min
		m["max"] = *f.Max
		if f.Step != nil {
			m["step"] = *f.Step
		}
		return m, nil
	case schemas.FieldTypeSelect:
		if len(f.Options) == 0 {
			return nil, fmt.Errorf("select field '%s' requires options", f.ID)
		}
		m := base(true)
		m["type"] = "select"
		m["id"] = f.ID
		m["options"] = f.Options
		return m, nil
	case schemas.FieldTypeRadio:
		if len(f.Options) == 0 {
			return nil, fmt.Errorf("radio field '%s' requires options", f.ID)
		}
		m := base(true)
		m["type"] = "radio-group"
		m["id"] = f.ID
		m["options"] = f.Options
		return m, nil
	case schemas.FieldTypeCheckboxGroup:
		if len(f.Options) == 0 {
			return nil, fmt.Errorf("checkbox_group field '%s' requires options", f.ID)
		}
		m := base(true)
		m["type"] = "checkbox-group"
		m["id"] = f.ID
		m["options"] = f.Options
		return m, nil
	case schemas.FieldTypeCheckbox:
		m := base(false) // placeholder popped for checkbox
		m["type"] = "checkbox"
		m["id"] = f.ID
		m["checkboxLabel"] = f.Label
		return m, nil
	case schemas.FieldTypeSwitch:
		m := base(false) // placeholder popped for switch
		m["type"] = "switch"
		m["id"] = f.ID
		m["switchLabel"] = f.Label
		return m, nil
	case schemas.FieldTypeDate:
		m := base(true)
		m["type"] = "date"
		m["id"] = f.ID
		return m, nil
	default:
		return nil, fmt.Errorf("unsupported AskUserFormField type: %s", f.Type)
	}
}

// extractValuesFromRaw finds the submitted form values inside an approval
// response payload. Ports ask_user.py::_extract_values_from_raw: prefer
// raw["values"], then raw["response"]["values"].
func extractValuesFromRaw(raw map[string]any) map[string]any {
	out := map[string]any{}
	if raw == nil {
		return out
	}
	if direct, ok := raw["values"].(map[string]any); ok {
		return copyAnyMap(direct)
	}
	if respObj, ok := raw["response"].(map[string]any); ok {
		if inner, ok := respObj["values"].(map[string]any); ok {
			return copyAnyMap(inner)
		}
	}
	return out
}

func copyAnyMap(in map[string]any) map[string]any {
	out := make(map[string]any, len(in))
	for k, v := range in {
		out[k] = v
	}
	return out
}

// parseApprovalResult converts an approval decision + feedback + raw payload
// into an AskUserResponse using the exact table from
// ask_user.py::_parse_approval_result_to_response:
//
//	approved / request_changes / submitted -> submitted
//	rejected                               -> cancelled
//	expired                                -> timeout
//	error                                  -> error
//	anything else                          -> error (defensive)
//
// Values come from raw["values"] or raw["response"]["values"], falling back to
// parsing feedback as JSON.
func parseApprovalResult(decision string, feedback *string, raw map[string]any) schemas.AskUserResponse {
	switch decision {
	case "rejected":
		return schemas.AskUserResponse{Status: "cancelled", Values: map[string]any{}, Feedback: feedback}
	case "expired":
		return schemas.AskUserResponse{Status: "timeout", Values: map[string]any{}, Feedback: feedback}
	case "error":
		errMsg := "agentfield reported decision=error"
		if feedback != nil {
			errMsg = *feedback
		}
		return schemas.AskUserResponse{Status: "error", Values: map[string]any{}, Feedback: feedback, Error: &errMsg}
	}

	values := extractValuesFromRaw(raw)
	if len(values) == 0 && feedback != nil {
		var parsed map[string]any
		if err := json.Unmarshal([]byte(*feedback), &parsed); err == nil && parsed != nil {
			values = parsed
		}
	}

	switch decision {
	case "approved", "request_changes", "submitted":
		return schemas.AskUserResponse{Status: "submitted", Values: values, Feedback: feedback}
	default:
		errMsg := fmt.Sprintf("unknown decision: %q", decision)
		return schemas.AskUserResponse{Status: "error", Values: values, Feedback: feedback, Error: &errMsg}
	}
}

// RequestUserInputParams carries the pause context for RequestUserInputAndPause.
type RequestUserInputParams struct {
	NodeID         string
	ExecutionID    string
	ExpiresInHours float64 // default 24 when <= 0
	UserID         string
	WebhookURL     string
	Metadata       map[string]any
}

// RequestUserInputAndPause builds a hax form, pauses the execution via
// agent.Pause (webhook-resumed — the execution transitions to "waiting" and
// blocks until the approval callback resolves it), and maps the outcome to an
// AskUserResponse. Ports ask_user.py::request_user_input_and_pause onto the
// agent.Pause primitive (design §4.6).
//
// The workflow is genuinely suspended on the control plane while the form is
// outstanding; expiry is enforced server-side per ExpiresInHours (surfacing as
// a decision="expired" -> status="timeout"). A create failure or a pause error
// degrades to a typed status rather than raising.
func RequestUserInputAndPause(
	ctx context.Context,
	app App,
	pauser Pauser,
	hax *HaxClient,
	spec schemas.AskUserForm,
	p RequestUserInputParams,
) schemas.AskUserResponse {
	expiresHours := p.ExpiresInHours
	if expiresHours <= 0 {
		expiresHours = 24
	}

	payload, err := BuildHaxFormPayload(spec)
	if err != nil {
		noteSafe(ctx, app, fmt.Sprintf("ask_user: failed to build form from spec: %v", err),
			"ask_user", "form_builder", "error")
		msg := fmt.Sprintf("Failed to build form from spec: %v", err)
		return schemas.AskUserResponse{Status: "error", Values: map[string]any{}, Error: &msg}
	}

	noteSafe(ctx, app, fmt.Sprintf("ask_user: submitting hax form-builder request (%q)", spec.Title),
		"ask_user", "hax", "create_form_request")

	created, err := hax.CreateRequest(ctx, CreateRequestParams{
		Type:             "form-builder",
		Payload:          payload,
		Title:            spec.Title,
		Description:      spec.Description,
		ExpiresInSeconds: int(expiresHours * 3600),
		UserID:           p.UserID,
		WebhookURL:       p.WebhookURL,
		Metadata:         p.Metadata,
	})
	if err != nil {
		noteSafe(ctx, app, fmt.Sprintf("ask_user: hax create_request failed: %v", err),
			"ask_user", "hax", "error")
		msg := fmt.Sprintf("create_form_request failed: %v", err)
		return schemas.AskUserResponse{Status: "error", Values: map[string]any{}, Error: &msg}
	}
	noteSafe(ctx, app, fmt.Sprintf("ask_user: hax form request created (request_id=%s)", created.ID),
		"ask_user", "hax", "submitted")

	// Pause the execution for approval. agent.Pause transitions it to "waiting"
	// on the control plane and blocks until the /webhooks/approval callback
	// resolves it (or it expires) — no polling. The callback URL is derived by
	// the SDK from the agent's public URL, so unlike the old poll flow we pass
	// no CallbackURL here (mirrors Python passing only approval_request_id /
	// approval_request_url / expires_in_hours to app.pause; execution_id is
	// forwarded when known).
	result, err := pauser.Pause(ctx, agent.PauseOptions{
		ApprovalRequestID:  created.ID,
		ApprovalRequestURL: created.URL,
		ExpiresInHours:     int(expiresHours),
		ExecutionID:        p.ExecutionID,
	})
	if err != nil {
		// A deadline/cancel is treated as the pause timing out; any other
		// failure surfaces as an error (mirrors Python's TimeoutError vs
		// generic-exception split around app.pause). A plain expiry is NOT an
		// error: the SDK returns decision="expired", handled below via
		// parseApprovalResult -> status="timeout".
		if errors.Is(err, context.DeadlineExceeded) {
			noteSafe(ctx, app, "ask_user: pause expired without human response",
				"ask_user", "pause", "timeout")
			return schemas.AskUserResponse{Status: "timeout", Values: map[string]any{}}
		}
		noteSafe(ctx, app, fmt.Sprintf("ask_user: pause raised: %v", err),
			"ask_user", "pause", "error")
		msg := fmt.Sprintf("pause failed: %v", err)
		return schemas.AskUserResponse{Status: "error", Values: map[string]any{}, Error: &msg}
	}

	// feedback = approval_result.feedback or None (empty collapses to nil, as in
	// the Python _parse_approval_result_to_response).
	var feedback *string
	if fb := result.Feedback; fb != "" {
		feedback = &fb
	}
	resp := parseApprovalResult(result.Decision, feedback, result.RawResponse)
	noteSafe(ctx, app, fmt.Sprintf("ask_user: response received (status=%s, %d value(s))", resp.Status, len(resp.Values)),
		"ask_user", "hax", "response", resp.Status)
	return resp
}

// FormatPriorUserResponses renders prior_user_responses as a markdown block for
// the LLM prompt. Ports ask_user.py::format_prior_user_responses verbatim so a
// re-invoked reasoner surfaces already-answered questions and does not re-ask.
func FormatPriorUserResponses(prior []map[string]any) string {
	if len(prior) == 0 {
		return ""
	}
	lines := []string{"## Prior Clarification From User", ""}
	for idx, entry := range prior {
		question := stringOr(entry["question"], "(no title)")
		status := stringOr(entry["status"], "unknown")
		lines = append(lines, fmt.Sprintf("### Question %d: %s", idx+1, question))
		lines = append(lines, fmt.Sprintf("_Status: %s_", status))
		values, _ := entry["values"].(map[string]any)
		if len(values) > 0 {
			lines = append(lines, "")
			lines = append(lines, "Values submitted by user:")
			for _, k := range sortedKeys(values) {
				lines = append(lines, fmt.Sprintf("- **%s**: %v", k, values[k]))
			}
		}
		if fb, ok := entry["feedback"].(string); ok && fb != "" {
			lines = append(lines, "")
			lines = append(lines, fmt.Sprintf("User feedback: %s", fb))
		}
		lines = append(lines, "")
	}
	lines = append(lines,
		"USE THESE PRIOR ANSWERS. DO NOT RE-ASK THE SAME QUESTIONS. Only "+
			"emit `ask_user_form` if you need DIFFERENT clarification not already "+
			"covered above.")
	return strings.Join(lines, "\n")
}

func stringOr(v any, fallback string) string {
	if s, ok := v.(string); ok && s != "" {
		return s
	}
	return fallback
}

// sortedKeys returns the keys of m in deterministic (sorted) order — Go maps
// have no stable iteration order, and a rendered prompt block should not vary
// run-to-run.
func sortedKeys(m map[string]any) []string {
	keys := make([]string, 0, len(m))
	for k := range m {
		keys = append(keys, k)
	}
	sort.Strings(keys)
	return keys
}
