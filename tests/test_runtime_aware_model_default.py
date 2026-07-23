"""Runtime-aware planning-model defaults + distinct empty-completion errors.

Validation contract (see PR "fix(planning): make auto model defaults
runtime-aware; surface empty harness completions distinctly"):

- OpenRouter-only env + runtime ``codex`` → the auto default is NOT an
  ``openrouter/``-prefixed id (it is a codex-native model). A caller pinning
  ``codex`` in an OpenRouter-only environment must never hand the codex CLI a
  model its OpenAI backend can't resolve (the silent ~1s empty-completion bug).
- OpenRouter-only env + runtime ``open_code`` → the OpenRouter auto default is
  preserved (unchanged behavior).
- runtime ``claude_code`` → the ``sonnet`` historical default is preserved.
- ``SWE_DEFAULT_MODEL`` (deployer env) wins in every runtime cell.
- An empty harness completion raises a distinct error that names the provider
  and the model, separate from the generic schema-invalid message.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from swe_af.execution.fatal_error import (
    EmptyHarnessCompletionError,
    check_empty_harness_completion,
)
from swe_af.execution.schemas import (
    _CODEX_CHATGPT_MODEL,
    _OPENROUTER_AUTO_DEFAULT_MODEL,
    _default_planning_model,
)

# open_code's non-auto base default (an explicit open_code runtime, i.e. not
# the OpenRouter-only auto-selection path). Mirrors _RUNTIME_BASE_MODELS.
_OPEN_CODE_BASE = "openrouter/minimax/minimax-m2.5"

# Every env var that steers runtime/model selection — cleared before each test
# so results never depend on the developer's ambient shell.
_STEERING_ENV_KEYS = (
    "ANTHROPIC_API_KEY",
    "OPENROUTER_API_KEY",
    "OPENAI_API_KEY",
    "SWE_DEFAULT_RUNTIME",
    "SWE_DEFAULT_MODEL",
    "AI_MODEL",
    "HARNESS_MODEL",
    "SWE_MODEL_LOW",
    "SWE_MODEL_MED",
    "SWE_MODEL_HIGH",
    "SWE_CODEX_AUTH_MODE",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _STEERING_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Matrix: (env) x (runtime) -> resolved planning-model default
# ---------------------------------------------------------------------------

# Env presets applied via monkeypatch.setenv. "swe_default_model" also sets an
# OpenRouter key to prove the explicit env value wins over the auto default.
_ENV_PRESETS: dict[str, dict[str, str]] = {
    "openrouter_only": {"OPENROUTER_API_KEY": "sk-or-test"},
    "anthropic": {"ANTHROPIC_API_KEY": "sk-ant-test"},
    "swe_default_model": {
        "OPENROUTER_API_KEY": "sk-or-test",
        "SWE_DEFAULT_MODEL": "explicit/model-x",
    },
}

# (env_preset, runtime) -> expected resolved default.
# Under "swe_default_model" the deployer env wins verbatim for every runtime.
# Under "openrouter_only"/"anthropic" the runtime's own auto/base default is
# used — critically never an openrouter/ id for codex.
_MATRIX: list[tuple[str, str, str]] = [
    ("openrouter_only", "open_code", _OPENROUTER_AUTO_DEFAULT_MODEL),
    ("openrouter_only", "codex", _CODEX_CHATGPT_MODEL),
    ("openrouter_only", "claude_code", "sonnet"),
    ("anthropic", "open_code", _OPEN_CODE_BASE),
    ("anthropic", "codex", _CODEX_CHATGPT_MODEL),
    ("anthropic", "claude_code", "sonnet"),
    ("swe_default_model", "open_code", "explicit/model-x"),
    ("swe_default_model", "codex", "explicit/model-x"),
    ("swe_default_model", "claude_code", "explicit/model-x"),
]


@pytest.mark.parametrize(
    ("env_preset", "runtime", "expected"),
    _MATRIX,
    ids=[f"{e}-{r}" for e, r, _ in _MATRIX],
)
def test_default_planning_model_matrix(
    monkeypatch: pytest.MonkeyPatch,
    env_preset: str,
    runtime: str,
    expected: str,
) -> None:
    """The resolved planning-model default is correct for each env x runtime."""
    for key, value in _ENV_PRESETS[env_preset].items():
        monkeypatch.setenv(key, value)

    assert _default_planning_model(runtime) == expected


@pytest.mark.parametrize("env_preset", ["openrouter_only", "anthropic"])
def test_codex_default_is_never_openrouter_prefixed(
    monkeypatch: pytest.MonkeyPatch, env_preset: str
) -> None:
    """Contract core: pinning codex never yields an openrouter/ model as the
    auto default, regardless of which provider key happens to be present."""
    for key, value in _ENV_PRESETS[env_preset].items():
        monkeypatch.setenv(key, value)

    resolved = _default_planning_model("codex")
    assert not resolved.startswith("openrouter/"), (
        f"codex runtime leaked a cross-runtime model id: {resolved!r}"
    )


def test_runtime_aliases_are_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    """Alias runtime spellings resolve the same as their canonical form."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    # "opencode" -> open_code, "claude"/"claude-code" -> claude_code
    assert _default_planning_model("opencode") == _OPENROUTER_AUTO_DEFAULT_MODEL
    assert _default_planning_model("claude") == "sonnet"
    assert _default_planning_model("claude-code") == "sonnet"


