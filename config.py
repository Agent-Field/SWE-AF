"""Pipeline configuration for the planning agents."""

from __future__ import annotations

from dataclasses import dataclass, field

from agent_ai.types import Tool


@dataclass
class AgentRoleConfig:
    """Configuration for a single agent role."""

    model: str = "sonnet"
    max_turns: int = 15
    allowed_tools: list[str] = field(default_factory=lambda: [
        Tool.READ, Tool.GLOB, Tool.GREP, Tool.BASH,
    ])


@dataclass
class PipelineConfig:
    """Configuration for the full planning pipeline."""

    product_manager: AgentRoleConfig = field(default_factory=lambda: AgentRoleConfig(
        model="sonnet",
        max_turns=15,
        allowed_tools=[Tool.READ, Tool.GLOB, Tool.GREP, Tool.BASH],
    ))
    architect: AgentRoleConfig = field(default_factory=lambda: AgentRoleConfig(
        model="sonnet",
        max_turns=25,
        allowed_tools=[Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP, Tool.BASH],
    ))
    tech_lead: AgentRoleConfig = field(default_factory=lambda: AgentRoleConfig(
        model="sonnet",
        max_turns=15,
        allowed_tools=[Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP],
    ))
    sprint_planner: AgentRoleConfig = field(default_factory=lambda: AgentRoleConfig(
        model="sonnet",
        max_turns=30,
        allowed_tools=[Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP],
    ))
    max_review_iterations: int = 2
    artifacts_dir: str = ".artifacts"
    permission_mode: str | None = None
    env: dict[str, str] = field(default_factory=dict)
