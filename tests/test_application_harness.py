from __future__ import annotations

import asyncio
import socket
import uuid
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from swe_af.runtime.application_harness import (
    DEFAULT_BACKEND,
    HarnessBackendUnavailable,
    dispatch_harness,
    register_backend,
)
from swe_af.fast.planner import fast_plan_tasks
from swe_af.fast.schemas import FastPlanResult
from swe_af.execution.schemas import DAGState, IssueOutcome, IssueResult
from swe_af.reasoners.execution_agents import run_replanner
from swe_af.runtime.mock_harness import MockHarnessError, register_local_mock


class NetworkAttemptBlocked(AssertionError):
    """Raised when the test-only network guard intercepts a network path."""


def _destination(args, kwargs):
    if "address" in kwargs:
        return kwargs["address"]
    if args:
        return args[0]
    host = kwargs.get("host")
    port = kwargs.get("port")
    return (host, port) if port is not None else host


@contextmanager
def _deny_network_connections():
    """Block Python socket and asyncio connection paths for this context only.

    This is a test guard, not a process/container egress sandbox: native code,
    subprocesses, pre-existing connected sockets, and unlisted transports can
    bypass Python-level hooks.
    """
    attempts = []

    def deny(path, destination):
        detail = f"network attempt blocked via {path}; destination={destination!r}"
        attempts.append(detail)
        raise NetworkAttemptBlocked(detail)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(
            socket.socket,
            "connect",
            lambda sock, address: deny("socket.socket.connect", address),
        )
        patch.setattr(
            socket.socket,
            "connect_ex",
            lambda sock, address: deny("socket.socket.connect_ex", address),
        )
        patch.setattr(
            socket,
            "create_connection",
            lambda address, *args, **kwargs: deny(
                "socket.create_connection", address
            ),
        )
        patch.setattr(
            socket,
            "getaddrinfo",
            lambda host, service, *args, **kwargs: deny(
                "socket.getaddrinfo", (host, service)
            ),
        )

        from asyncio.base_events import BaseEventLoop
        from asyncio.proactor_events import BaseProactorEventLoop
        from asyncio.selector_events import BaseSelectorEventLoop

        patch.setattr(
            BaseEventLoop,
            "create_connection",
            lambda loop, *args, **kwargs: deny(
                "asyncio.BaseEventLoop.create_connection",
                _destination(args[1:], kwargs) if args else _destination((), kwargs),
            ),
        )
        patch.setattr(
            BaseEventLoop,
            "_connect_sock",
            lambda loop, exceptions, addr_info, *args, **kwargs: deny(
                "asyncio.BaseEventLoop._connect_sock",
                addr_info[4] if len(addr_info) > 4 else addr_info,
            ),
        )
        patch.setattr(
            BaseEventLoop,
            "getaddrinfo",
            lambda loop, host, port, *args, **kwargs: deny(
                "asyncio.BaseEventLoop.getaddrinfo", (host, port)
            ),
        )
        patch.setattr(
            BaseSelectorEventLoop,
            "sock_connect",
            lambda loop, sock, address: deny(
                "asyncio.BaseSelectorEventLoop.sock_connect", address
            ),
        )
        patch.setattr(
            BaseProactorEventLoop,
            "sock_connect",
            lambda loop, sock, address: deny(
                "asyncio.BaseProactorEventLoop.sock_connect", address
            ),
        )
        yield attempts


_ORIGINAL_SOCKET_CONNECT = socket.socket.connect
_ORIGINAL_SOCKET_CONNECT_EX = socket.socket.connect_ex
_ORIGINAL_CREATE_CONNECTION = socket.create_connection
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


@pytest.mark.parametrize(
    ("path", "invoke"),
    [
        (
            "socket.socket.connect",
            lambda: socket.socket.connect(None, ("198.51.100.8", 443)),
        ),
        (
            "socket.socket.connect_ex",
            lambda: socket.socket.connect_ex(None, ("198.51.100.8", 443)),
        ),
        (
            "socket.create_connection",
            lambda: socket.create_connection(("example.invalid", 443)),
        ),
        (
            "socket.getaddrinfo",
            lambda: socket.getaddrinfo("example.invalid", 443),
        ),
    ],
)
def test_network_guard_blocks_socket_paths_with_destination(path, invoke):
    with pytest.raises(NetworkAttemptBlocked) as exc_info:
        with _deny_network_connections() as attempts:
            invoke()
    assert path in str(exc_info.value)
    assert "destination=" in str(exc_info.value)
    assert attempts and path in attempts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "invoke"),
    [
        (
            "asyncio.BaseEventLoop.create_connection",
            lambda loop, address: loop.create_connection(
                lambda: None, address[0], address[1]
            ),
        ),
        (
            "asyncio.BaseEventLoop._connect_sock",
            lambda loop, address: loop._connect_sock(
                [], (socket.AF_INET, socket.SOCK_STREAM, 0, "", address)
            ),
        ),
        (
            "asyncio.BaseEventLoop.getaddrinfo",
            lambda loop, address: loop.getaddrinfo(address[0], address[1]),
        ),
    ],
)
async def test_network_guard_blocks_asyncio_base_paths(path, invoke):
    loop = asyncio.get_running_loop()
    address = ("198.51.100.9", 443)
    with pytest.raises(NetworkAttemptBlocked) as exc_info:
        with _deny_network_connections() as attempts:
            await invoke(loop, address)
    assert path in str(exc_info.value)
    assert "198.51.100.9" in str(exc_info.value)
    assert attempts and path in attempts[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("path", "invoke"),
    [
        (
            "asyncio.BaseSelectorEventLoop.sock_connect",
            lambda address: asyncio.selector_events.BaseSelectorEventLoop.sock_connect(
                None, None, address
            ),
        ),
        (
            "asyncio.BaseProactorEventLoop.sock_connect",
            lambda address: asyncio.proactor_events.BaseProactorEventLoop.sock_connect(
                None, None, address
            ),
        ),
    ],
)
async def test_network_guard_blocks_event_loop_sock_connect(path, invoke):
    address = ("198.51.100.10", 443)
    with pytest.raises(NetworkAttemptBlocked) as exc_info:
        with _deny_network_connections() as attempts:
            await invoke(address)
    assert path in str(exc_info.value)
    assert "198.51.100.10" in str(exc_info.value)
    assert attempts and path in attempts[0]