def test_omitted_runtime_falls_back_to_env_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No runtime arg → runtime is resolved from env (historical behavior)."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    # OpenRouter-only env auto-selects open_code, so the auto default applies.
    assert _default_planning_model() == _OPENROUTER_AUTO_DEFAULT_MODEL


# ---------------------------------------------------------------------------
# Empty-completion detection: distinct from schema-invalid
# ---------------------------------------------------------------------------


def _fake_result(*, parsed=None, result="", error_message=None):
    """A HarnessResult-like stand-in (parsed/result/error_message + is_error)."""
    return SimpleNamespace(
        parsed=parsed,
        result=result,
        error_message=error_message,
        is_error=False,
    )


class TestCheckEmptyHarnessCompletion:
    def test_empty_completion_raises_naming_provider_and_model(self) -> None:
        result = _fake_result(parsed=None, result="")
        with pytest.raises(EmptyHarnessCompletionError) as exc:
            check_empty_harness_completion(
                result,
                role="PM",
                provider="codex",
                model="openrouter/deepseek/deepseek-v4-flash",
            )
        message = str(exc.value)
        assert "PM" in message
        assert "provider=codex" in message
        assert "model=openrouter/deepseek/deepseek-v4-flash" in message
        assert "empty completion" in message

    def test_is_runtime_error_subclass(self) -> None:
        err = EmptyHarnessCompletionError(role="PM", provider="codex", model="m")
        assert isinstance(err, RuntimeError)
        # Exposes structured fields for programmatic handling.
        assert err.provider == "codex"
        assert err.model == "m"
        assert err.role == "PM"

    def test_error_message_detail_is_appended(self) -> None:
        result = _fake_result(parsed=None, result="", error_message="backend refused")
        with pytest.raises(EmptyHarnessCompletionError, match="backend refused"):
            check_empty_harness_completion(
                result, role="Architect", provider="opencode", model="m"
            )

    def test_unparseable_but_nonempty_output_is_noop(self) -> None:
        # Text present but not schema-parseable → NOT an empty completion; the
        # caller's generic schema-invalid path handles it.
        result = _fake_result(parsed=None, result="this is not valid json")
        check_empty_harness_completion(
            result, role="PM", provider="codex", model="m"
        )  # must not raise

    def test_valid_parsed_output_is_noop(self) -> None:
        result = _fake_result(parsed=object(), result="")
        check_empty_harness_completion(
            result, role="PM", provider="codex", model="m"
        )  # must not raise

    def test_reads_text_property_when_result_absent(self) -> None:
        # Object exposing only `.text` (HarnessResult's convenience property).
        with_text = SimpleNamespace(parsed=None, text="present", is_error=False)
        check_empty_harness_completion(
            with_text, role="PM", provider="codex", model="m"
        )  # non-empty text → no raise

    def test_message_is_distinct_from_generic_schema_invalid(self) -> None:
        """The empty-completion message must be distinguishable from the generic
        'failed to produce a valid ...' schema-quality message."""
        result = _fake_result(parsed=None, result="")
        with pytest.raises(EmptyHarnessCompletionError) as exc:
            check_empty_harness_completion(
                result, role="PM", provider="codex", model="m"
            )
        message = str(exc.value)
        generic = "Product manager failed to produce a valid PRD"
        assert generic not in message
        assert "empty completion" in message


# ---------------------------------------------------------------------------
# Reasoner wiring: run_product_manager surfaces the distinct error end-to-end
# ---------------------------------------------------------------------------


def test_run_product_manager_empty_completion_surfaces_provider_and_model(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty harness completion inside the PM reasoner raises the distinct
    error naming the pinned provider+model — not the generic PRD message.

    Reproduces the incident shape: ai_provider='codex' with an openrouter/
    model id leaked in, harness completes in ~1s with a null message.
    """
    from unittest.mock import AsyncMock, MagicMock, patch

    import swe_af.reasoners.pipeline as pipeline

    # HAX disabled so run_with_ask_user calls the reasoner once (no pause loop).
    monkeypatch.delenv("HAX_API_KEY", raising=False)

    empty = SimpleNamespace(
        parsed=None, result="", text="", error_message=None, is_error=False
    )
    mock_router = MagicMock()
    mock_router.harness = AsyncMock(return_value=empty)
    mock_router.note = MagicMock()
    mock_router.agentfield_server = "http://localhost:9999"

    real_pm = getattr(pipeline.run_product_manager, "_original_func", pipeline.run_product_manager)

    with patch.object(pipeline, "router", mock_router):
        with pytest.raises(EmptyHarnessCompletionError) as exc:
            _run(
                real_pm(
                    goal="Build a thing",
                    repo_path=str(tmp_path),
                    artifacts_dir=".artifacts",
                    model="openrouter/deepseek/deepseek-v4-flash",
                    ai_provider="codex",
                )
            )

    message = str(exc.value)
    assert "provider=codex" in message
    assert "model=openrouter/deepseek/deepseek-v4-flash" in message
    assert "Product manager failed to produce a valid PRD" not in message
