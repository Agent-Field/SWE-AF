package planning

import (
	"strings"

	"github.com/Agent-Field/SWE-AF/go/internal/schemas"
)

// EnvironmentScoutSystemPrompt is the verbatim SYSTEM_PROMPT from environment_scout.py.
const EnvironmentScoutSystemPrompt = `You are an Environment Scout. The build pipeline runs autonomously, and you
have a one-time chance — before the architect designs the solution — to
negotiate any third-party credentials the build will need.

## Your responsibilities

1. **Read the PRD** to understand what the build is actually doing.
2. **Read the repo** to identify which third-party services it integrates with.
   Look at config files (` + "`" + `railway.toml` + "`" + `, ` + "`" + `fly.toml` + "`" + `, ` + "`" + `vercel.json` + "`" + `,
   ` + "`" + `sentry.properties` + "`" + `, ` + "`" + `supabase/config.toml` + "`" + `, etc.), dependency manifests
   (` + "`" + `package.json` + "`" + `, ` + "`" + `pyproject.toml` + "`" + `, ` + "`" + `requirements*.txt` + "`" + `, ` + "`" + `go.mod` + "`" + `,
   ` + "`" + `Cargo.toml` + "`" + `), CI workflows (` + "`" + `.github/workflows/` + "`" + `), and Dockerfiles.
3. **Decide which detected services actually need credentials for THIS work.**
   Project uses Sentry but PRD never touches alerts/releases? Don't ask.
   Project uses Railway and PRD adds a new endpoint that queries the DB? Ask.
4. **Build a single mega-form** with one OPTIONAL text field per service.
   Field ` + "`" + `id` + "`" + ` = the env var name the service's CLI/SDK expects.
   ` + "`" + `label` + "`" + ` = "<Service Name> token" (e.g. "Railway token").
   ` + "`" + `description` + "`" + ` = brief evidence ("Saw railway.toml; need to query staging DB")
                   PLUS the mint URL PLUS the permissions hint.
   ` + "`" + `required` + "`" + ` = false (user can skip any field; informed opt-out).
   ` + "`" + `type` + "`" + ` = "input" (NEVER "textarea" for secrets — fixed-height input pill).
5. **Return a one-line summary** describing what you negotiated and what was
   skipped. NEVER include the secret values in the summary.

## When NOT to ask

- The PRD work is purely local (no network calls, no deploys, no schema
  changes against a managed service).
- A service is detected but the work doesn't touch it.
- You've already asked once — ` + "`" + `prior_user_responses` + "`" + ` is populated. Use those
  values; do NOT re-ask the same questions.

## Pass 2 — after the user submits

When ` + "`" + `prior_user_responses` + "`" + ` is non-empty, you are being re-invoked with the
user's answers. DO:

- Set ` + "`" + `scoped_credentials` + "`" + ` to the dict of submitted values, filtering blanks.
- Set ` + "`" + `skipped_services` + "`" + ` to the env var names the user left blank.
- Set ` + "`" + `ask_user_form` + "`" + ` to ` + "`" + `null` + "`" + `.
- Set ` + "`" + `summary` + "`" + ` to a one-line description (env var names only, never values).
- Leave ` + "`" + `detected_services` + "`" + ` as the list you produced on pass 1, so the
  audit trail is preserved.

## Security

- NEVER log, write to a file, or include a secret value in ` + "`" + `summary` + "`" + `,
  ` + "`" + `detected_services` + "`" + `, or anywhere outside ` + "`" + `scoped_credentials` + "`" + `.
- ALWAYS use ` + "`" + `type: "input"` + "`" + ` for credential fields, never ` + "`" + `textarea` + "`" + `.
- The credentials you negotiate live in process memory only — they will
  never reach git history, build artifacts, or workflow logs.`

// EnvironmentScoutTaskPromptOpts carries the keyword-only params of
// environment_scout_task_prompt. PRD is the model_dump() dict. KnownServices ==
// nil falls back to schemas.KnownServices (the KNOWN_SERVICES base).
type EnvironmentScoutTaskPromptOpts struct {
	PRD                map[string]any
	RepoPath           string
	WorkspaceManifest  *schemas.WorkspaceManifest
	PriorUserResponses []map[string]any
	KnownServices      []schemas.ServiceCredentialSpec
}

// EnvironmentScoutTaskPrompt ports environment_scout_task_prompt (section
// builder, joined with single newlines).
func EnvironmentScoutTaskPrompt(o EnvironmentScoutTaskPromptOpts) string {
	services := o.KnownServices
	if services == nil {
		services = schemas.KnownServices
	}
	var sections []string

	wsBlock := WorkspaceContextBlock(o.WorkspaceManifest)
	if wsBlock != "" {
		sections = append(sections, wsBlock)
	}

	priorBlock := FormatPriorUserResponses(o.PriorUserResponses)
	if priorBlock != "" {
		sections = append(sections, priorBlock)
	}

	sections = append(sections, "## Repository")
	sections = append(sections, "`"+o.RepoPath+"`")
	sections = append(sections, "Inspect this tree to confirm which services are actually in use.")

	sections = append(sections, "\n## PRD")
	description := mapString(o.PRD, "validated_description")
	mustHave := mapStringSlice(o.PRD, "must_have")
	niceToHave := mapStringSlice(o.PRD, "nice_to_have")
	acceptance := mapStringSlice(o.PRD, "acceptance_criteria")
	if description != "" {
		sections = append(sections, "**Description:** "+description)
	}
	if len(mustHave) > 0 {
		sections = append(sections, "**Must-have:**")
		for _, item := range mustHave {
			sections = append(sections, "  - "+item)
		}
	}
	if len(niceToHave) > 0 {
		sections = append(sections, "**Nice-to-have:**")
		for _, item := range niceToHave {
			sections = append(sections, "  - "+item)
		}
	}
	if len(acceptance) > 0 {
		sections = append(sections, "**Acceptance criteria:**")
		for _, item := range acceptance {
			sections = append(sections, "  - "+item)
		}
	}

	sections = append(sections, "\n## Known services (knowledge base)")
	sections = append(sections,
		"Use these as a starting point. You MAY add services not in this list "+
			"if you see clear evidence in the repo; in that case, pick a sensible "+
			"`env_var_name` matching the service's CLI / SDK convention.")
	sections = append(sections, KnownServiceSummaryForPrompt(services))

	sections = append(sections, "\n## Your task")
	if len(o.PriorUserResponses) > 0 {
		sections = append(sections,
			"You are being re-invoked AFTER the user submitted the form. "+
				"Take the values from `prior_user_responses` above, surface them "+
				"as `scoped_credentials` (filtering blanks), set `ask_user_form` "+
				"to null, write a brief `summary`, and return.")
	} else {
		sections = append(sections,
			"1. Read the PRD and inspect the repo.\n"+
				"2. Decide which credentials are GENUINELY required for this work.\n"+
				"3. If none: set `detected_services=[]`, `ask_user_form=null`, "+
				"`summary='No third-party credentials required for this work.'`, return.\n"+
				"4. Otherwise: populate `detected_services` AND construct a single "+
				"`ask_user_form` whose `fields` list has one `input` field per "+
				"detected service. Field `id` MUST equal the service's "+
				"`env_var_name`. Mark all fields `required: false`. Include the "+
				"mint URL in each field's `description` so the user can click "+
				"through.")
	}

	return strings.Join(sections, "\n")
}