def test_network_guard_restores_methods_and_is_inactive_after_context(monkeypatch):
    def harmless_connect(sock, address):
        return "original sentinel"

    monkeypatch.setattr(socket.socket, "connect", harmless_connect)
    with pytest.raises(NetworkAttemptBlocked):
        with _deny_network_connections():
            socket.socket.connect(None, ("198.51.100.11", 443))

    # The pre-context implementation is restored and runs normally afterwards.
    assert socket.socket.connect is harmless_connect
    assert socket.socket.connect(None, ("not-a-real-socket", 1)) == "original sentinel"
    assert socket.socket.connect_ex is _ORIGINAL_SOCKET_CONNECT_EX
    assert socket.create_connection is _ORIGINAL_CREATE_CONNECTION
    assert socket.getaddrinfo is _ORIGINAL_GETADDRINFO


def test_network_guard_does_not_contaminate_following_tests():
    assert socket.socket.connect is _ORIGINAL_SOCKET_CONNECT
    assert socket.socket.connect_ex is _ORIGINAL_SOCKET_CONNECT_EX
    assert socket.create_connection is _ORIGINAL_CREATE_CONNECTION
    assert socket.getaddrinfo is _ORIGINAL_GETADDRINFO


@pytest.mark.asyncio
async def test_default_calls_agentfield_once_and_preserves_result(monkeypatch):
    monkeypatch.delenv("SWE_HARNESS_BACKEND", raising=False)
    result = SimpleNamespace(parsed={"ok": True})
    seen = []

    class Agent:
        async def harness(self, *args, **kwargs):
            seen.append((args, kwargs))
            return result

    returned = await dispatch_harness(Agent(), "prompt", model="local")
    assert returned is result
    assert seen == [(('prompt',), {"model": "local"})]
    assert DEFAULT_BACKEND == "agentfield"


@pytest.mark.asyncio
async def test_explicit_handler_receives_typed_request_without_agentfield_fallback(monkeypatch):
    monkeypatch.setenv("SWE_HARNESS_BACKEND", "mock")
    called = []
    expected = object()

    async def mock_handler(agent, request):
        called.append(request)
        return expected

    class Agent:
        async def harness(self, *args, **kwargs):
            raise AssertionError("must not fall back to AgentField")

    returned = await dispatch_harness(
        Agent(), prompt="local prompt", model="fixture", handlers={"mock": mock_handler}
    )
    assert returned is expected
    assert called[0].prompt == "local prompt"
    assert called[0].options["model"] == "fixture"


@pytest.mark.asyncio
async def test_registered_backend_is_selected_at_real_dispatch_boundary(monkeypatch):
    backend_name = f"fixture-{uuid.uuid4().hex}"
    monkeypatch.setenv("SWE_HARNESS_BACKEND", backend_name)
    calls = []

    async def handler(agent, request):
        calls.append(request.prompt)
        return "local-result"

    register_backend(backend_name, handler)

    class Agent:
        async def harness(self, *args, **kwargs):
            raise AssertionError("must not fall back to AgentField")

    assert await dispatch_harness(Agent(), "through-reasoner-boundary") == "local-result"
    assert calls == ["through-reasoner-boundary"]


