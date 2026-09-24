"""Internal reasoners for the SWE planning pipeline.

Each reasoner wraps a single agent role (PM, Architect, Tech Lead, Sprint Planner)
and uses router.harness() for actual AI execution. The @router.reasoner() decorator provides
FastAPI endpoints, workflow DAG tracking, and observability via router.note().
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, quote_plus

from pydantic import BaseModel, ValidationError

from swe_af.execution.fatal_error import (
    check_empty_harness_completion,
    check_fatal_harness_error,
)
from swe_af.execution.schemas import DEFAULT_AGENT_MAX_TURNS
from swe_af.reasoners.schemas import (
    Architecture,
    PlannedIssue,
    PRD,
    ReviewResult,
)
from swe_af.runtime.providers import runtime_to_harness_adapter
from swe_af.surface import internal_role

from . import router


# ---------------------------------------------------------------------------
# Pure helpers (NOT reasoners)
# ---------------------------------------------------------------------------


def _ensure_paths(base: str) -> dict[str, str]:
    """Create artifact directories under *base* and return a path map."""
    paths = {
        "base": base,
        "logs": os.path.join(base, "logs"),
        "plan": os.path.join(base, "plan"),
        "issues": os.path.join(base, "plan", "issues"),
        "prd": os.path.join(base, "plan", "prd.md"),
        "architecture": os.path.join(base, "plan", "architecture.md"),
        "review": os.path.join(base, "plan", "review.md"),
        "rationale": os.path.join(base, "rationale.md"),
    }
    for d in ("logs", "plan", "issues"):
        Path(paths[d]).mkdir(parents=True, exist_ok=True)
    return paths


def _compute_levels(issues: list[dict]) -> list[list[str]]:
    """Topological sort of issues into parallel execution levels (Kahn's algorithm).

    Accepts a list of issue dicts (each must have ``name`` and ``depends_on`` keys).
    Returns a list of levels where each level is a list of issue names that can
    execute concurrently (all their dependencies are in prior levels).

    Raises ValueError on dependency cycles.
    """
    name_set = {i["name"] for i in issues}
    in_degree: dict[str, int] = {i["name"]: 0 for i in issues}
    dependents: dict[str, list[str]] = defaultdict(list)

    for issue in issues:
        for dep in issue.get("depends_on", []):
            if dep in name_set:
                in_degree[issue["name"]] += 1
                dependents[dep].append(issue["name"])

    queue: deque[str] = deque(n for n, d in in_degree.items() if d == 0)
    levels: list[list[str]] = []
    processed = 0

    while queue:
        level = list(queue)
        levels.append(level)
        processed += len(level)
        queue.clear()
        for name in level:
            for dep_name in dependents[name]:
                in_degree[dep_name] -= 1
                if in_degree[dep_name] == 0:
                    queue.append(dep_name)

    if processed != len(issues):
        cycle_nodes = [n for n, d in in_degree.items() if d > 0]
        raise ValueError(f"Dependency cycle detected among issues: {cycle_nodes}")

    return levels


def _validate_file_conflicts(issues: list[dict], levels: list[list[str]]) -> list[dict]:
    """Detect file conflicts between issues scheduled at the same parallel level.

    For each level, collects ``files_to_modify`` and ``files_to_create`` across
    all issues in that level.  If any file appears in more than one issue at the
    same level, it is reported as a conflict (parallel agents would overwrite
    each other).

    Returns a list of conflict dicts, e.g.::

        [{"level": 0, "file": "src/ops.rs", "issues": ["arithmetic-ops", "logical-ops"]}]

    An empty list means no conflicts were detected.
    """
    issue_by_name: dict[str, dict] = {i["name"]: i for i in issues}
    conflicts: list[dict] = []

    for level_idx, level_names in enumerate(levels):
        file_to_issues: dict[str, list[str]] = defaultdict(list)
        for name in level_names:
            issue = issue_by_name.get(name)
            if issue is None:
                continue
            for f in issue.get("files_to_create", []):
                file_to_issues[f].append(name)
            for f in issue.get("files_to_modify", []):
                file_to_issues[f].append(name)

        for filepath, touching_issues in file_to_issues.items():
            if len(touching_issues) > 1:
                conflicts.append(
                    {
                        "level": level_idx,
                        "file": filepath,
                        "issues": touching_issues,
                    }
                )

    return conflicts


def _assign_sequence_numbers(issues: list[dict], levels: list[list[str]]) -> list[dict]:
    """Assign 1-based sequential numbers based on topo-sorted level order.

    Numbers are assigned by flattening levels in order. Within each level,
    the sprint planner's original ordering is preserved. The ``sequence_number``
    is used only for display/file naming — ``name`` remains the canonical ID.
    """
    issue_by_name = {i["name"]: i for i in issues}
    counter = 1
    for level_names in levels:
        level_set = set(level_names)
        # Preserve sprint planner's ordering within each level
        for issue in issues:
            if issue["name"] in level_set:
                issue_by_name[issue["name"]]["sequence_number"] = counter
                counter += 1
    return list(issue_by_name.values())


# Per-stage bounds on the extra *outer* schema-bound harness calls a planning
# role gets when its structured output does not parse or validate. These count
# router.harness() calls, not model runs: the SDK already retries schema
# failures inside one harness() call (DEFAULT_SCHEMA_RETRIES = 2), so a single
# outer attempt can itself be up to three subprocess runs. The outer bounds
# re-issue the whole call with the validation error fed back into the task
# prompt (issue #146). Keep them small — each attempt is a full stage run over
# the PRD and architecture, and the architect's architecture object is by far
# the largest response in the pipeline. The bounds are internal constants, not
# user-facing knobs: nothing has ever passed a different value.
PLANNING_ROLE_SCHEMA_RETRIES = 1
SPRINT_PLANNER_SCHEMA_RETRIES = 2

# Cap on how much of one failed attempt's raw completion is written to the
# retry log. The first and last halves are kept — output-limit truncation is
# visible at the tail, malformed-JSON evidence usually at the head — and the
# middle is elided, so the default three attempts cannot grow the log without
# bound.
_MAX_RAW_RESPONSE_CHARS = 200_000

# Makes each retry-log run section unique even when two invocations share a run
# id and start within the same wall-clock second: the header's timestamp has
# second granularity, so the counter is what distinguishes them.
_RUN_SECTION_SEQ = itertools.count(1)


def _raw_completion_text(result) -> str:
    """Best-effort raw completion text from a HarnessResult-like object."""
    raw = getattr(result, "result", None)
    if not raw:
        raw = getattr(result, "text", None)
    return raw or ""


def _planning_run_id() -> str:
    """Identifier of the build this planning call belongs to.

    Used for the retry log's per-run header (so a log appended to across builds
    stays self-describing) and to find the credentials the scout negotiated for
    this run. Falls back to a stable placeholder when no context is attached
    (tests, direct invocation).
    """
    ctx = getattr(router, "ctx", None)
    run_id = (
        getattr(ctx, "run_id", None)
        or getattr(ctx, "root_workflow_id", None)
        or ""
    )
    return str(run_id) if run_id else "unknown-run"


def _credential_forms(value: str) -> list[str]:
    """Bounded spellings a credential value can take in a response.

    The log stores the model's raw completion text, so a credential can appear
    in the exact form the scout negotiated, JSON-escaped (a value containing
    ``"`` or ``\\`` is written escaped inside JSON), or URL-encoded when it is
    part of a URL. Redacting only the exact value misses those spellings, so
    each bounded encoding the log path can contain is included. The original
    value comes first; callers replace longest-first so a shorter spelling
    cannot split a longer one.
    """
    forms = [value]
    try:
        encoded_forms = (
            json.dumps(value, ensure_ascii=False)[1:-1],
            quote(value, safe=""),
            quote_plus(value, safe=""),
        )
    except (TypeError, ValueError, UnicodeError):
        # Diagnostics must never fail the stage (or mask a real error further
        # up): a pathological value that cannot be encoded still gets exact
        # matching.
        return forms
    for encoded in encoded_forms:
        if encoded != value and encoded not in forms:
            forms.append(encoded)
    return forms


def _redact_scoped_credentials(text: str) -> str:
    """Replace any run-scoped credential value with a marker.

    The harness subprocess inherits the scout's scoped credentials, so a
    response that echoes one can otherwise land in the archived retry log. A
    value is matched exactly, JSON-escaped, and URL-encoded (see
    :func:`_credential_forms`). Spellings are replaced longest-first so a
    shorter one cannot split a longer one.
    """
    if not text:
        return text
    try:
        from swe_af.hitl.credentials_store import get_scoped_credentials  # noqa: PLC0415
    except Exception:  # pragma: no cover - diagnostics must never fail a stage
        return text
    try:
        creds = get_scoped_credentials(_planning_run_id())
    except Exception:  # pragma: no cover - diagnostics must never fail a stage
        return text
    variants = [
        (form, name)
        for name, value in creds.items()
        if value
        for form in _credential_forms(value)
    ]
    for form, name in sorted(variants, key=lambda item: len(item[0]), reverse=True):
        text = text.replace(form, f"[REDACTED:{name}]")
    return text


def _flatten_reason(reason: str) -> str:
    """Collapse a multi-line failure reason onto one physical log line.

    The retry log is read by its terminal ``===== outcome: ... =====`` line, so
    a newline inside an interpolated error would push the outcome text onto
    continuation lines and hide it. Every attempt header and outcome goes
    through this before it is written.
    """
    return " ".join((reason or "").split())


def _describe_schema_failure(result, schema) -> str:
    """Describe a schema-bound harness call that produced no parsed result.

    Re-validates the raw completion against *schema* when it is available so
    the message names the parser error or the failing fields. Falls back to the
    harness's own ``error_message`` (the SDK's terminal schema-failure path
    carries its diagnosis there) and then to a generic description.
    """
    raw = _raw_completion_text(result)
    detail = (getattr(result, "error_message", "") or "").strip()
    if raw.strip():
        try:
            data = json.loads(raw)
        except (ValueError, TypeError) as exc:
            parse_error = f"raw response is not valid JSON ({exc})"
        else:
            try:
                schema.model_validate(data)
            except ValidationError as exc:
                fields = "; ".join(
                    f"{'.'.join(str(part) for part in err['loc']) or '<root>'}: "
                    f"{err['msg']}"
                    for err in exc.errors()[:10]
                )
                parse_error = f"raw response failed schema validation ({fields})"
            except Exception as exc:  # defensive: surface any validator error
                parse_error = f"raw response failed schema validation ({exc})"
            else:
                parse_error = "the harness returned no parsed result"
        if detail:
            return f"{parse_error}; harness reported: {detail}"
        return parse_error
    if detail:
        return detail
    return "the harness returned no parsed result and no error detail"


def _truncate_raw_response(raw: str) -> str:
    """Keep the head and tail of an oversized raw response."""
    if len(raw) <= _MAX_RAW_RESPONSE_CHARS:
        return raw
    half = _MAX_RAW_RESPONSE_CHARS // 2
    omitted = len(raw) - _MAX_RAW_RESPONSE_CHARS
    return (
        f"{raw[:half]}\n\n"
        f"# ... {omitted} characters omitted (full response was {len(raw)} chars)"
        f" ...\n\n{raw[-half:]}"
    )


def _append_artifact(path: str, text: str) -> None:
    """Append *text* to a run artifact, creating its parent directory."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(text if text.endswith("\n") else f"{text}\n")


