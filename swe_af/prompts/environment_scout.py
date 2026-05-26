"""Prompt builder for the Environment Scout reasoner.

The scout runs after the Product Manager and before the Architect. Its job
is to look at the PRD and the repo, figure out which third-party services
the build will need credentials for, and ask the user — in a single mega-
form — for scoped/temporary tokens BEFORE downstream phases start running.

The scout is parsimonious: it only asks for credentials whose absence would
actually block PRD execution. "Project uses Sentry" is not enough; "PRD
requires adding a new Sentry alerting rule" is.
"""

from __future__ import annotations

from swe_af.execution.schemas import WorkspaceManifest
from swe_af.hitl.ask_user import format_prior_user_responses
from swe_af.hitl.services import (
    KNOWN_SERVICES,
    ServiceCredentialSpec,
    known_service_summary_for_prompt,
)
from swe_af.prompts._utils import workspace_context_block

SYSTEM_PROMPT = """\
You are an Environment Scout. The build pipeline runs autonomously, and you
have a one-time chance — before the architect designs the solution — to
negotiate any third-party credentials the build will need.

## Your responsibilities

1. **Read the PRD** to understand what the build is actually doing.
2. **Read the repo** to identify which third-party services it integrates with.
   Look at config files (`railway.toml`, `fly.toml`, `vercel.json`,
   `sentry.properties`, `supabase/config.toml`, etc.), dependency manifests
   (`package.json`, `pyproject.toml`, `requirements*.txt`, `go.mod`,
   `Cargo.toml`), CI workflows (`.github/workflows/`), and Dockerfiles.
3. **Decide which detected services actually need credentials for THIS work.**
   Project uses Sentry but PRD never touches alerts/releases? Don't ask.
   Project uses Railway and PRD adds a new endpoint that queries the DB? Ask.
4. **Build a single mega-form** with one OPTIONAL text field per service.
   Field `id` = the env var name the service's CLI/SDK expects.
   `label` = "<Service Name> token" (e.g. "Railway token").
   `description` = brief evidence ("Saw railway.toml; need to query staging DB")
                   PLUS the mint URL PLUS the permissions hint.
   `required` = false (user can skip any field; informed opt-out).
   `type` = "input" (NEVER "textarea" for secrets — fixed-height input pill).
5. **Return a one-line summary** describing what you negotiated and what was
   skipped. NEVER include the secret values in the summary.

## When NOT to ask

- The PRD work is purely local (no network calls, no deploys, no schema
  changes against a managed service).
- A service is detected but the work doesn't touch it.
- You've already asked once — `prior_user_responses` is populated. Use those
  values; do NOT re-ask the same questions.

## Pass 2 — after the user submits

When `prior_user_responses` is non-empty, you are being re-invoked with the
user's answers. DO:

- Set `scoped_credentials` to the dict of submitted values, filtering blanks.
- Set `skipped_services` to the env var names the user left blank.
- Set `ask_user_form` to `null`.
- Set `summary` to a one-line description (env var names only, never values).
- Leave `detected_services` as the list you produced on pass 1, so the
  audit trail is preserved.

## Security

- NEVER log, write to a file, or include a secret value in `summary`,
  `detected_services`, or anywhere outside `scoped_credentials`.
- ALWAYS use `type: "input"` for credential fields, never `textarea`.
- The credentials you negotiate live in process memory only — they will
  never reach git history, build artifacts, or workflow logs.\
"""


def environment_scout_task_prompt(
    *,
    prd: dict,
    repo_path: str,
    workspace_manifest: WorkspaceManifest | None = None,
    prior_user_responses: list[dict] | None = None,
    known_services: list[ServiceCredentialSpec] | None = None,
) -> str:
    """Build the per-call task prompt the scout receives."""
    services = known_services or KNOWN_SERVICES
    sections: list[str] = []

    ws_block = workspace_context_block(workspace_manifest)
    if ws_block:
        sections.append(ws_block)

    prior_block = format_prior_user_responses(prior_user_responses)
    if prior_block:
        sections.append(prior_block)

    sections.append("## Repository")
    sections.append(f"`{repo_path}`")
    sections.append("Inspect this tree to confirm which services are actually in use.")

    sections.append("\n## PRD")
    description = prd.get("validated_description", "") or ""
    must_have = prd.get("must_have", []) or []
    nice_to_have = prd.get("nice_to_have", []) or []
    acceptance = prd.get("acceptance_criteria", []) or []
    if description:
        sections.append(f"**Description:** {description}")
    if must_have:
        sections.append("**Must-have:**")
        sections.extend(f"  - {item}" for item in must_have)
    if nice_to_have:
        sections.append("**Nice-to-have:**")
        sections.extend(f"  - {item}" for item in nice_to_have)
    if acceptance:
        sections.append("**Acceptance criteria:**")
        sections.extend(f"  - {item}" for item in acceptance)

    sections.append("\n## Known services (knowledge base)")
    sections.append(
        "Use these as a starting point. You MAY add services not in this list "
        "if you see clear evidence in the repo; in that case, pick a sensible "
        "`env_var_name` matching the service's CLI / SDK convention."
    )
    sections.append(known_service_summary_for_prompt(services))

    sections.append("\n## Your task")
    if prior_user_responses:
        sections.append(
            "You are being re-invoked AFTER the user submitted the form. "
            "Take the values from `prior_user_responses` above, surface them "
            "as `scoped_credentials` (filtering blanks), set `ask_user_form` "
            "to null, write a brief `summary`, and return."
        )
    else:
        sections.append(
            "1. Read the PRD and inspect the repo.\n"
            "2. Decide which credentials are GENUINELY required for this work.\n"
            "3. If none: set `detected_services=[]`, `ask_user_form=null`, "
            "`summary='No third-party credentials required for this work.'`, return.\n"
            "4. Otherwise: populate `detected_services` AND construct a single "
            "`ask_user_form` whose `fields` list has one `input` field per "
            "detected service. Field `id` MUST equal the service's "
            "`env_var_name`. Mark all fields `required: false`. Include the "
            "mint URL in each field's `description` so the user can click "
            "through."
        )

    return "\n".join(sections)