@pytest.mark.asyncio
async def test_real_fast_planner_uses_registered_local_mock_without_network(
    monkeypatch, tmp_path
):
    monkeypatch.setenv("SWE_HARNESS_BACKEND", "local-mock")

    # Guard before importing the composed app and keep it active through both
    # real reasoner calls. SDK-only intercepts below block its own HTTP client
    # and telemetry routes; this socket guard catches any unanticipated path.
    with _deny_network_connections() as network_attempts:
        from swe_af.fast.app import app as composed_fast_app

        assert composed_fast_app is not None

        # AgentWorkflow._ensure_execution_registered posts through
        # client._async_request; Agent.note uses a separate aiohttp path.
        # These intercept only those SDK boundaries and do not replace the
        # planner, application dispatcher, or local fixture handler.
        intercepted_requests = []
        intercepted_notes = []

        async def block_sdk_request(method, url, **kwargs):
            intercepted_requests.append((method, url))
            raise RuntimeError("test blocked AgentField transport")

        def intercept_sdk_note(message, tags=None):
            intercepted_notes.append((message, tuple(tags or ())))

        monkeypatch.setattr(
            composed_fast_app.client, "_async_request", block_sdk_request
        )
        monkeypatch.setattr(composed_fast_app, "note", intercept_sdk_note)

        output = await fast_plan_tasks(
            goal="local fixture goal",
            repo_path=str(tmp_path),
            ai_provider="opencode",
        )
        validated = FastPlanResult.model_validate(output)
        assert validated.fallback_used is False
        assert validated.tasks[0].name == "fixture-task"
        assert any(
            method == "POST" and url.endswith("/api/v1/workflow/executions")
            for method, url in intercepted_requests
        )
        assert intercepted_notes

        state = DAGState(repo_path=str(tmp_path)).model_dump()
        failure = IssueResult(
            issue_name="fixture-task",
            outcome=IssueOutcome.FAILED_UNRECOVERABLE,
        )
        with pytest.raises(
            MockHarnessError, match="does not execute or emulate tools"
        ):
            await run_replanner(
                dag_state=state,
                failed_issues=[failure.model_dump()],
                ai_provider="opencode",
            )
        assert any(
            method == "POST" and url.endswith("/api/v1/workflow/executions")
            for method, url in intercepted_requests
        )
        assert network_attempts == []


@pytest.mark.asyncio
async def test_fast_planner_surfaces_mock_limit_error(monkeypatch, tmp_path):
    from swe_af.fast.app import app as composed_fast_app

    assert composed_fast_app is not None
    monkeypatch.setenv("SWE_HARNESS_BACKEND", "local-mock")
    with pytest.raises(MockHarnessError, match="exceeds byte limit"):
        await fast_plan_tasks(
            goal="x" * (64 * 1024 + 1),
            repo_path=str(tmp_path),
            ai_provider="opencode",
        )


@pytest.mark.asyncio
async def test_mock_rejects_oversized_input_before_fixture(monkeypatch):
    register_local_mock()
    monkeypatch.setenv("SWE_HARNESS_BACKEND", "local-mock")

    class Agent:
        async def harness(self, *args, **kwargs):
            raise AssertionError("must not fall back to AgentField")

    with pytest.raises(MockHarnessError, match="exceeds byte limit"):
        await dispatch_harness(Agent(), "x" * (64 * 1024 + 1), schema=object)


@pytest.mark.asyncio
async def test_concurrent_mock_dispatches_are_isolated(monkeypatch):
    register_local_mock()
    monkeypatch.setenv("SWE_HARNESS_BACKEND", "local-mock")

    class Agent:
        async def harness(self, *args, **kwargs):
            raise AssertionError("must not fall back to AgentField")

    first, second = await asyncio.gather(
        dispatch_harness(Agent(), "first", schema=FastPlanResult),
        dispatch_harness(Agent(), "second", schema=FastPlanResult),
    )
    assert first.parsed.tasks[0].name == second.parsed.tasks[0].name == "fixture-task"
    assert first is not second


@pytest.mark.asyncio
async def test_mock_rejects_tool_requests_and_unknown_schema(monkeypatch):
    register_local_mock()
    monkeypatch.setenv("SWE_HARNESS_BACKEND", "local-mock")

    class Agent:
        async def harness(self, *args, **kwargs):
            raise AssertionError("must not fall back to AgentField")

    with pytest.raises(MockHarnessError, match="does not execute"):
        await dispatch_harness(Agent(), "prompt", tools=["Bash"])
    with pytest.raises(MockHarnessError, match="no fixture"):
        await dispatch_harness(Agent(), "prompt", schema=type("Unknown", (), {}))


@pytest.mark.asyncio
async def test_unavailable_backend_fails_closed(monkeypatch):
    monkeypatch.setenv("SWE_HARNESS_BACKEND", "mock")

    class Agent:
        async def harness(self, *args, **kwargs):
            raise AssertionError("must not fall back to AgentField")

    with pytest.raises(HarnessBackendUnavailable, match="no fallback"):
        await dispatch_harness(Agent(), "prompt")


@pytest.mark.asyncio
async def test_cancellation_propagates_without_retry(monkeypatch):
    monkeypatch.delenv("SWE_HARNESS_BACKEND", raising=False)
    calls = 0

    class Agent:
        async def harness(self, *args, **kwargs):
            nonlocal calls
            calls += 1
            raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        await dispatch_harness(Agent(), "prompt")
    assert calls == 1
