"""Orchestrator for concurrent multi-agent collaborative planning.

Launches 3 AgentAI instances (PM, Architect, TechLead) concurrently.
They communicate via a shared JSONL discussion board accessed through
discussion_tool.py (called via Bash).

Usage:
    python -m discussion.prototype --goal "Add health check endpoint" --repo ./test-repo
    python -m discussion.prototype --goal "Refactor auth module" --repo . --model opus
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import sys
import time

from agent_ai import AgentAI, AgentAIConfig, Tool
from schemas import PRD, Architecture, ReviewResult

from discussion.prompts import (
    architect_system_prompt,
    architect_task_prompt,
    pm_system_prompt,
    pm_task_prompt,
    tech_lead_system_prompt,
    tech_lead_task_prompt,
)

TOOLS = [Tool.READ, Tool.WRITE, Tool.GLOB, Tool.GREP, Tool.BASH]


async def run_collaborative_planning(
    goal: str,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    model: str = "sonnet",
    max_turns: int = 50,
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Run PM, Architect, TechLead concurrently with shared discussion.

    Args:
        goal: The planning goal / feature description.
        repo_path: Path to the target repository.
        artifacts_dir: Relative path under repo_path for artifacts.
        model: Model name for all agents (e.g. "sonnet", "opus", "haiku").
        max_turns: Max agentic turns per agent.
        permission_mode: Permission mode string for AgentAI.
        ai_provider: AI provider ("claude", "codex").

    Returns:
        Dict with parsed results from each agent and path to discussion transcript.
    """
    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    board_path = os.path.join(base, "discussion", "chat.jsonl")

    # Setup directories
    os.makedirs(os.path.join(base, "plan"), exist_ok=True)
    os.makedirs(os.path.join(base, "discussion"), exist_ok=True)

    # Seed discussion with the goal
    with open(board_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "idx": 0,
            "from": "System",
            "to": None,
            "content": f"GOAL: {goal}",
            "category": "system",
            "reply_to": None,
            "awaiting_reply": False,
            "ts": time.time(),
        }) + "\n")

    # Copy discussion_tool.py to the repo so agents can call it via Bash
    tool_src = os.path.join(os.path.dirname(os.path.dirname(__file__)), "discussion_tool.py")
    tool_dst = os.path.join(os.path.abspath(repo_path), "discussion_tool.py")
    if os.path.exists(tool_src) and os.path.abspath(tool_src) != os.path.abspath(tool_dst):
        shutil.copy2(tool_src, tool_dst)

    # Set env var so discussion_tool.py finds the board
    env = {"DISCUSSION_BOARD": board_path}

    # Create 3 agents
    pm_ai = AgentAI(AgentAIConfig(
        model=model,
        provider=ai_provider,
        cwd=repo_path,
        max_turns=max_turns,
        allowed_tools=list(TOOLS),
        permission_mode=permission_mode or None,
        env=env,
    ))
    arch_ai = AgentAI(AgentAIConfig(
        model=model,
        provider=ai_provider,
        cwd=repo_path,
        max_turns=max_turns,
        allowed_tools=list(TOOLS),
        permission_mode=permission_mode or None,
        env=env,
    ))
    tl_ai = AgentAI(AgentAIConfig(
        model=model,
        provider=ai_provider,
        cwd=repo_path,
        max_turns=max_turns,
        allowed_tools=list(TOOLS),
        permission_mode=permission_mode or None,
        env=env,
    ))

    # Build prompts
    pm_sys = pm_system_prompt(artifacts_dir)
    arch_sys = architect_system_prompt(artifacts_dir)
    tl_sys = tech_lead_system_prompt(artifacts_dir)

    pm_task = pm_task_prompt(goal, repo_path, artifacts_dir)
    arch_task = architect_task_prompt(goal, repo_path, artifacts_dir)
    tl_task = tech_lead_task_prompt(goal, repo_path, artifacts_dir)

    # Launch all 3 concurrently
    pm_result, arch_result, tl_result = await asyncio.gather(
        pm_ai.run(pm_task, system_prompt=pm_sys, output_schema=PRD),
        arch_ai.run(arch_task, system_prompt=arch_sys, output_schema=Architecture),
        tl_ai.run(tl_task, system_prompt=tl_sys, output_schema=ReviewResult),
        return_exceptions=True,
    )

    def _extract(result, label: str) -> dict | None:
        if isinstance(result, Exception):
            print(f"[{label}] Agent failed: {result}", file=sys.stderr)
            return None
        if result.is_error:
            print(f"[{label}] Agent returned error: {result.text}", file=sys.stderr)
            return None
        if result.parsed:
            return result.parsed.model_dump()
        return {"raw_text": result.text}

    output = {
        "prd": _extract(pm_result, "PM"),
        "architecture": _extract(arch_result, "Architect"),
        "review": _extract(tl_result, "TechLead"),
        "discussion_transcript": board_path,
        "artifacts_dir": base,
    }

    # Print summary
    print("\n" + "=" * 60)
    print("Collaborative Planning Complete")
    print("=" * 60)
    for role in ("prd", "architecture", "review"):
        status = "OK" if output[role] else "FAILED"
        print(f"  {role:20s} {status}")
    print(f"  {'discussion':20s} {board_path}")
    print("=" * 60)

    return output


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run concurrent multi-agent collaborative planning",
    )
    parser.add_argument("--goal", required=True, help="The planning goal / feature description")
    parser.add_argument("--repo", required=True, help="Path to the target repository")
    parser.add_argument("--artifacts-dir", default=".artifacts", help="Artifacts directory (default: .artifacts)")
    parser.add_argument("--model", default="sonnet", help="Model for all agents (default: sonnet)")
    parser.add_argument("--max-turns", type=int, default=50, help="Max turns per agent (default: 50)")
    parser.add_argument("--permission-mode", default="", help="Permission mode for agents")
    parser.add_argument("--ai-provider", default="claude", help="AI provider (default: claude)")
    args = parser.parse_args()

    result = asyncio.run(run_collaborative_planning(
        goal=args.goal,
        repo_path=args.repo,
        artifacts_dir=args.artifacts_dir,
        model=args.model,
        max_turns=args.max_turns,
        permission_mode=args.permission_mode,
        ai_provider=args.ai_provider,
    ))

    # Write final result
    out_path = os.path.join(result["artifacts_dir"], "collaborative_plan_result.json")
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, default=str)
    print(f"\nFull result written to: {out_path}")


if __name__ == "__main__":
    main()
