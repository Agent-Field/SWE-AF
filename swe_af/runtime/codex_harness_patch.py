from __future__ import annotations

import asyncio
import contextvars
import json
import os
from pathlib import Path
from typing import Any

_PATCHED = False

# Set by the wrapped Agent.harness for the duration of a harness call.
# Read by the dispatching build_prompt_suffix so that claude_code / open_code
# calls keep the original AgentField "use Write tool" instruction and only
# codex calls get the Codex-native structured-output instruction.
active_provider: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "swe_af_codex_active_provider", default=None
)

_ORIGINAL_BUILD_PROMPT_SUFFIX: Any = None


def _codex_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return schema
    strict = dict(schema)
    schema_type = strict.get("type")
    if schema_type == "object":
        properties = strict.get("properties")
        if isinstance(properties, dict):
            cleaned: dict[str, Any] = {}
            for key, value in properties.items():
                if isinstance(value, dict):
                    child = dict(value)
                    child.pop("default", None)
                    cleaned[key] = _codex_strict_json_schema(child)
                else:
                    cleaned[key] = value
            strict["properties"] = cleaned
            strict["required"] = list(cleaned.keys())
            strict["additionalProperties"] = False
    if schema_type == "array":
        items = strict.get("items")
        if isinstance(items, dict):
            strict["items"] = _codex_strict_json_schema(items)
    for key in ("allOf", "anyOf", "oneOf"):
        branch = strict.get(key)
        if isinstance(branch, list):
            strict[key] = [
                _codex_strict_json_schema(item) if isinstance(item, dict) else item
                for item in branch
            ]
    defs = strict.get("$defs")
    if isinstance(defs, dict):
        strict["$defs"] = {
            key: _codex_strict_json_schema(value) if isinstance(value, dict) else value
            for key, value in defs.items()
        }
    definitions = strict.get("definitions")
    if isinstance(definitions, dict):
        strict["definitions"] = {
            key: _codex_strict_json_schema(value) if isinstance(value, dict) else value
            for key, value in definitions.items()
        }
    return strict


def _codex_schema_strict_expressible(schema: Any) -> bool:
    """Whether a strict-rewritten schema can be sent through --output-schema.

    The server behind ``codex exec`` validates --output-schema against OpenAI
    strict-mode rules (probed live on codex-cli 0.144.1): every object node
    needs ``additionalProperties: false`` plus a full ``required`` array, every
    node needs a ``type`` (or ``$ref`` / anyOf combinator), and free-form maps
    (``dict[str, Any]``), typed maps, bare ``Any`` nodes, and boolean
    subschemas are rejected with ``invalid_json_schema``. Schemas containing
    such nodes must skip the flag: codex still persists its final answer via
    --output-last-message and the harness runner validates locally (issue
    Agent-Field/SWE-AF#106).
    """
    if not isinstance(schema, dict):
        return False  # boolean subschemas ("default_value": true) and the like
    for def_key in ("$defs", "definitions"):
        defs = schema.get(def_key)
        if isinstance(defs, dict):
            if not all(_codex_schema_strict_expressible(value) for value in defs.values()):
                return False
    if isinstance(schema.get("$ref"), str):
        return True  # target checked via the $defs walk above
    for key in ("anyOf", "oneOf", "allOf"):
        branch = schema.get(key)
        if isinstance(branch, list):
            return all(_codex_schema_strict_expressible(item) for item in branch)
    type_value = schema.get("type")
    if isinstance(type_value, str):
        if type_value == "object":
            properties = schema.get("properties")
            if not isinstance(properties, dict):
                return False  # free-form map — strict mode would force {}
            if schema.get("additionalProperties") is not False:
                return False
            return all(
                _codex_schema_strict_expressible(value) for value in properties.values()
            )
        if type_value == "array":
            items = schema.get("items")
            if not isinstance(items, dict):
                return False
            return _codex_schema_strict_expressible(items)
        return True  # primitive leaf
    if isinstance(type_value, list):
        # e.g. ["string", "null"]; only primitive members are safely strict.
        return bool(type_value) and all(
            isinstance(tv, str) and tv not in ("object", "array") for tv in type_value
        )
    return False  # no type, no $ref, no combinator — e.g. {} for Any


_SCHEMA_REJECTION_MARKERS = ("invalid_json_schema", "invalid schema for response_format")


def _is_output_schema_rejection(output: str) -> bool:
    """Whether CLI output carries the server-side strict validator's 400."""
    lower = output.lower()
    return any(marker in lower for marker in _SCHEMA_REJECTION_MARKERS)


def _without_flag_value(cmd: list[str], flag: str) -> list[str]:
    """Return cmd with one ``flag value`` pair removed."""
    out: list[str] = []
    skip_next = False
    for i, arg in enumerate(cmd):
        if skip_next:
            skip_next = False
            continue
        if arg == flag and i + 1 < len(cmd):
            skip_next = True
            continue
        out.append(arg)
    return out


