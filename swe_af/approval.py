"""Approval workflow client for pausing SWE-AF execution into AgentField's waiting state.

Handles requesting human plan approval via the control plane's approval API
and polling until a decision is received.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from agentfield import Agent

logger = logging.getLogger(__name__)


@dataclass
class ApprovalResult:
    """Outcome of a human approval request."""

    decision: str  # "approved", "rejected", "expired", "error"
    feedback: str = ""
    request_id: str = ""
    request_url: str = ""
    raw_response: dict = field(default_factory=dict)

    @property
    def approved(self) -> bool:
        return self.decision == "approved"


class ApprovalClient:
    """Handles pausing an execution into ``waiting`` (approval) via AgentField CP.

    Parameters
    ----------
    agent:
        The AgentField ``Agent`` instance (provides ``agentfield_server``,
        ``api_key``, ``node_id``, and ``note()``).
    project_id:
        The hax-sdk project UUID to create approval requests under.
    """

    # Polling backoff parameters
    INITIAL_POLL_INTERVAL = 5.0  # seconds
    MAX_POLL_INTERVAL = 60.0  # seconds
    BACKOFF_FACTOR = 2.0

    def __init__(self, agent: Agent, project_id: str) -> None:
        self._agent = agent
        self._project_id = project_id
        self._base_url = agent.agentfield_server.rstrip("/")
        self._api_key = agent.api_key or ""

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
    ) -> ApprovalResult:
        """Request human approval for a plan, blocking until resolved.

        1. Calls CP: ``POST /api/v1/agents/{node}/executions/{id}/request-approval``
        2. Polls CP: ``GET .../approval-status`` with exponential backoff
        3. Returns ``ApprovalResult`` with the human's decision
        """
        node_id = self._agent.node_id

        # Build the plan-review-v1 template payload
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
        }

        request_body = {
            "title": "SWE-AF Plan Review",
            "description": "Review the proposed implementation plan before execution begins",
            "template_type": "plan-review-v1",
            "payload": payload,
            "project_id": self._project_id,
            "expires_in_hours": expires_in_hours,
        }

        self._agent.note(
            f"Requesting plan approval for execution {execution_id}",
            tags=["build", "approval", "request"],
        )

        # Call the control plane approval endpoint
        try:
            resp = await self._post(
                f"/api/v1/agents/{node_id}/executions/{execution_id}/request-approval",
                json=request_body,
            )
        except Exception as exc:
            logger.error("Failed to request approval: %s", exc)
            self._agent.note(
                f"Approval request failed: {exc}",
                tags=["build", "approval", "error"],
            )
            return ApprovalResult(
                decision="error",
                feedback=f"Failed to create approval request: {exc}",
            )

        if resp.status_code >= 400:
            error_detail = resp.text[:500]
            logger.error(
                "Approval request returned %d: %s", resp.status_code, error_detail
            )
            self._agent.note(
                f"Approval request returned HTTP {resp.status_code}",
                tags=["build", "approval", "error"],
            )
            return ApprovalResult(
                decision="error",
                feedback=f"HTTP {resp.status_code}: {error_detail}",
            )

        data = resp.json()
        request_id = data.get("approval_request_id", "")
        request_url = data.get("approval_request_url", "")

        # Save pending state immediately so resume_build() can pick up polling
        # if the process crashes while waiting.
        if on_request_created:
            on_request_created(request_id, request_url)

        self._agent.note(
            f"Approval requested — waiting for human review at {request_url}",
            tags=["build", "approval", "waiting"],
        )

        # Poll until resolved
        result = await self._poll_approval(execution_id, request_id, request_url)
        return result

    async def _poll_approval(
        self,
        execution_id: str,
        request_id: str,
        request_url: str,
    ) -> ApprovalResult:
        """Poll approval status with exponential backoff until resolved."""
        node_id = self._agent.node_id
        interval = self.INITIAL_POLL_INTERVAL

        while True:
            await asyncio.sleep(interval)

            try:
                resp = await self._get(
                    f"/api/v1/agents/{node_id}/executions/{execution_id}/approval-status",
                )
            except Exception as exc:
                logger.warning("Approval poll failed (will retry): %s", exc)
                interval = min(interval * self.BACKOFF_FACTOR, self.MAX_POLL_INTERVAL)
                continue

            if resp.status_code >= 400:
                logger.warning(
                    "Approval poll returned %d (will retry)", resp.status_code
                )
                interval = min(interval * self.BACKOFF_FACTOR, self.MAX_POLL_INTERVAL)
                continue

            data = resp.json()
            status = data.get("status", "unknown")

            if status == "pending":
                # Still waiting — back off
                interval = min(interval * self.BACKOFF_FACTOR, self.MAX_POLL_INTERVAL)
                continue

            # Terminal states: approved, rejected, expired
            feedback = ""
            raw_response = {}
            if data.get("response"):
                try:
                    import json
                    raw_response = json.loads(data["response"]) if isinstance(data["response"], str) else data["response"]
                    feedback = raw_response.get("feedback", "")
                except (json.JSONDecodeError, AttributeError):
                    feedback = str(data["response"])

            decision = status  # approved, rejected, expired
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

    async def get_approval_status(self, execution_id: str) -> dict:
        """One-shot poll of approval status (no blocking/retry)."""
        node_id = self._agent.node_id
        resp = await self._get(
            f"/api/v1/agents/{node_id}/executions/{execution_id}/approval-status",
        )
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------ #
    # HTTP helpers                                                        #
    # ------------------------------------------------------------------ #

    def _headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._api_key:
            headers["X-API-Key"] = self._api_key
        return headers

    async def _post(self, path: str, **kwargs) -> httpx.Response:
        url = self._base_url + path
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.post(url, headers=self._headers(), **kwargs)

    async def _get(self, path: str, **kwargs) -> httpx.Response:
        url = self._base_url + path
        async with httpx.AsyncClient(timeout=30.0) as client:
            return await client.get(url, headers=self._headers(), **kwargs)