def _persist_raw_response(
    path: str, result, *, attempt: int, attempts: int, error: str
) -> None:
    """Append one failed attempt's raw completion to the stage's retry log."""
    raw = _raw_completion_text(result)
    redacted = _redact_scoped_credentials(raw)
    body = (
        _truncate_raw_response(redacted)
        if raw.strip()
        else "(the harness returned no raw completion text)"
    )
    # Redact while the reason is still multi-line (so a value containing a
    # newline is matched), then flatten so the attempt header stays one line.
    header = _flatten_reason(
        _redact_scoped_credentials(
            f"===== attempt {attempt}/{attempts} failed: {error} ====="
        )
    )
    block = f"{header}\n# raw completion text follows\n{body}"
    _append_artifact(path, _redact_scoped_credentials(block))


def _record_retry_outcome(path: str, outcome: str) -> None:
    """Append the terminal retry outcome to the stage's retry log.

    Redaction runs before flattening so a credential containing a newline is
    still matched, and the outcome always lands on one physical line.
    """
    line = _redact_scoped_credentials(f"===== outcome: {outcome} =====")
    _append_artifact(path, _flatten_reason(line))


def _record_outcome_best_effort(stage: str, path: str, outcome: str) -> None:
    """Record a terminal retry outcome without ever failing the stage."""
    try:
        _record_retry_outcome(path, outcome)
    except Exception as exc:  # diagnostics must never mask the real outcome
        router.note(
            f"{stage} could not write the retry outcome to {path}: {exc}",
            tags=["planning", "schema_retry", "artifact_error"],
        )


