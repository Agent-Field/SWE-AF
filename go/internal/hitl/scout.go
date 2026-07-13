package hitl

import (
	"fmt"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// This file holds the deterministic substrate for run_environment_scout — the
// two-pass, ask-user-driven reasoner described in scout_schema.py:
//
//	Pass 1: scan the repo, emit one optional credential field per detected
//	        service, leave scoped_credentials empty.
//	Pass 2: read the user's submitted values back out of prior_user_responses
//	        and surface them as scoped_credentials.
//
// The LLM orchestration lives in the role reasoner; these helpers keep the
// deterministic pieces (form assembly, value extraction) testable in isolation.

// BuildScoutForm assembles the pass-1 ask-user form for the detected services:
// one optional text field per service, keyed by env_var_name so the submitted
// values map straight onto scoped_credentials. Returns nil when nothing was
// detected (the scout should then return ask_user_form=None and short-circuit).
func BuildScoutForm(detected []schemas.ServiceCredentialSpec) *schemas.AskUserForm {
	if len(detected) == 0 {
		return nil
	}
	fields := make([]schemas.AskUserFormField, 0, len(detected))
	for _, spec := range detected {
		desc := fmt.Sprintf("Mint at %s — %s", spec.MintURL, spec.PermissionsHint)
		fields = append(fields, schemas.AskUserFormField{
			ID:          spec.EnvVarName,
			Type:        schemas.FieldTypeInput,
			Label:       fmt.Sprintf("%s (%s)", spec.ServiceName, spec.EnvVarName),
			Description: &desc,
			Required:    false,
		})
	}
	return &schemas.AskUserForm{
		Title:       "Scoped credentials for this build",
		Fields:      fields,
		SubmitLabel: "Submit",
	}
}

// ScopedCredentialsFromPriorResponses extracts the most recent user-submitted
// values from prior_user_responses and returns them as a string->string
// credential map, dropping any non-string or blank values. This is the pass-2
// projection from scout_schema.py (values -> scoped_credentials).
func ScopedCredentialsFromPriorResponses(prior []map[string]any) map[string]string {
	out := map[string]string{}
	if len(prior) == 0 {
		return out
	}
	last := prior[len(prior)-1]
	values, ok := last["values"].(map[string]any)
	if !ok {
		return out
	}
	for k, v := range values {
		s, ok := v.(string)
		if !ok || s == "" {
			continue
		}
		out[k] = s
	}
	return out
}