def _augment_codex_error_message(message: str, detail: str) -> str:
    lower = f"{message}\n{detail}".lower()
    hints = (
        ".git/index.lock",
        ".git/refs",
        "repository metadata is read-only",
    )
    if any(token in lower for token in hints):
        return (
            f"{message}\n\n"
            "Codex tried to mutate git metadata under workspace-write; "
            "git must be host-managed."
        )
    return message


async def _run_codex_cli_with_stdin(
    cmd: list[str],
    prompt_for_codex: str,
    *,
    env: dict[str, str] | None,
    cwd: str | None,
) -> tuple[str, str, int]:
    # Windows: CreateProcess does no PATHEXT resolution, and npm installs the
    # codex CLI only as a .cmd shim — spawning the bare name raises
    # FileNotFoundError ([WinError 2]) on every call even though the shell
    # finds it. resolve_cli_command resolves the shim's real path via
    # shutil.which (a no-op on POSIX and for names that already carry a path).
    from agentfield.harness._cli import resolve_cli_command

    proc = await asyncio.create_subprocess_exec(
        resolve_cli_command(cmd[0]),
        *cmd[1:],
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
        cwd=cwd,
    )
    stdout_bytes, stderr_bytes = await proc.communicate(prompt_for_codex.encode("utf-8"))
    stdout = stdout_bytes.decode("utf-8", errors="replace")
    stderr = stderr_bytes.decode("utf-8", errors="replace")
    return stdout, stderr, int(proc.returncode)


