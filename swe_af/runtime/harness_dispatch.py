"""Explicit, fail-closed selection boundary for SWE-AF harness backends.

The default backend delegates unchanged to AgentField. Alternative backends
must be explicitly registered on a dispatcher instance and selected with
``SWE_HARNESS_BACKEND``. Tool-using alternative handlers require a safe tool
executor; no concrete executor or Gemini backend is provided here.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

from swe_af.runtime.tool_executor import SafeToolExecutor, TOOL_NAMES


DEFAULT_HARNESS_BACKEND = "agentfield"
HARNESS_BACKEND_ENV = "SWE_HARNESS_BACKEND"


class HarnessBackendConfigurationError(RuntimeError):
    """The configured backend name is invalid or has no registered handler."""


class HarnessToolExecutorUnavailable(HarnessBackendConfigurationError):
    """A selected alternative backend requested tools without a safe executor."""


class HarnessOutcome(Protocol):
    """Fields currently returned by AgentField ``HarnessResult``."""

    result: str | None
    parsed: Any
    is_error: bool
    error_message: str | None
    failure_type: Any
    cost_usd: float | None
    num_turns: int
    duration_ms: int
    session_id: str
    messages: list[dict[str, Any]]
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_creation_tokens: int
    total_tokens: int
    model: str | None


class HarnessAgent(Protocol):
    """Minimum public surface needed from an AgentField Agent instance."""

    async def harness(self, prompt: str, **kwargs: Any) -> HarnessOutcome: ...


@dataclass(frozen=True)
class HarnessExecutionRequest:
    """Typed view of a harness request with lossless AgentField option passthrough."""

    prompt: str
    options: Mapping[str, Any]
    prompt_as_keyword: bool = False

    @classmethod
    def create(
        cls,
        prompt: str,
        options: Mapping[str, Any],
        *,
        prompt_as_keyword: bool = False,
    ) -> HarnessExecutionRequest:
        return cls(
            prompt=prompt,
            options=MappingProxyType(dict(options)),
            prompt_as_keyword=prompt_as_keyword,
        )

    @property
    def provider(self) -> str | None:
        value = self.options.get("provider")
        return value if isinstance(value, str) else None

    @property
    def model(self) -> str | None:
        value = self.options.get("model")
        return value if isinstance(value, str) else None

    @property
    def schema(self) -> Any:
        return self.options.get("schema")

    @property
    def tools(self) -> Sequence[str] | None:
        value = self.options.get("tools")
        return value if isinstance(value, (list, tuple)) else None

    @property
    def cwd(self) -> str | None:
        value = self.options.get("cwd")
        return value if isinstance(value, str) else None

    @property
    def project_dir(self) -> str | None:
        value = self.options.get("project_dir")
        return value if isinstance(value, str) else None

    @property
    def permission_mode(self) -> str | None:
        value = self.options.get("permission_mode")
        return value if isinstance(value, str) else None

    @property
    def timeout_seconds(self) -> float | int | None:
        value = self.options.get("timeout_seconds")
        return value if isinstance(value, (int, float)) else None

    @property
    def system_prompt(self) -> str | None:
        value = self.options.get("system_prompt")
        return value if isinstance(value, str) else None

    def agentfield_options(self) -> dict[str, Any]:
        """Recreate the exact keyword mapping expected by ``Agent.harness``."""
        return dict(self.options)


HarnessBackendHandler = Callable[
    [HarnessAgent, HarnessExecutionRequest, SafeToolExecutor | None],
    Awaitable[HarnessOutcome],
]


@dataclass(frozen=True)
class HarnessBackendRegistration:
    handler: HarnessBackendHandler
    tool_executor: SafeToolExecutor | None = None


async def _agentfield_handler(
    agent: HarnessAgent,
    request: HarnessExecutionRequest,
    _tool_executor: SafeToolExecutor | None,
) -> HarnessOutcome:
    options = request.agentfield_options()
    if request.prompt_as_keyword:
        return await agent.harness(prompt=request.prompt, **options)
    return await agent.harness(request.prompt, **options)


def configured_backend_name() -> str:
    """Return a strict backend selection; blank/unknown values are not defaults."""
    raw = os.getenv(HARNESS_BACKEND_ENV)
    if raw is None:
        return DEFAULT_HARNESS_BACKEND
    name = raw.strip()
    if not name:
        raise HarnessBackendConfigurationError(
            f"{HARNESS_BACKEND_ENV} is set but empty; expected a registered backend name"
        )
    return name


class HarnessDispatcher:
    """Immutable handler registry; selection is explicit and never falls back.

    Existing AgentField CLI tools retain their provider-specific semantics only
    for the built-in AgentField registration. Any alternative backend receiving
    a non-empty tool list must be configured with a SafeToolExecutor whose
    allowlist covers every requested tool.
    """

    def __init__(
        self, backend_handlers: Mapping[str, HarnessBackendRegistration]
    ) -> None:
        handlers = dict(backend_handlers)
        for name, registration in handlers.items():
            if not isinstance(name, str) or not name.strip() or name != name.strip():
                raise ValueError(
                    "backend handler names must be non-empty trimmed strings"
                )
            if not isinstance(registration, HarnessBackendRegistration):
                raise TypeError(
                    f"handler for backend {name!r} must be a HarnessBackendRegistration"
                )
        self._backend_handlers = MappingProxyType(handlers)

    async def dispatch(
        self,
        agent: HarnessAgent,
        request: HarnessExecutionRequest,
        *,
        backend_name: str,
    ) -> HarnessOutcome:
        registration = self._backend_handlers.get(backend_name)
        if registration is None:
            raise HarnessBackendConfigurationError(
                f"harness backend {backend_name!r} is not registered; "
                f"registered backends: {', '.join(sorted(self._backend_handlers)) or '(none)'}"
            )

        raw_tools = request.options.get("tools")
        if raw_tools is None:
            requested_tools = frozenset()
        elif not isinstance(raw_tools, (list, tuple)) or not all(
            isinstance(tool, str) for tool in raw_tools
        ):
            raise HarnessBackendConfigurationError(
                f"tools for backend {backend_name!r} must be a list/tuple of tool names"
            )
        else:
            requested_tools = frozenset(raw_tools)

        if requested_tools and registration is not _AGENTFIELD_REGISTRATION:
            executor = registration.tool_executor
            if executor is None:
                raise HarnessToolExecutorUnavailable(
                    f"backend {backend_name!r} requested tools but has no SafeToolExecutor"
                )
            unknown_tools = requested_tools.difference(TOOL_NAMES)
            if unknown_tools:
                raise HarnessBackendConfigurationError(
                    f"backend {backend_name!r} requested unsupported tools: "
                    f"{', '.join(sorted(unknown_tools))}"
                )
            raw_executor_tools = executor.allowed_tools
            if not isinstance(raw_executor_tools, frozenset) or not all(
                isinstance(tool, str) for tool in raw_executor_tools
            ):
                raise HarnessBackendConfigurationError(
                    f"SafeToolExecutor for backend {backend_name!r} must expose a "
                    "frozenset of tool names"
                )
            executor_tools = frozenset(raw_executor_tools)
            invalid_executor_tools = executor_tools.difference(TOOL_NAMES)
            if invalid_executor_tools:
                raise HarnessBackendConfigurationError(
                    f"SafeToolExecutor for backend {backend_name!r} has unknown tools: "
                    f"{', '.join(sorted(invalid_executor_tools))}"
                )
            missing_tools = requested_tools.difference(executor_tools)
            if missing_tools:
                raise HarnessBackendConfigurationError(
                    f"SafeToolExecutor for backend {backend_name!r} does not allow: "
                    f"{', '.join(sorted(missing_tools))}"
                )

        return await registration.handler(agent, request, registration.tool_executor)


_AGENTFIELD_REGISTRATION = HarnessBackendRegistration(handler=_agentfield_handler)
_DEFAULT_DISPATCHER = HarnessDispatcher(
    {DEFAULT_HARNESS_BACKEND: _AGENTFIELD_REGISTRATION}
)


async def dispatch_harness(
    agent: HarnessAgent,
    *args: Any,
    harness_dispatcher: HarnessDispatcher | None = None,
    **options: Any,
) -> HarnessOutcome:
    """Select and dispatch one backend, preserving AgentField call semantics.

    The application-level selector is ``SWE_HARNESS_BACKEND`` (unset means
    ``agentfield``). Unknown configuration and missing handlers raise before
    executing a handler. Prompt call shape, provider/model/schema/tools/workspace
    options, result identity, exceptions, and cancellation are preserved.
    """
    if args:
        if len(args) != 1 or "prompt" in options:
            raise TypeError("dispatch_harness expects exactly one prompt")
        prompt = args[0]
        prompt_as_keyword = False
    else:
        try:
            prompt = options.pop("prompt")
        except KeyError as exc:
            raise TypeError("dispatch_harness missing required prompt") from exc
        prompt_as_keyword = True

    request = HarnessExecutionRequest.create(
        prompt, options, prompt_as_keyword=prompt_as_keyword
    )
    backend_name = configured_backend_name()
    dispatcher = (
        _DEFAULT_DISPATCHER if harness_dispatcher is None else harness_dispatcher
    )
    return await dispatcher.dispatch(agent, request, backend_name=backend_name)
