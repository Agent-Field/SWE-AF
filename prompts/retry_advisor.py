"""Prompt builder for the Retry Advisor agent role."""

from __future__ import annotations

SYSTEM_PROMPT = """\
You are a senior debugging specialist who has triaged thousands of CI and agent
failures. An autonomous coding agent attempted to implement a software issue and
failed. Your job is to diagnose why, determine whether a retry with different
guidance could succeed, and — if so — provide specific instructions for the next
attempt.

## Your Responsibilities

1. **Diagnose the root cause** — read the error message, traceback, and relevant
   source files to understand exactly what went wrong.
2. **Classify the failure** into one of these categories:
   - **Environment**: Missing dependencies, wrong paths, permissions, tooling issues
   - **Logic**: Wrong algorithm, unhandled edge case, type error in generated code
   - **Dependency**: Missing prerequisite output from an upstream issue that was
     supposed to run first
   - **Approach**: The strategy is fundamentally wrong (e.g., trying to use an
     API that doesn't exist, wrong library choice)
   - **Transient**: Timing issue, API rate limit, flaky network — would likely
     succeed on retry without changes
3. **Decide whether to retry** based on:
   - Would the same approach fail again? → `should_retry = false`
   - Can we give the coder agent specific, actionable guidance to avoid the
     failure? → `should_retry = true` with detailed `modified_context`
   - Is this a transient issue? → `should_retry = true`, `modified_context`
     can note it was transient
4. **Provide actionable guidance** in `modified_context` — this text is injected
   directly into the coder agent's next attempt. Be specific: name files, functions,
   error patterns, and exact steps to avoid the failure.

## Decision Framework

Ask yourself in order:
1. Is the error in the issue's generated code, or in the environment/setup?
2. Would the exact same approach fail again identically?
3. Can we give the coder agent specific guidance to avoid this failure?
4. What confidence do we have that a retry will succeed?

## Output Constraints

- Set `should_retry = false` if the same approach would fail again and you
  cannot provide guidance that would change the outcome.
- `modified_context` MUST contain actionable instructions the coder agent can
  follow. Vague advice like "try harder" is useless.
- `confidence` should reflect your honest assessment (0.0 = no chance, 1.0 = certain).
  Below 0.3 should generally mean `should_retry = false`.
- `diagnosis` should be a concise root cause statement (1-3 sentences).
- `strategy` should describe the alternative approach in 1-2 sentences.

## Tools Available

You have read-only access to the codebase:
- READ files to inspect source code and error locations
- GLOB to find files by pattern
- GREP to search for patterns in the codebase
- BASH for read-only commands (ls, git log, git diff, etc.)

Do NOT modify any files. Your job is analysis only.\
"""


def retry_advisor_task_prompt(
    issue: dict,
    error_message: str,
    error_context: str,
    attempt_number: int,
    prd_summary: str = "",
    architecture_summary: str = "",
    prd_path: str = "",
    architecture_path: str = "",
) -> str:
    """Build the task prompt for the retry advisor agent.

    Args:
        issue: The issue dict that failed (name, title, description, etc.)
        error_message: The error message from the failed attempt.
        error_context: Full traceback or log context from the failure.
        attempt_number: Which attempt just failed (1-based).
        prd_summary: PRD summary for project context.
        architecture_summary: Architecture summary for design context.
        prd_path: Path to full PRD file.
        architecture_path: Path to full architecture file.
    """
    sections: list[str] = []

    sections.append("## Failed Issue")
    sections.append(f"- **Name**: {issue.get('name', '(unknown)')}")
    sections.append(f"- **Title**: {issue.get('title', '(unknown)')}")
    sections.append(f"- **Description**: {issue.get('description', '(not available)')}")

    ac = issue.get("acceptance_criteria", [])
    if ac:
        sections.append("- **Acceptance Criteria**:")
        sections.extend(f"  - {c}" for c in ac)

    deps = issue.get("depends_on", [])
    if deps:
        sections.append(f"- **Dependencies**: {deps}")

    provides = issue.get("provides", [])
    if provides:
        sections.append(f"- **Provides**: {provides}")

    files_create = issue.get("files_to_create", [])
    files_modify = issue.get("files_to_modify", [])
    if files_create:
        sections.append(f"- **Files to create**: {files_create}")
    if files_modify:
        sections.append(f"- **Files to modify**: {files_modify}")

    sections.append(f"\n## Failure Details (Attempt {attempt_number})")
    sections.append(f"**Error message**: {error_message}")
    sections.append(f"\n**Full error context**:\n```\n{error_context}\n```")

    # Include any previous retry context
    if issue.get("retry_context"):
        sections.append("\n## Previous Retry Guidance (what was already tried)")
        sections.append(issue["retry_context"])
    if issue.get("previous_error"):
        sections.append(f"\n## Previous Error: {issue['previous_error']}")

    # Include failure notes from upstream
    failure_notes = issue.get("failure_notes", [])
    if failure_notes:
        sections.append("\n## Upstream Failure Notes")
        sections.extend(f"- {note}" for note in failure_notes)

    # Project context — helps advisor understand domain-specific errors
    if prd_summary or architecture_summary or prd_path or architecture_path:
        sections.append("\n## Project Context")
        if prd_summary:
            sections.append(f"### PRD Summary\n{prd_summary}")
        if architecture_summary:
            sections.append(f"### Architecture Summary\n{architecture_summary}")
        if prd_path or architecture_path:
            sections.append("### Reference Docs")
            if prd_path:
                sections.append(f"- PRD: `{prd_path}`")
            if architecture_path:
                sections.append(f"- Architecture: `{architecture_path}`")

    sections.append(
        "\n## Your Task\n"
        "1. Read the error context carefully.\n"
        "2. Inspect relevant files in the codebase to understand the failure.\n"
        "3. Diagnose the root cause.\n"
        "4. Decide whether a retry with different guidance could succeed.\n"
        "5. If yes, provide specific, actionable guidance in `modified_context`.\n"
        "6. Return a RetryAdvice JSON object."
    )

    return "\n".join(sections)
