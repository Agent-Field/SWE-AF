"""Prompt builder for the Architect agent role."""

from __future__ import annotations

import os
import time

from swe_af.reasoners.schemas import PRD

SYSTEM_PROMPT = """\
You are a senior Software Architect whose designs ship on time because they are
exactly as complex as the problem demands — no more, no less. Teams trust your
architecture documents because every decision is justified, every interface is
precise, and every component earns its existence.

## Your Responsibilities

You own the technical blueprint. Your architecture document becomes the single
source of truth that every downstream engineer and agent works from. If two
engineers independently implement components using only your document, their
code should integrate cleanly on the first attempt. Ambiguous interfaces, vague
responsibilities, or hand-wavy "figure it out later" sections are failures of
your craft.

## What Makes You Exceptional

You study the existing codebase obsessively before designing anything. The code
tells you which patterns to follow, which conventions to respect, and where the
natural extension points are. Your designs feel like a natural evolution of what
already exists, not a foreign transplant.

You make trade-offs visible. Every significant decision includes: what you chose,
what you rejected, why, and what the consequences are. An engineer reading your
document understands not just WHAT to build, but WHY this approach and not the
obvious alternatives.

## Your Quality Standards

- **Interface precision**: Every public interface is defined with exact signatures,
  parameter types, return types, and error cases. These definitions are canonical —
  they will be copied verbatim into implementation code. Never leave types or
  signatures as "TBD."
- **Data flow clarity**: For every operation, the path from input to output is
  traceable through your architecture. Include concrete data flow examples with
  real values showing how data transforms at each layer.
- **Error flow as first-class**: Error paths are designed with the same rigor as
  happy paths. Define error types, propagation strategy, and where each error
  category originates.
- **Performance budgets**: When performance matters, break down the target budget
  across components. "< 100μs total" becomes "~15μs parsing + ~5μs context + ~10μs
  evaluation + 70μs margin." Include fallback optimization strategies if budgets
  are missed.
- **Extension points without premature implementation**: Document where future
  capabilities will plug in, but do NOT implement hooks, abstractions, or
  indirection for them. Show the migration path, not the scaffolding.
- **Dependency justification**: Every external dependency earns its inclusion.
  State what it provides, why you can't reasonably build it, and what the cost is
  (compile time, binary size, maintenance risk).

## Parallel Agent Execution Constraints

Your architecture is decomposed into issues executed by isolated agents in
parallel git worktrees:

- **File boundary = isolation boundary**: Components built by different agents
  MUST live in different files. Two parallel issues modifying the same file
  creates merge conflicts — restructure to give each issue distinct files.
- **Shared types module first**: Define ALL cross-component types (error enums,
  data structures, config types) in a foundational module built before anything
  else. All other modules import from it. This eliminates type duplication.
- **Interface contracts are the ONLY coordination**: Parallel agents each read
  YOUR document and implement to the interfaces you define. Be exact with
  signatures, types, and error variants — or agents will produce incompatible code.
- **Explicit module dependency graph**: For each component, list which other
  components it imports from. This maps directly to the execution DAG.\
"""


def _poll_for_prd_markdown(prd_path: str) -> str:
    """Poll for PRD file with exponential backoff.

    Args:
        prd_path: Path to PRD markdown file to poll for.

    Returns:
        PRD markdown content if file found, empty string if timeout.
    """
    max_wait = 120  # 2 minutes
    poll_interval = 0.5  # Start with 500ms
    elapsed = 0.0

    while elapsed < max_wait:
        if os.path.exists(prd_path):
            # File exists — wait additional 200ms for write completion
            time.sleep(0.2)
            try:
                with open(prd_path, "r", encoding="utf-8") as f:
                    prd_markdown = f.read()
                return prd_markdown
            except Exception:
                # If read fails, continue polling
                pass

        time.sleep(poll_interval)
        elapsed += poll_interval
        poll_interval = min(poll_interval * 1.5, 5.0)  # Exponential backoff, cap at 5s

    # Timeout — return empty string for graceful degradation
    return ""


def architect_prompts(
    *,
    prd: PRD | None = None,
    repo_path: str,
    prd_path: str | None = None,
    architecture_path: str,
    feedback: str | None = None,
) -> tuple[str, str]:
    """Return (system_prompt, task_prompt) for the architect.

    Args:
        prd: PRD object. If None, will poll for prd_path.
        repo_path: Path to repository.
        prd_path: Path to PRD file. If prd is None, polls for this file.
        architecture_path: Path to write architecture document.
        feedback: Optional feedback from Tech Lead.

    Returns:
        Tuple of (system_prompt, task_prompt)
    """
    # Poll for PRD file if prd is None but prd_path is provided
    prd_markdown = ""
    if prd is None and prd_path is not None:
        prd_markdown = _poll_for_prd_markdown(prd_path)

    # Format PRD content based on what we have
    if prd is not None:
        # Structured PRD object - use formatted fields
        ac_formatted = "\n".join(f"- {c}" for c in prd.acceptance_criteria)
        must_have = "\n".join(f"- {m}" for m in prd.must_have)
        out_of_scope = "\n".join(f"- {o}" for o in prd.out_of_scope)
        prd_description = prd.validated_description
    else:
        # Use markdown content from polled file (or empty if timeout)
        ac_formatted = ""
        must_have = ""
        out_of_scope = ""
        prd_description = prd_markdown

    feedback_block = ""
    if feedback:
        feedback_block = f"""
## Revision Feedback from Tech Lead
The previous architecture was reviewed and needs revision:
{feedback}
Address these concerns directly.
"""

    # Build task prompt based on PRD format
    if prd is not None:
        # Structured format
        task = f"""\
## Product Requirements
{prd_description}

## Acceptance Criteria
{ac_formatted}

## Scope
- Must have:
{must_have}
- Out of scope:
{out_of_scope}

## Repository
{repo_path}

The full PRD is at: {prd_path}
{feedback_block}
## Your Mission

Design the technical architecture. Read the codebase deeply first — your design
should feel like a natural extension of what already exists.

Write your architecture document to: {architecture_path}

The bar: this document is the single source of truth. Every interface you define
will be copied verbatim into code. Every type signature becomes a real type. Every
component boundary becomes a real module. Two engineers working independently from
this document should produce code that integrates on the first try.
"""
    else:
        # Markdown format (from polled file or empty if timeout)
        prd_reference = f"The full PRD is at: {prd_path}" if prd_path else ""
        task = f"""\
## Product Requirements
{prd_description}

## Repository
{repo_path}

{prd_reference}
{feedback_block}
## Your Mission

Design the technical architecture. Read the codebase deeply first — your design
should feel like a natural extension of what already exists.

Write your architecture document to: {architecture_path}

The bar: this document is the single source of truth. Every interface you define
will be copied verbatim into code. Every type signature becomes a real type. Every
component boundary becomes a real module. Two engineers working independently from
this document should produce code that integrates on the first try.
"""
    return SYSTEM_PROMPT, task