def apply_codex_harness_patch() -> None:
    global _PATCHED, _ORIGINAL_BUILD_PROMPT_SUFFIX
    if _PATCHED:
        return
    try:
        from agentfield.agent import Agent
        from agentfield.harness import _runner, _schema
        from agentfield.harness._cli import (
            estimate_cli_cost,
            extract_final_text,
            parse_jsonl,
            strip_ansi,
        )
        from agentfield.harness._result import FailureType, Metrics, RawResult
        from agentfield.harness.providers.codex import CodexProvider
    except Exception:
        return

    _ORIGINAL_BUILD_PROMPT_SUFFIX = _schema.build_prompt_suffix

    def build_prompt_suffix_with_schema_file(schema: Any, cwd: str) -> str:
        """Use Codex-native structured output instead of AgentField's Write-tool suffix.

        AgentField's default suffix asks the model to create
        ``.agentfield_output.json`` with a Write tool. Codex CLI executions may
        run under read-only sandboxing and do not have AgentField's Write tool,
        so that instruction causes no final output. The Codex provider below
        passes ``--output-schema`` and ``--output-last-message`` to the CLI; this
        suffix only needs to create the schema file and ask for final JSON.
        """
        schema_json = json.dumps(
            _codex_strict_json_schema(_schema.schema_to_json_schema(schema)),
            indent=2,
        )
        _schema.write_schema_file(schema_json, cwd)
        schema_path = _schema.get_schema_path(cwd)
        return (
            "\n\n---\n"
            "CRITICAL CODEX STRUCTURED OUTPUT REQUIREMENTS:\n"
            f"Return a single final JSON object conforming to: {schema_path}\n"
            "Do not use markdown fences, comments, or surrounding prose.\n"
            "Do not try to create .agentfield_output.json yourself; the Codex "
            "CLI will persist your final JSON response for AgentField."
        )

    async def execute_with_native_structured_output(self: Any, prompt: str, options: dict[str, object]) -> Any:
        cwd = str(options.get("cwd")) if isinstance(options.get("cwd"), str) else None
        model = options.get("model")
        permission_mode = options.get("permission_mode")
        env_value = options.get("env")
        merged_env = {**os.environ}
        if isinstance(env_value, dict):
            merged_env.update({str(k): str(v) for k, v in env_value.items() if isinstance(k, str)})

        cmd = [self._bin, "exec", "--json", "--skip-git-repo-check"]
        if cwd:
            cmd.extend(["-C", cwd])
        if model:
            cmd.extend(["-m", str(model)])

        if permission_mode == "auto":
            cmd.append("--dangerously-bypass-approvals-and-sandbox")
        elif permission_mode in {"read-only", "workspace-write", "danger-full-access"}:
            cmd.extend(["--sandbox", str(permission_mode)])
        else:
            cmd.extend(["--sandbox", "workspace-write"])

        prompt_for_codex = prompt
        used_output_schema = False
        if cwd:
            schema_path = _schema.get_schema_path(cwd)
            output_path = _schema.get_output_path(cwd)
            if Path(schema_path).exists():
                # --output-schema only when the schema survives OpenAI's strict
                # validator; otherwise the server 400s the whole session with
                # invalid_json_schema. --output-last-message is safe either way
                # and keeps the final answer capturable for local validation.
                try:
                    schema_expressible = _codex_schema_strict_expressible(
                        json.loads(Path(schema_path).read_text(encoding="utf-8"))
                    )
                except Exception:
                    schema_expressible = False
                if schema_expressible:
                    cmd.extend(["--output-schema", schema_path])
                    used_output_schema = True
                cmd.extend(["--output-last-message", output_path])
                prompt_for_codex += (
                    "\n\n---\n"
                    "CODEX STRUCTURED OUTPUT CONTRACT:\n"
                    f"The Codex CLI will save your final response to: {output_path}\n"
                    f"Your final response MUST be a single JSON object conforming to: {schema_path}\n"
                    "Return the JSON object as your final answer. Do not write "
                    "the output file yourself or make the output file the task."
                )

        async def _invoke(cmd_to_run: list[str]) -> tuple[str, str, int]:
            timeout_seconds = options.get("timeout_seconds")
            if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
                return await asyncio.wait_for(
                    _run_codex_cli_with_stdin(
                        cmd_to_run, prompt_for_codex, env=merged_env, cwd=cwd
                    ),
                    timeout=float(timeout_seconds),
                )
            return await _run_codex_cli_with_stdin(
                cmd_to_run, prompt_for_codex, env=merged_env, cwd=cwd
            )

        try:
            start = asyncio.get_running_loop().time()
            stdout, stderr, returncode = await _invoke(cmd)
            # Reactive fallback: if the server's strict validator refused the
            # schema anyway (its rules can tighten upstream at any time), rerun
            # once without --output-schema — the prompt still pins the JSON
            # contract and --output-last-message still captures the answer.
            if (
                returncode != 0
                and used_output_schema
                and _is_output_schema_rejection(f"{stdout}\n{stderr}")
            ):
                stdout, stderr, returncode = await _invoke(
                    _without_flag_value(cmd, "--output-schema")
                )
            duration_ms = int((asyncio.get_running_loop().time() - start) * 1000)
        except FileNotFoundError as exc:
            return RawResult(
                result="",
                messages=[],
                metrics=Metrics(
                    duration_api_ms=0,
                    num_turns=1,
                    total_cost_usd=0.0,
                    session_id="",
                ),
                is_error=True,
                error_message=str(exc),
                failure_type=FailureType.CRASH,
                returncode=-1,
            )
        except asyncio.TimeoutError:
            return RawResult(
                result="",
                messages=[],
                metrics=Metrics(
                    duration_api_ms=0,
                    num_turns=1,
                    total_cost_usd=0.0,
                    session_id="",
                ),
                is_error=True,
                error_message="Codex CLI timed out",
                failure_type=FailureType.TIMEOUT,
                returncode=-1,
            )

        stderr_clean = strip_ansi(stderr or "")
        records = parse_jsonl(stdout or "")
        result_text = extract_final_text(records) or ""

        if not result_text and cwd:
            output_path = _schema.get_output_path(cwd)
            output_file = Path(output_path)
            if output_file.exists():
                try:
                    result_text = output_file.read_text(encoding="utf-8")
                except Exception:
                    result_text = ""

        is_error = returncode != 0
        error_message = ""
        failure_type = FailureType.NONE
        if is_error:
            base_error = stderr_clean or "Codex CLI failed"
            error_message = _augment_codex_error_message(base_error, base_error)
            failure_type = FailureType.CRASH

        return RawResult(
            result=result_text,
            messages=records if isinstance(records, list) else [],
            metrics=Metrics(
                duration_api_ms=duration_ms,
                num_turns=1,
                total_cost_usd=estimate_cli_cost(
                    model=str(options.get("model", "")),
                    prompt=prompt_for_codex,
                    result_text=result_text,
                ),
                session_id="",
            ),
            is_error=is_error,
            error_message=error_message,
            failure_type=failure_type,
            returncode=returncode,
        )

    def build_prompt_suffix_dispatching(schema: Any, cwd: str) -> str:
        """Route to codex-native suffix only when the active call is for codex.

        Without this gate, every claude_code / open_code harness call would
        also receive the codex-specific instruction "Do not try to create
        .agentfield_output.json yourself; the Codex CLI will persist your
        final JSON response" — which is wrong for those providers and forces
        their runner into the slower stdout-parse fallback path.
        """
        if active_provider.get() == "codex":
            return build_prompt_suffix_with_schema_file(schema, cwd)
        return _ORIGINAL_BUILD_PROMPT_SUFFIX(schema, cwd)

    _orig_agent_harness = Agent.harness

    async def _harness_with_provider_context(
        self: Any, prompt: str, *args: Any, **kwargs: Any
    ) -> Any:
        provider_value = kwargs.get("provider")
        token = active_provider.set(str(provider_value) if provider_value else None)
        try:
            return await _orig_agent_harness(self, prompt, *args, **kwargs)
        finally:
            active_provider.reset(token)

    _schema.build_prompt_suffix = build_prompt_suffix_dispatching
    _runner.build_prompt_suffix = build_prompt_suffix_dispatching
    CodexProvider.execute = execute_with_native_structured_output
    Agent.harness = _harness_with_provider_context
    _PATCHED = True
