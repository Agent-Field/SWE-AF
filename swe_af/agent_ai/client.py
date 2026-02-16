"""Provider-agnostic AI client facade."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Type

from pydantic import BaseModel

from swe_af.agent_ai.factory import build_provider_client
from swe_af.agent_ai.types import AgentResponse, Tool

DEFAULT_TOOLS: list[str] = [
    Tool.READ,
    Tool.WRITE,
    Tool.EDIT,
    Tool.BASH,
    Tool.GLOB,
    Tool.GREP,
]


@dataclass
class AgentAIConfig:
    """Configuration for AgentAI."""

    provider: Literal["claude", "codex", "opencode"] = "claude"
    codex_bin: str = "codex"
    opencode_bin: str = "opencode"
    model: str = "sonnet"
    cwd: str | Path = "."
    max_turns: int = 10
    allowed_tools: list[str] = field(default_factory=lambda: list(DEFAULT_TOOLS))
    system_prompt: str | None = None
    max_retries: int = 3
    initial_delay: float = 1.0
    max_delay: float = 30.0
    backoff_factor: float = 2.0
    permission_mode: str | None = None
    max_budget_usd: float | None = None
    env: dict[str, str] = field(default_factory=dict)


class AgentAI:
    """Async facade that dispatches requests to the selected provider client."""

    def __init__(self, config: AgentAIConfig | None = None) -> None:
        self.config = config or AgentAIConfig()

    async def run(
        self,
        prompt: str,
        *,
        model: str | None = None,
        cwd: str | Path | None = None,
        max_turns: int | None = None,
        allowed_tools: list[str] | None = None,
        system_prompt: str | None = None,
        output_schema: Type[BaseModel] | None = None,
        max_retries: int | None = None,
        max_budget_usd: float | None = None,
        permission_mode: str | None = None,
        env: dict[str, str] | None = None,
        log_file: str | Path | None = None,
    ) -> AgentResponse[BaseModel]:
        provider_client = build_provider_client(self.config)
        return await provider_client.run(
            prompt,
            model=model,
            cwd=cwd,
            max_turns=max_turns,
            allowed_tools=allowed_tools,
            system_prompt=system_prompt,
            output_schema=output_schema,
            max_retries=max_retries,
            max_budget_usd=max_budget_usd,
            permission_mode=permission_mode,
            env=env,
            log_file=log_file,
        )


# Backward-compatible aliases retained during migration.
ClaudeAI = AgentAI
ClaudeAIConfig = AgentAIConfig
