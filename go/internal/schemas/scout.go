package schemas

// ScoutResult is the structured output the environment-scout LLM emits. Ported
// from hitl/scout_schema.py::ScoutResult.
//
// scoped_credentials is populated on pass 2 only; ask_user_form is Optional
// (default None) so maps to a pointer.
type ScoutResult struct {
	DetectedServices  []ServiceCredentialSpec `json:"detected_services"`
	ScopedCredentials map[string]string       `json:"scoped_credentials"`
	SkippedServices   []string                `json:"skipped_services"`
	Summary           string                  `json:"summary"`
	AskUserForm       *AskUserForm            `json:"ask_user_form"`
}
