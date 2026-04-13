"""Approval workflow client for pausing SWE-AF execution for human review.

Calls hax-sdk directly to create approval requests, then notifies the
AgentField control plane to transition execution state to "waiting".
When the approval resolves (via hax-sdk webhook), notifies the CP to
transition back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from aiohttp import web
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from agentfield import Agent

logger = logging.getLogger(__name__)

# Environment variables for hax-sdk integration
HAX_API_KEY_ENV = "HAX_API_KEY"
HAX_SDK_URL_ENV = "HAX_SDK_URL"
HAX_SDK_URL_DEFAULT = "http://localhost:3000"


def is_approval_enabled() -> bool:
    """Check whether approval is enabled (HAX_API_KEY is set)."""
    return bool(os.environ.get(HAX_API_KEY_ENV, "").strip())


@dataclass
class ApprovalResult:
    """Outcome of a human approval request."""

    decision: str  # "approved", "rejected", "request_changes", "expired", "error"
    feedback: str = ""
    request_id: str = ""
    request_url: str = ""
    raw_response: dict = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.decision == "approved"

    @property
    def changes_requested(self) -> bool:
        return self.decision == "request_changes"


def _find_free_port() -> int:
    """Find an available TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _CallbackServer:
    """Lightweight HTTP server that receives a single approval callback then shuts down."""

    def __init__(self) -> None:
        self._event: asyncio.Event = asyncio.Event()
        self._result: dict | None = None
        self._port: int = 0
        self._runner: web.AppRunner | None = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}/approval-callback"

    async def start(self) -> str:
        """Start the callback server and return its URL."""
        app = web.Application()
        app.router.add_post("/approval-callback", self._handle)

        self._port = _find_free_port()
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", self._port)
        await site.start()
        logger.info("Approval callback server listening on %s", self.url)
        return self.url

    async def _handle(self, request: web.Request) -> web.Response:
        """Handle the approval callback from hax-sdk."""
        try:
            self._result = await request.json()
            logger.info("Received approval callback: %s", self._result)
        except Exception:
            self._result = {"decision": "error", "feedback": "malformed callback payload"}
        self._event.set()
        return web.json_response({"status": "received"})

    async def wait(self, timeout: float) -> dict | None:
        """Wait for the callback. Returns the payload or None on timeout."""
        try:
            await asyncio.wait_for(self._event.wait(), timeout=timeout)
            return self._result
        except asyncio.TimeoutError:
            return None

    async def stop(self) -> None:
        """Shut down the callback server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None


class ApprovalClient:
    """Handles approval workflow: hax-sdk for requests, CP for execution state.

    Parameters
    ----------
    agent:
        The AgentField ``Agent`` instance (provides ``agentfield_server``,
        ``api_key``, ``node_id``, and ``note()``).
    """

    # Default timeout for waiting on the callback (matches default expiry)
    DEFAULT_TIMEOUT = 72 * 3600  # 72 hours in seconds

    def __init__(self, agent: Agent) -> None:
        self._agent = agent
        self._cp_base_url = agent.agentfield_server.rstrip("/")
        self._cp_api_key = agent.api_key or ""
        self._hax_api_key = os.environ.get(HAX_API_KEY_ENV, "")
        self._hax_sdk_url = os.environ.get(HAX_SDK_URL_ENV, HAX_SDK_URL_DEFAULT).rstrip("/")

    async def request_plan_approval(
        self,
        execution_id: str,
        plan_summary: str,
        issues: list[dict],
        architecture: str,
        prd: str,
        goal_description: str = "",
        repo_url: str = "",
        expires_in_hours: int = 72,
        on_request_created: callable | None = None,
        revision_number: int = 0,
        revision_history: list[dict] | None = None,
    ) -> ApprovalResult:
        """Request human approval for a plan.

        1. Starts a local callback server
        2. Calls hax-sdk directly to create the approval request
        3. Notifies CP to transition execution to "waiting"
        4. Waits for hax-sdk webhook callback
        5. Notifies CP to resolve the approval
        """
        node_id = self._agent.node_id

        # Build the plan-review-v2 template payload
        payload = {
            "planSummary": plan_summary,
            "issues": issues,
            "architecture": architecture,
            "prd": prd,
            "metadata": {
                "repoUrl": repo_url,
                "goalDescription": goal_description,
                "agentNodeId": node_id,
                "executionId": execution_id,
            },
            "revisionNumber": revision_number,
            "revisionHistory": revision_history or [],
        }

        # Start callback server for hax-sdk webhook
        callback = _CallbackServer()
        callback_url = await callback.start()

        title = "SWE-AF Plan Review"
        if revision_number > 0:
            title = f"SWE-AF Plan Review (Revision {revision_number})"

        # --- Step 1: Create request on hax-sdk directly ---
        hax_request_body = {
            "title": title,
            "description": "Review the proposed implementation plan before execution begins",
            "type": "plan-review-v2",
            "payload": payload,
            "webhookUrl": callback_url,
            "expiresInSeconds": expires_in_hours * 3600,
        }

        # Optionally assign the request to a specific Hub user
        approval_user_id = os.environ.get("AGENTFIELD_APPROVAL_USER_ID", "")
        if approval_user_id:
            hax_request_body["userId"] = approval_user_id

        self._agent.note(
            f"Requesting plan approval for execution {execution_id}",
            tags=["build", "approval", "request"],
        )

        try:
            hax_resp = await self._hax_post("/api/v1/requests", json=hax_request_body)
        except Exception as exc:
            await callback.stop()
            logger.error("Failed to create approval request on hax-sdk: %s", exc)
            self._agent.note(
                f"Approval request failed: {exc}",
                tags=["build", "approval", "error"],
            )
            return ApprovalResult(
                decision="error",
                feedback=f"Failed to create approval request: {exc}",
            )

        if hax_resp.status_code >= 400:
            await callback.stop()
            error_detail = hax_resp.text[:500]
            logger.error(
                "hax-sdk returned %d: %s", hax_resp.status_code, error_detail
            )
            self._agent.note(
                f"hax-sdk returned HTTP {hax_resp.status_code}",
                tags=["build", "approval", "error"],
            )
            return ApprovalResult(
                decision="error",
                feedback=f"hax-sdk HTTP {hax_resp.status_code}: {error_detail}",
            )

        hax_data = hax_resp.json()
        request_id = hax_data.get("id", "")
        request_url = hax_data.get("url", "")

        # --- Step 2: Tell CP to transition execution to "waiting" ---
        try:
            cp_body = {
                "approval_request_id": request_id,
                "approval_request_url": request_url,
                "expires_in_hours": expires_in_hours,
            }
            cp_resp = await self._cp_post(
                f"/api/v1/executions/{execution_id}/request-approval",
                json=cp_body,
            )
            if cp_resp.status_code >= 400:
                logger.warning(
                    "CP request-approval returned %d: %s (non-fatal)",
                    cp_resp.status_code, cp_resp.text[:200],
                )
        except Exception as exc:
            logger.warning("Failed to notify CP of waiting state: %s (non-fatal)", exc)

        # Notify caller of request creation (for state persistence)
        if on_request_created:
            on_request_created(request_id, request_url)

        self._agent.note(
            f"Approval requested — waiting for webhook callback at {callback_url}",
            tags=["build", "approval", "waiting"],
        )

        # --- Step 3: Wait for hax-sdk webhook callback ---
        timeout = expires_in_hours * 3600
        try:
            result = await self._wait_for_callback(
                callback, execution_id, request_id, request_url, timeout
            )
        finally:
            await callback.stop()

        # --- Step 4: Tell CP to resolve the approval state ---
        await self._notify_cp_resolution(request_id, result)

        return result

    async def _wait_for_callback(
        self,
        callback: _CallbackServer,
        execution_id: str,
        request_id: str,
        request_url: str,
        timeout: float,
    ) -> ApprovalResult:
        """Wait for the hax-sdk webhook callback, falling back to status poll."""
        cb_data = await callback.wait(timeout)

        if cb_data is not None:
            return self._parse_hax_webhook(cb_data, request_id, request_url)

        # Timeout — fall back to one status poll via CP
        logger.warning("Callback timed out after %ds — falling back to status poll", timeout)
        return await self._fallback_poll(execution_id, request_id, request_url)

    def _parse_hax_webhook(self, data: dict, request_id: str, request_url: str) -> ApprovalResult:
        """Parse the hax-sdk webhook payload into an ApprovalResult.

        Supports two formats:
        1. hax-sdk envelope: {"type":"completed","data":{"requestId":"...","response":{"decision":"approved"}}}
        2. Flat format: {"decision":"approved","feedback":"..."}
        """
        decision = ""
        feedback = ""
        raw_response = {}

        # Try hax-sdk envelope format
        event_data = data.get("data")
        if event_data and isinstance(event_data, dict):
            response_obj = event_data.get("response", {})
            if isinstance(response_obj, dict):
                decision = response_obj.get("decision", "")
                feedback = response_obj.get("feedback", "")
                raw_response = response_obj

            # Handle "expired" event type
            if data.get("type") == "expired":
                decision = "expired"
        else:
            # Flat format
            decision = data.get("decision", "error")
            feedback = data.get("feedback", "")
            response_field = data.get("response")
            if response_field:
                try:
                    raw_response = json.loads(response_field) if isinstance(response_field, str) else response_field
                    if not feedback:
                        feedback = raw_response.get("feedback", "")
                except (json.JSONDecodeError, AttributeError):
                    if not feedback:
                        feedback = str(response_field)

        if not decision:
            decision = "error"

        self._agent.note(
            f"Approval resolved: {decision}"
            + (f" — feedback: {feedback[:200]}" if feedback else ""),
            tags=["build", "approval", decision],
        )

        return ApprovalResult(
            decision=decision,
            feedback=feedback,
            request_id=request_id,
            request_url=request_url,
            raw_response=raw_response,
        )

    async def _notify_cp_resolution(self, request_id: str, result: ApprovalResult) -> None:
        """Notify the CP that the approval has resolved (best-effort)."""
        try:
            body = {
                "requestId": request_id,
                "decision": result.decision,
                "feedback": result.feedback,
            }
            if result.raw_response:
                body["response"] = json.dumps(result.raw_response)

            resp = await self._cp_post("/api/v1/webhooks/approval-response", json=body)
            if resp.status_code >= 400:
                logger.warning(
                    "CP approval-response returned %d: %s",
                    resp.status_code, resp.text[:200],
                )
        except Exception as exc:
            logger.warning("Failed to notify CP of approval resolution: %s", exc)

    async def _fallback_poll(
        self,
        execution_id: str,
        request_id: str,
        request_url: str,
    ) -> ApprovalResult:
        """Single status poll as fallback when callback doesn't arrive."""
        try:
            resp = await self._cp_get(
                f"/api/v1/executions/{execution_id}/approval-status",
            )
            if resp.status_code < 400:
                data = resp.json()
                status = data.get("status", "unknown")
                if status != "pending":
                    return ApprovalResult(
                        decision=status,
                        feedback="",
                        request_id=request_id,
                        request_url=request_url,
                    )
        except Exception as exc:
            logger.warning("Fallback poll failed: %s", exc)

        return ApprovalResult(
            decision="expired",
            feedback="Approval timed out without response",
            request_id=request_id,
            request_url=request_url,
        )

    async def wait_for_approval(
        self,
        execution_id: str,
        request_id: str,
        request_url: str = "",
        timeout: float | None = None,
    ) -> ApprovalResult:
        """Resume waiting for an approval (e.g. after crash recovery).

        Starts a callback server and waits for the result.
        Falls back to polling if the callback doesn't arrive.
        """
        callback = _CallbackServer()
        await callback.start()

        effective_timeout = timeout or self.DEFAULT_TIMEOUT

        try:
            result = await self._wait_for_callback(
                callback, execution_id, request_id, request_url, effective_timeout
            )
        finally:
            await callback.stop()

        # Notify CP of resolution
        await self._notify_cp_resolution(request_id, result)

        return result

    async def get_approval_status(self, execution_id: str) -> dict:
        """One-shot poll of approval status (no blocking/retry)."""
        resp = await self._cp_get(
            f"/api/v1/executions/{execution_id}/approval-status",
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                        #
    # ------------------------------------------------------------------ #

    def _cp_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._cp_api_key:
            headers["X-API-Key"] = self._cp_api_key
        return headers

    def _hax_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._hax_api_key:
            headers["Authorization"] = f"Bearer {self._hax_api_key}"
        return headers

    async def _cp_post(self, path: str, **kwargs) -> httpx.Response:
        url = self._cp_base_url + path
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(url, headers=self._cp_headers(), **kwargs)

    async def _cp_get(self, path: str, **kwargs) -> httpx.Response:
        url = self._cp_base_url + path
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.get(url, headers=self._cp_headers(), **kwargs)

    async def _hax_post(self, path: str, **kwargs) -> httpx.Response:
        url = self._hax_sdk_url + path
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(url, headers=self._hax_headers(), **kwargs)
