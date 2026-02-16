"""Provider factory for AgentAI."""

from __future__ import annotations

from typing import TYPE_CHECKING

from swe_af.agent_ai.providers.base import ProviderClient

if TYPE_CHECKING:
    from swe_af.agent_ai.client import AgentAIConfig


def build_provider_client(config: "AgentAIConfig") -> ProviderClient:
    """Build the provider-specific client for the current config."""
    if config.provider == "claude":
        from swe_af.agent_ai.providers.claude import ClaudeProviderClient, ClaudeProviderConfig

        provider_cfg = ClaudeProviderConfig(
            model=config.model,
            cwd=config.cwd,
            max_turns=config.max_turns,
            allowed_tools=list(config.allowed_tools),
            system_prompt=config.system_prompt,
            max_retries=config.max_retries,
            initial_delay=config.initial_delay,
            max_delay=config.max_delay,
            backoff_factor=config.backoff_factor,
            permission_mode=config.permission_mode,
            max_budget_usd=config.max_budget_usd,
            env=dict(config.env),
        )
        return ClaudeProviderClient(provider_cfg)

    if config.provider == "codex":
        from swe_af.agent_ai.providers.codex import CodexProviderClient, CodexProviderConfig

        provider_cfg = CodexProviderConfig(
            codex_bin=config.codex_bin,
            model=config.model,
            cwd=config.cwd,
            max_turns=config.max_turns,
            allowed_tools=list(config.allowed_tools),
            system_prompt=config.system_prompt,
            max_retries=config.max_retries,
            initial_delay=config.initial_delay,
            max_delay=config.max_delay,
            backoff_factor=config.backoff_factor,
            permission_mode=config.permission_mode,
            max_budget_usd=config.max_budget_usd,
            env=dict(config.env),
        )
        return CodexProviderClient(provider_cfg)

    if config.provider == "opencode":
        from swe_af.agent_ai.providers.opencode import (
            OpenCodeProviderClient,
            OpenCodeProviderConfig,
        )

        provider_cfg = OpenCodeProviderConfig(
            opencode_bin=config.opencode_bin,
            model=config.model,
            cwd=config.cwd,
            max_turns=config.max_turns,
            allowed_tools=list(config.allowed_tools),
            system_prompt=config.system_prompt,
            max_retries=config.max_retries,
            initial_delay=config.initial_delay,
            max_delay=config.max_delay,
            backoff_factor=config.backoff_factor,
            permission_mode=config.permission_mode,
            max_budget_usd=config.max_budget_usd,
            env=dict(config.env),
        )
        return OpenCodeProviderClient(provider_cfg)

    raise ValueError(f"Unsupported provider: {config.provider}")