def _record_run_header_best_effort(stage: str, path: str) -> None:
    """Start a self-describing section for this invocation of the retry log.

    The log is append-only and keyed by repo path, so a second build against
    the same path appends after the first. The header makes each build's
    section identifiable without a reader having to guess which outcome is
    current, and its section counter keeps two invocations with the same run id
    in the same second distinct.
    """
    try:
        _append_artifact(
            path,
            f"===== run {_planning_run_id()} | {stage} | started "
            f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} | "
            f"section {next(_RUN_SECTION_SEQ)} =====",
        )
    except Exception as exc:  # diagnostics must never fail the stage
        router.note(
            f"{stage} could not write the retry-log header to {path}: {exc}",
            tags=["planning", "schema_retry", "artifact_error"],
        )


def _schema_retry_context(error: str) -> str:
    """Prompt suffix feeding a schema failure back into a retry attempt."""
    return (
        "## Retry Context\n"
        "Your previous response could not be parsed into the required "
        "structured output. The validation error was:\n\n"
        f"{error}\n\n"
        "Produce the complete structured output again, correcting that error. "
        "Include every required field."
    )


async def _run_planning_call_with_schema_retries(
    invoke,
    *,
    stage: str,
    failure_label: str,
    provider: str,
    model: str,
    schema,
    raw_response_path: str,
    max_schema_retries: int,
):
    """Run one schema-bound planning call, retrying parse/validation failures.

    ``invoke`` is an async callable that takes the previous validation error
    (``None`` on the first attempt) and returns a HarnessResult-like object.
    Fatal API errors and empty completions fail immediately; only a response
    that was produced but did not parse/validate is retried, up to
    *max_schema_retries* extra attempts (callers pass the per-stage
    ``PLANNING_ROLE_SCHEMA_RETRIES`` / ``SPRINT_PLANNER_SCHEMA_RETRIES``
    constants). Each invocation starts a self-describing run section, every
    failed attempt is appended, and the section always ends with a terminal
    outcome line, including when a fatal API error or an empty completion ends
    the loop early, so the log never trails off mid-sequence. A failure to
    write any of that is noted but never replaces the schema failure. Run-scoped
    credential values are redacted before anything reaches the log.
    """
    attempts = max(0, max_schema_retries) + 1
    last_error = ""
    persistence_error = ""
    _record_run_header_best_effort(stage, raw_response_path)
    for attempt in range(1, attempts + 1):
        try:
            result = await invoke(last_error or None)
            check_fatal_harness_error(result)
            check_empty_harness_completion(
                result, role=stage, provider=provider, model=model
            )
        except (Exception, asyncio.CancelledError) as exc:
            # A fatal API error or an empty completion can arrive on any
            # attempt, including a retry. Record a terminal outcome before
            # re-raising so the log never ends mid-sequence.
            _record_outcome_best_effort(
                stage,
                raw_response_path,
                f"FAILED after attempt {attempt}/{attempts}: {exc}",
            )
            raise
        if result.parsed is not None:
            _record_outcome_best_effort(
                stage,
                raw_response_path,
                f"succeeded on attempt {attempt}/{attempts}",
            )
            return result

        last_error = _describe_schema_failure(result, schema)
        try:
            _persist_raw_response(
                raw_response_path,
                result,
                attempt=attempt,
                attempts=attempts,
                error=last_error,
            )
        except Exception as exc:  # diagnostics must never mask the real failure
            persistence_error = str(exc)
            router.note(
                f"{stage} could not write the raw response to "
                f"{raw_response_path}: {exc}",
                tags=["planning", "schema_retry", "artifact_error"],
            )
        if attempt < attempts:
            router.note(
                f"{stage} structured output invalid on attempt "
                f"{attempt}/{attempts} — retrying with the validation error",
                tags=["planning", "schema_retry"],
            )

    _record_outcome_best_effort(
        stage,
        raw_response_path,
        f"FAILED after {attempts} attempt(s): {last_error}",
    )
    detail = (
        f"; raw response could not be written: {persistence_error}"
        if persistence_error
        else ""
    )
    raise RuntimeError(
        f"{failure_label} after {attempts} attempt(s) "
        f"(provider={provider}, model={model}; "
        f"raw response: {raw_response_path}) — "
        f"{_redact_scoped_credentials(last_error)}{detail}"
    )


