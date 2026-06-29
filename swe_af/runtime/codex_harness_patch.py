from __future__ import annotations

import asyncio
import contextvars
import json
import os
import shutil
import tempfile
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
active_output_paths: contextvars.ContextVar[dict[str, str] | None] = contextvars.ContextVar(
    "swe_af_codex_output_paths", default=None
)

_ORIGINAL_BUILD_PROMPT_SUFFIX: Any = None


def _codex_strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(schema, dict):
        return schema
    if not schema:
        # Codex/OpenAI structured output rejects unconstrained `{}` schemas,
        # including Pydantic `Any` branches inside `anyOf`.
        return {"type": "string"}
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
        else:
            additional = strict.get("additionalProperties")
            if additional is True:
                # Codex/OpenAI strict structured output does not accept
                # free-form maps. Keep the field object-shaped for Pydantic,
                # but require it to be empty.
                strict["properties"] = {}
                strict["required"] = []
                strict["additionalProperties"] = False
            elif isinstance(additional, dict):
                strict["properties"] = {}
                strict["required"] = []
                strict["additionalProperties"] = False
            else:
                strict["properties"] = {}
                strict["required"] = []
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


def _codex_no_final_message_error(records: Any) -> tuple[str, bool]:
    if not isinstance(records, list):
        return ("Codex CLI completed without a final assistant message.", False)

    for record in records:
        if not isinstance(record, dict):
            continue
        payload = record.get("payload")
        event = payload if isinstance(payload, dict) else record
        if event.get("type") != "token_count":
            continue
        rate_limits = event.get("rate_limits")
        if not isinstance(rate_limits, dict):
            continue
        credits = rate_limits.get("credits")
        if isinstance(credits, dict) and credits.get("has_credits") is False:
            limit_id = rate_limits.get("limit_id") or "unknown"
            balance = credits.get("balance")
            balance_note = f", balance={balance}" if balance is not None else ""
            return (
                "Codex CLI completed without a final assistant message because "
                f"Codex reported unavailable credits/rate-limit capacity "
                f"(limit_id={limit_id}{balance_note}).",
                True,
            )
        rate_limit_type = rate_limits.get("rate_limit_reached_type")
        if rate_limit_type:
            return (
                "Codex CLI completed without a final assistant message because "
                f"Codex reported a rate limit ({rate_limit_type}).",
                True,
            )

    return ("Codex CLI completed without a final assistant message.", False)


async def _run_codex_cli_with_stdin(
    cmd: list[str],
    prompt_for_codex: str,
    *,
    env: dict[str, str] | None,
    cwd: str | None,
) -> tuple[str, str, int]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
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
            apply_subprocess_env,
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
        output_dir = tempfile.mkdtemp(prefix=".agentfield-codex-", dir=cwd)
        schema_path = Path(output_dir) / "schema.json"
        output_path = Path(output_dir) / "output.json"
        schema_path.write_text(schema_json, encoding="utf-8")
        active_output_paths.set(
            {
                "schema": str(schema_path),
                "output": str(output_path),
                "dir": output_dir,
            }
        )
        return (
            "\n\n---\n"
            "CRITICAL CODEX STRUCTURED OUTPUT REQUIREMENTS:\n"
            f"Return a single final JSON object conforming to: {schema_path}\n"
            "Do not use markdown fences, comments, or surrounding prose.\n"
            "Do not try to create .agentfield_output.json yourself; the Codex "
            "CLI will persist your final JSON response for AgentField."
        )

    async def execute_with_native_structured_output(self: Any, prompt: str, options: dict[str, object]) -> Any:
        root = options.get("project_dir") or options.get("cwd")
        cwd = str(root) if isinstance(root, str) else None
        model = options.get("model")
        permission_mode = options.get("permission_mode")
        env_value = options.get("env")
        merged_env = {**os.environ}
        if isinstance(env_value, dict):
            merged_env.update({str(k): str(v) for k, v in env_value.items() if isinstance(k, str)})
        apply_subprocess_env(merged_env)

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
        output_paths = active_output_paths.get()
        schema_path = output_paths.get("schema") if output_paths else None
        output_path = output_paths.get("output") if output_paths else None
        if not schema_path and cwd:
            schema_path = _schema.get_schema_path(cwd)
            output_path = _schema.get_output_path(cwd)

        if schema_path and output_path and Path(schema_path).exists():
            cmd.extend(["--output-schema", schema_path])
            cmd.extend(["--output-last-message", output_path])
            prompt_for_codex += (
                "\n\n---\n"
                "CODEX STRUCTURED OUTPUT CONTRACT:\n"
                f"The Codex CLI will save your final response to: {output_path}\n"
                f"Your final response MUST be a single JSON object conforming to: {schema_path}\n"
                "Return the JSON object as your final answer. Do not write "
                "the output file yourself or make the output file the task."
            )

        try:
            start = asyncio.get_running_loop().time()
            timeout_seconds = options.get("timeout_seconds")
            if isinstance(timeout_seconds, (int, float)) and timeout_seconds > 0:
                stdout, stderr, returncode = await asyncio.wait_for(
                    _run_codex_cli_with_stdin(cmd, prompt_for_codex, env=merged_env, cwd=cwd),
                    timeout=float(timeout_seconds),
                )
            else:
                stdout, stderr, returncode = await _run_codex_cli_with_stdin(
                    cmd, prompt_for_codex, env=merged_env, cwd=cwd
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

        if not result_text and output_path:
            output_file = Path(output_path)
            if output_file.exists():
                try:
                    result_text = output_file.read_text(encoding="utf-8")
                except Exception:
                    result_text = ""

        is_error = returncode != 0
        error_message = ""
        failure_type = FailureType.NONE
        if not result_text:
            error_message, is_api_error = _codex_no_final_message_error(records)
            is_error = True
            failure_type = FailureType.API_ERROR if is_api_error else FailureType.NO_OUTPUT
        if is_error:
            stdout_error = ""
            if isinstance(records, list):
                for record in records:
                    if isinstance(record, dict) and record.get("type") in {
                        "error",
                        "turn.failed",
                    }:
                        stdout_error = json.dumps(record, ensure_ascii=False)
                        break
            base_error = "\n".join(
                part for part in (stderr_clean, stdout_error) if part
            ) or error_message or "Codex CLI failed"
            error_message = _augment_codex_error_message(base_error, base_error)
            if returncode != 0:
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
        output_token = active_output_paths.set(None)
        try:
            return await _orig_agent_harness(self, prompt, *args, **kwargs)
        finally:
            output_paths = active_output_paths.get()
            tmp_dir = output_paths.get("dir") if output_paths else None
            if tmp_dir:
                shutil.rmtree(tmp_dir, ignore_errors=True)
            active_output_paths.reset(output_token)
            active_provider.reset(token)

    _schema.build_prompt_suffix = build_prompt_suffix_dispatching
    _runner.build_prompt_suffix = build_prompt_suffix_dispatching
    CodexProvider.execute = execute_with_native_structured_output
    Agent.harness = _harness_with_provider_context
    _PATCHED = True
