"""Application-owned, fail-closed boundary for harness invocation.

AgentField remains the sole default implementation. This module deliberately
exposes no provider-SDK registration hooks; alternative handlers must be passed
explicitly by an application composition root and are not enabled here.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Protocol

DEFAULT_BACKEND = "agentfield"
BACKEND_ENV = "SWE_HARNESS_BACKEND"


class HarnessBackendUnavailable(RuntimeError):
    """The requested application backend has no explicitly supplied handler."""


class HarnessAgent(Protocol):
    async def harness(self, prompt: str, **options: Any) -> Any: ...


@dataclass(frozen=True)
class HarnessRequest:
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
    ) -> "HarnessRequest":
        return cls(prompt, MappingProxyType(dict(options)), prompt_as_keyword)


BackendHandler = Callable[[HarnessAgent, HarnessRequest], Awaitable[Any]]
_REGISTERED_HANDLERS: dict[str, BackendHandler] = {}
_REGISTRY_LOCK = threading.Lock()


def register_backend(name: str, handler: BackendHandler) -> None:
    """Register an application-owned backend before selecting it by env.

    Registration is explicit, process-local, and never replaces AgentField or
    an existing handler. It does not provide transport, retry, or safety
    guarantees for the supplied implementation.
    """
    if not isinstance(name, str) or not name.strip() or name != name.strip():
        raise ValueError("backend name must be a non-empty trimmed string")
    if name == DEFAULT_BACKEND or not callable(handler):
        raise ValueError("cannot replace AgentField; backend handler must be callable")
    with _REGISTRY_LOCK:
        existing = _REGISTERED_HANDLERS.get(name)
        if existing is handler:
            return
        if existing is not None:
            raise ValueError(f"harness backend {name!r} is already registered")
        _REGISTERED_HANDLERS[name] = handler


def _agentfield(agent: HarnessAgent, request: HarnessRequest) -> Awaitable[Any]:
    options = dict(request.options)
    if request.prompt_as_keyword:
        return agent.harness(prompt=request.prompt, **options)
    return agent.harness(request.prompt, **options)


def configured_backend() -> str:
    value = os.getenv(BACKEND_ENV)
    if value is None:
        return DEFAULT_BACKEND
    value = value.strip()
    if not value:
        raise HarnessBackendUnavailable(f"{BACKEND_ENV} is set but empty")
    return value


async def dispatch_harness(
    agent: HarnessAgent,
    *args: Any,
    handlers: Mapping[str, BackendHandler] | None = None,
    **options: Any,
) -> Any:
    """Dispatch exactly once; unknown backends fail instead of falling back.

    The default preserves AgentField's call shape and result/exception identity.
    A non-default backend is usable only when an application composition root
    explicitly supplies its handler in ``handlers``. No handler is registered
    globally, and a handler's internal transport/retry behavior is not managed
    by this application-level boundary.
    """
    if args:
        if len(args) != 1 or "prompt" in options:
            raise TypeError("dispatch_harness expects exactly one prompt")
        prompt = args[0]
        keyword_prompt = False
    else:
        try:
            prompt = options.pop("prompt")
        except KeyError as exc:
            raise TypeError("dispatch_harness missing required prompt") from exc
        keyword_prompt = True

    request = HarnessRequest.create(
        prompt, options, prompt_as_keyword=keyword_prompt
    )
    backend = configured_backend()
    with _REGISTRY_LOCK:
        selected = {DEFAULT_BACKEND: _agentfield, **_REGISTERED_HANDLERS}
    if handlers is not None:
        for name, handler in handlers.items():
            if name == DEFAULT_BACKEND:
                raise ValueError("AgentField default handler cannot be replaced")
            if not isinstance(name, str) or not name.strip() or not callable(handler):
                raise ValueError("backend handlers require a non-empty name and callable")
            selected[name] = handler
    handler = selected.get(backend)
    if handler is None:
        raise HarnessBackendUnavailable(
            f"harness backend {backend!r} is unavailable; no fallback was attempted"
        )
    return await handler(agent, request)