# ---------------------------------------------------------------------------
# Reasoners
# ---------------------------------------------------------------------------


@internal_role(router, "planning")
async def run_product_manager(
    goal: str,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    additional_context: str = "",
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
    prior_user_responses: list[dict] | None = None,
) -> dict:
    """Run the product manager agent to scope a goal into a PRD."""
    router.note("PM starting", tags=["pm", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)

    from swe_af.prompts.product_manager import product_manager_prompts, pm_task_prompt  # noqa: PLC0415
    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415

    system_prompt, _ = product_manager_prompts(
        goal=goal,
        repo_path=repo_path,
        prd_path=paths["prd"],
        additional_context=additional_context,
        prior_user_responses=prior_user_responses,
    )
    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    provider = runtime_to_harness_adapter(ai_provider)

    from swe_af.hitl import (  # noqa: PLC0415
        AskUserBudget,
        approval_webhook_url,
        build_hax_client_from_env,
        run_with_ask_user,
    )

    async def _invoke_pm(prior_user_responses: list[dict] | None) -> PRD | None:
        task_prompt = pm_task_prompt(
            goal=goal,
            repo_path=repo_path,
            prd_path=paths["prd"],
            additional_context=additional_context,
            workspace_manifest=ws_manifest,
            prior_user_responses=prior_user_responses,
        )

        async def _invoke(previous_error: str | None):
            prompt = task_prompt
            if previous_error:
                prompt = f"{task_prompt}\n\n{_schema_retry_context(previous_error)}"
            return await router.harness(
                prompt=prompt,
                schema=PRD,
                provider=provider,
                model=model,
                max_turns=max_turns,
                tools=["Read", "Write", "Glob", "Grep", "Bash"],
                permission_mode=permission_mode or None,
                system_prompt=system_prompt,
                cwd=repo_path,
            )

        result = await _run_planning_call_with_schema_retries(
            _invoke,
            stage="PM",
            failure_label="Product manager failed to produce a valid PRD",
            provider=provider,
            model=model,
            schema=PRD,
            raw_response_path=os.path.join(
                base, "plan", "product_manager_raw_response.txt"
            ),
            max_schema_retries=PLANNING_ROLE_SCHEMA_RETRIES,
        )
        return result.parsed

    initial_prior = list(prior_user_responses or [])
    parsed = await run_with_ask_user(
        reasoner_fn=_invoke_pm,
        reasoner_kwargs={"prior_user_responses": initial_prior},
        app=router,
        hax_client=build_hax_client_from_env(),
        budget=AskUserBudget(remaining=2),
        webhook_url=approval_webhook_url(router),
        note_label="product_manager",
    )

    if parsed is None:
        # Defensive: _invoke_pm raises through the retry helper on schema
        # failures, so this is only reachable if the ask-user wrapper returns
        # no result. Name the provider+model so the failure is diagnosable.
        raise RuntimeError(
            f"Product manager failed to produce a valid PRD "
            f"(provider={provider}, model={model})"
        )

    router.note("PM complete", tags=["pm", "complete"])
    return parsed.model_dump()


@internal_role(router, "planning")
async def run_environment_scout(
    prd: dict,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
    prior_user_responses: list[dict] | None = None,
) -> dict:
    """Negotiate scoped third-party credentials with the user before architecture.

    Runs once between PM and Architect. Reads the PRD + repo, identifies
    third-party services the build will need to talk to, and asks the user
    via a single Hax form for scoped/temporary tokens. The negotiated values
    are returned in ``scoped_credentials`` (env_var -> value); the caller is
    responsible for stashing them in the in-memory store with
    ``store_scoped_credentials(execution_id, creds)``.

    Returns an empty ``scoped_credentials`` dict when:
      * HAX is disabled (``build_hax_client_from_env`` returns None)
      * The LLM decides no credentials are needed (e.g. purely local PRD)
      * The user opts out by leaving every form field blank
    """
    router.note("Environment scout starting", tags=["scout", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    _ensure_paths(base)  # ensure paths exist; we don't write artifacts here

    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415
    from swe_af.hitl import (  # noqa: PLC0415
        AskUserBudget,
        ScoutResult,
        approval_webhook_url,
        build_hax_client_from_env,
        run_with_ask_user,
        store_scoped_credentials,
    )
    from swe_af.prompts.environment_scout import (  # noqa: PLC0415
        SYSTEM_PROMPT,
        environment_scout_task_prompt,
    )

    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    provider = runtime_to_harness_adapter(ai_provider)

    async def _invoke_scout(prior_user_responses: list[dict] | None) -> ScoutResult | None:
        task_prompt = environment_scout_task_prompt(
            prd=prd,
            repo_path=repo_path,
            workspace_manifest=ws_manifest,
            prior_user_responses=prior_user_responses,
        )
        result = await router.harness(
            prompt=task_prompt,
            schema=ScoutResult,
            provider=provider,
            model=model,
            max_turns=max_turns,
            tools=["Read", "Glob", "Grep", "Bash"],
            permission_mode=permission_mode or None,
            system_prompt=SYSTEM_PROMPT,
            cwd=repo_path,
        )
        check_fatal_harness_error(result)
        return result.parsed

    initial_prior = list(prior_user_responses or [])
    parsed = await run_with_ask_user(
        reasoner_fn=_invoke_scout,
        reasoner_kwargs={"prior_user_responses": initial_prior},
        app=router,
        hax_client=build_hax_client_from_env(),
        budget=AskUserBudget(remaining=2),
        webhook_url=approval_webhook_url(router),
        note_label="environment_scout",
    )

    if parsed is None:
        router.note(
            "Scout produced no parseable result — proceeding without credentials",
            tags=["scout", "fallback"],
        )
        return ScoutResult(
            summary="Scout produced no parseable result; proceeding without credentials.",
        ).model_dump(exclude={"scoped_credentials"})

    # Stash credentials in the process-local in-memory store under the build's
    # run_id (shared across every reasoner in this build). This MUST happen
    # before we strip them out of the return value — otherwise the build()
    # caller has no way to retrieve them.
    ctx = getattr(router, "ctx", None)
    scope_id = (
        getattr(ctx, "run_id", None) or getattr(ctx, "root_workflow_id", None) or ""
    )
    if scope_id and parsed.scoped_credentials:
        store_scoped_credentials(scope_id, parsed.scoped_credentials)

    creds_count = len(parsed.scoped_credentials)
    skipped_count = len(parsed.skipped_services)
    router.note(
        f"Scout complete: {creds_count} credential(s) negotiated, "
        f"{skipped_count} skipped",
        tags=["scout", "complete"],
    )
    # SAFETY: scoped_credentials is EXCLUDED from the returned dict. The
    # control plane logs reasoner return values, so any value in this dict is
    # persisted. The credentials live only in process memory via the call
    # above; downstream reasoners retrieve them with get_scoped_credentials
    # using the same scope_id (router.ctx.run_id).
    return parsed.model_dump(exclude={"scoped_credentials"})


@internal_role(router, "planning")
async def run_architect(
    prd: dict,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    feedback: str = "",
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Run the architect agent to produce a technical architecture."""
    router.note("Architect starting", tags=["architect", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)

    prd_obj = PRD(**prd)
    from swe_af.prompts.architect import architect_prompts, architect_task_prompt  # noqa: PLC0415
    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415

    system_prompt, _ = architect_prompts(
        prd=prd_obj,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        feedback=feedback or None,
    )
    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    task_prompt = architect_task_prompt(
        prd=prd_obj,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        feedback=feedback or None,
        workspace_manifest=ws_manifest,
    )
    provider = runtime_to_harness_adapter(ai_provider)

    async def _invoke(previous_error: str | None):
        prompt = task_prompt
        if previous_error:
            prompt = f"{task_prompt}\n\n{_schema_retry_context(previous_error)}"
        return await router.harness(
            prompt=prompt,
            schema=Architecture,
            provider=provider,
            model=model,
            max_turns=max_turns,
            tools=["Read", "Write", "Glob", "Grep", "Bash"],
            permission_mode=permission_mode or None,
            system_prompt=system_prompt,
            cwd=repo_path,
        )

    result = await _run_planning_call_with_schema_retries(
        _invoke,
        stage="Architect",
        failure_label="Architect failed to produce a valid architecture",
        provider=provider,
        model=model,
        schema=Architecture,
        raw_response_path=os.path.join(base, "plan", "architect_raw_response.txt"),
        max_schema_retries=PLANNING_ROLE_SCHEMA_RETRIES,
    )

    router.note("Architect complete", tags=["architect", "complete"])
    return result.parsed.model_dump()


@internal_role(router, "planning")
async def run_tech_lead(
    prd: dict,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    revision_number: int = 0,
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Run the tech lead agent to review the architecture against the PRD."""
    router.note("Tech Lead starting", tags=["tech_lead", "start"])

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)

    from swe_af.prompts.tech_lead import tech_lead_prompts, tech_lead_task_prompt  # noqa: PLC0415
    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415

    system_prompt, _ = tech_lead_prompts(
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        revision_number=revision_number,
    )
    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    task_prompt = tech_lead_task_prompt(
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
        revision_number=revision_number,
        workspace_manifest=ws_manifest,
    )
    provider = runtime_to_harness_adapter(ai_provider)

    async def _invoke(previous_error: str | None):
        prompt = task_prompt
        if previous_error:
            prompt = f"{task_prompt}\n\n{_schema_retry_context(previous_error)}"
        return await router.harness(
            prompt=prompt,
            schema=ReviewResult,
            provider=provider,
            model=model,
            max_turns=max_turns,
            tools=["Read", "Write", "Glob", "Grep"],
            permission_mode=permission_mode or None,
            system_prompt=system_prompt,
            cwd=repo_path,
        )

    result = await _run_planning_call_with_schema_retries(
        _invoke,
        stage="Tech lead",
        failure_label="Tech lead failed to produce a valid review",
        provider=provider,
        model=model,
        schema=ReviewResult,
        raw_response_path=os.path.join(base, "plan", "tech_lead_raw_response.txt"),
        max_schema_retries=PLANNING_ROLE_SCHEMA_RETRIES,
    )

    review = result.parsed.model_dump()
    review_json_path = os.path.join(base, "plan", "review.json")
    with open(review_json_path, "w") as f:
        json.dump(review, f, indent=2, default=str)

    router.note("Tech Lead complete", tags=["tech_lead", "complete"])
    return review


@internal_role(router, "planning")
async def run_sprint_planner(
    prd: dict,
    architecture: dict,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    model: str = "sonnet",
    max_turns: int = DEFAULT_AGENT_MAX_TURNS,
    permission_mode: str = "",
    ai_provider: str = "claude",
    workspace_manifest: dict | None = None,
) -> dict:
    """Run the sprint planner to decompose work into executable issues.

    Returns a dict with ``issues`` (list of issue dicts) and ``rationale`` (str).

    A response that does not parse/validate is retried up to
    ``SPRINT_PLANNER_SCHEMA_RETRIES`` times, each retry feeding the validation
    error back into the task prompt. Every failed attempt and the terminal
    outcome are appended to ``plan/sprint_planner_raw_response.txt``.
    """
    router.note("Sprint Planner starting", tags=["sprint_planner", "start"])

    class SprintPlanOutput(BaseModel):
        issues: list[PlannedIssue]
        rationale: str

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    paths = _ensure_paths(base)

    prd_obj = PRD(**prd)
    arch_obj = Architecture(**architecture)
    from swe_af.prompts.sprint_planner import (
        sprint_planner_prompts,
        sprint_planner_task_prompt,
    )  # noqa: PLC0415
    from swe_af.execution.schemas import WorkspaceManifest  # noqa: PLC0415

    system_prompt, _ = sprint_planner_prompts(
        prd=prd_obj,
        architecture=arch_obj,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
    )
    ws_manifest = (
        WorkspaceManifest(**workspace_manifest) if workspace_manifest else None
    )
    task_prompt = sprint_planner_task_prompt(
        goal=prd_obj.validated_description,
        prd=prd_obj,
        architecture=arch_obj,
        workspace_manifest=ws_manifest,
        repo_path=repo_path,
        prd_path=paths["prd"],
        architecture_path=paths["architecture"],
    )
    provider = runtime_to_harness_adapter(ai_provider)

    async def _invoke(previous_error: str | None):
        prompt = task_prompt
        if previous_error:
            prompt = f"{task_prompt}\n\n{_schema_retry_context(previous_error)}"
        return await router.harness(
            prompt=prompt,
            schema=SprintPlanOutput,
            provider=provider,
            model=model,
            max_turns=max_turns,
            tools=["Read", "Write", "Glob", "Grep"],
            permission_mode=permission_mode or None,
            system_prompt=system_prompt,
            cwd=repo_path,
        )

    result = await _run_planning_call_with_schema_retries(
        _invoke,
        stage="Sprint planner",
        failure_label="Sprint planner failed to produce valid issues",
        provider=provider,
        model=model,
        schema=SprintPlanOutput,
        raw_response_path=os.path.join(base, "plan", "sprint_planner_raw_response.txt"),
        max_schema_retries=SPRINT_PLANNER_SCHEMA_RETRIES,
    )

    router.note("Sprint Planner complete", tags=["sprint_planner", "complete"])
    return {
        "issues": [issue.model_dump() for issue in result.parsed.issues],
        "rationale": result.parsed.rationale,
    }
