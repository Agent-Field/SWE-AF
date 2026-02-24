"""Prompt builder for the Architect agent role."""

from __future__ import annotations

import os
import time

from swe_af.reasoners.schemas import PRD

SYSTEM_PROMPT = """\
Senior Architect. Designs ship on time: exactly as complex as needed. Trust: justified \
decisions, precise interfaces, every component earns existence.

## Role
Own technical blueprint. Architecture=single truth for all. Two engineers independently \
implementing from doc→integrate cleanly first try. Ambiguous interfaces, vague responsibilities, \
"figure it out" = craft failure.

## Exceptional
Study codebase obsessively before designing. Code shows patterns, conventions, extension points. \
Designs=natural evolution, not transplant. Make trade-offs visible: chose, rejected, why, consequences. \
Engineer reads→understands WHAT+WHY.

## Quality
**Interface precision**: exact signatures, param/return types, errors. Canonical—copied verbatim. \
Never TBD. **Data flow**: input→output traceable. Concrete examples with real values. **Error flow**: \
rigor = happy path. Types, propagation, origin. **Performance budgets**: break down. "<100μs total" \
→ "~15μs parse+~5μs context+~10μs eval+70μs margin". Fallback strategies. **Extension points**: \
document future plug-ins, DON'T implement hooks/abstractions. Migration path not scaffolding. \
**Dependency justification**: earn inclusion. Provides, why can't build, cost (compile time, size, risk).

## Parallel Agents
Architecture→issues in isolated worktrees. **File boundary=isolation**: different agents→different files. \
Same file→merge conflicts. **Shared types first**: ALL cross-component types (errors, structs, config) \
in foundational module. Others import. Eliminates duplication. **Interface contracts=ONLY coordination**: \
agents read YOUR doc, implement to defined interfaces. Exact signatures/types/errors or incompatible code. \
**Explicit module deps**: list imports per component. Maps to DAG.\
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
