package schemas

// ServiceCredentialSpec is one row in the scout's service knowledge base; also
// returned by the scout. Ported from hitl/services.py::ServiceCredentialSpec.
type ServiceCredentialSpec struct {
	ServiceName      string   `json:"service_name"`
	EnvVarName       string   `json:"env_var_name"`
	MintURL          string   `json:"mint_url"`
	PermissionsHint  string   `json:"permissions_hint"`
	SignalFiles      []string `json:"signal_files"`
	EvidenceTemplate string   `json:"evidence_template"`
}

// KnownServices is the KNOWN_SERVICES knowledge base from
// hitl/services.py::KNOWN_SERVICES — the 9 entries, verbatim.
var KnownServices = []ServiceCredentialSpec{
	{
		ServiceName:      "Railway",
		EnvVarName:       "RAILWAY_TOKEN",
		MintURL:          "https://railway.com/account/tokens",
		PermissionsHint:  "Project token, read-only if possible, set expiry to 1 day.",
		SignalFiles:      []string{"railway.toml", "railway.json", ".railway/config.json"},
		EvidenceTemplate: "Saw {signal} — build likely needs Railway access to deploy or query services.",
	},
	{
		ServiceName:      "Fly.io",
		EnvVarName:       "FLY_API_TOKEN",
		MintURL:          "https://fly.io/user/personal_access_tokens",
		PermissionsHint:  "Deploy token scoped to this app, 1-day expiry.",
		SignalFiles:      []string{"fly.toml", "fly.io.toml", ".fly/config.toml"},
		EvidenceTemplate: "Saw {signal} — build may need Fly.io access for deploys.",
	},
	{
		ServiceName:      "Vercel",
		EnvVarName:       "VERCEL_TOKEN",
		MintURL:          "https://vercel.com/account/tokens",
		PermissionsHint:  "Scope to this team only, 1-day expiry.",
		SignalFiles:      []string{"vercel.json", ".vercel/project.json"},
		EvidenceTemplate: "Saw {signal} — build may need Vercel access.",
	},
	{
		ServiceName:      "Supabase",
		EnvVarName:       "SUPABASE_ACCESS_TOKEN",
		MintURL:          "https://supabase.com/dashboard/account/tokens",
		PermissionsHint:  "Personal access token, 1-day expiry — required only if migrations or schema changes are part of the work.",
		SignalFiles:      []string{"supabase/config.toml", "supabase/.gitignore", "supabase/migrations"},
		EvidenceTemplate: "Saw {signal} — Supabase project detected.",
	},
	{
		ServiceName:      "Sentry",
		EnvVarName:       "SENTRY_AUTH_TOKEN",
		MintURL:          "https://sentry.io/settings/account/api/auth-tokens/",
		PermissionsHint:  "Auth token scoped to project:read + project:releases, 1-day expiry.",
		SignalFiles:      []string{"sentry.properties", ".sentryclirc", "sentry.io.json"},
		EvidenceTemplate: "Saw {signal} — Sentry integration detected.",
	},
	{
		ServiceName:      "Datadog",
		EnvVarName:       "DATADOG_API_KEY",
		MintURL:          "https://app.datadoghq.com/organization-settings/api-keys",
		PermissionsHint:  "Application API key (NOT a client token), restricted to read scopes if possible.",
		SignalFiles:      []string{"datadog.yaml", ".datadog/conf.yaml"},
		EvidenceTemplate: "Saw {signal} — Datadog integration detected.",
	},
	{
		ServiceName:      "GitHub",
		EnvVarName:       "GH_TOKEN",
		MintURL:          "https://github.com/settings/personal-access-tokens/new",
		PermissionsHint:  "Fine-grained PAT scoped to THIS repo only, repo:contents+pull-requests, 1-day expiry.",
		SignalFiles:      []string{".github/workflows", "CODEOWNERS"},
		EvidenceTemplate: "Saw {signal} — work likely needs GitHub API beyond what gh CLI provides anonymously.",
	},
	{
		ServiceName:      "OpenAI",
		EnvVarName:       "OPENAI_API_KEY",
		MintURL:          "https://platform.openai.com/api-keys",
		PermissionsHint:  "Restricted API key with low usage cap; 1-day expiry.",
		SignalFiles:      []string{}, // Detected via dependency manifests, not signal files.
		EvidenceTemplate: "Project depends on the OpenAI SDK.",
	},
	{
		ServiceName:      "Anthropic",
		EnvVarName:       "ANTHROPIC_API_KEY",
		MintURL:          "https://console.anthropic.com/settings/keys",
		PermissionsHint:  "Restricted API key, set monthly spend limit, 1-day expiry.",
		SignalFiles:      []string{},
		EvidenceTemplate: "Project depends on the Anthropic SDK.",
	},
}
