"""Pydantic schemas for the issue-level build entry point (``implement_issue``).

The caller — typically a bigger coding harness (Claude Code, Codex, OpenCode)
delegating a well-scoped task — owns planning. ``IssueSpec`` is the wire shape
it sends; ``to_planned_issue()`` maps it 1:1 onto the internal ``PlannedIssue``
dict the coding loop consumes, so no planning agents run at all.
"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from swe_af.execution.schemas import (
    ROLE_TO_MODEL_FIELD,
    _default_runtime,
)

# Roles the issue-level path can invoke. "git" covers the optional PR step.
ISSUE_MODEL_ROLE_KEYS: tuple[str, ...] = (
    "coder",
    "code_reviewer",
    "qa",
    "qa_synthesizer",
    "verifier",
    "git",
)
_ALLOWED_ISSUE_MODEL_KEYS: set[str] = set(ISSUE_MODEL_ROLE_KEYS) | {"default"}

ISSUE_MODEL_FIELDS: list[str] = [
    ROLE_TO_MODEL_FIELD[role] for role in ISSUE_MODEL_ROLE_KEYS
]


def slugify(text: str, max_length: int = 40) -> str:
    """Kebab-case slug safe for git branch names and file paths."""
    slug = re.sub(r"[^0-9a-zA-Z]+", "-", text.strip().lower()).strip("-")
    slug = re.sub(r"-+", "-", slug)[:max_length].strip("-")
    return slug or "issue"


class IssueSpec(BaseModel):
    """A caller-provided, fully-scoped issue for ``implement_issue``."""

    model_config = ConfigDict(extra="forbid")

    title: str
    description: str
    name: str = ""  # kebab-case slug; derived from title when empty
    acceptance_criteria: list[str] = []
    files_to_create: list[str] = []
    files_to_modify: list[str] = []
    testing_strategy: str = ""
    estimated_complexity: str = "small"
    needs_deeper_qa: bool = False  # True => coder → QA + reviewer → synthesizer

    @field_validator("title", "description")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("must be a non-empty string")
        return v

    def to_planned_issue(self, additional_context: str = "") -> dict:
        """Map onto the internal ``PlannedIssue`` dict shape (see
        ``swe_af.reasoners.schemas.PlannedIssue``) consumed by the coding loop.

        Built as a plain dict on purpose: importing ``swe_af.reasoners``
        would load every planning/execution agent module into the process.
        """
        description = self.description
        if additional_context:
            description = (
                f"{description}\n\n## Additional context from the caller\n\n"
                f"{additional_context}"
            )
        return {
            "name": slugify(self.name or self.title),
            "title": self.title,
            "description": description,
            "acceptance_criteria": list(self.acceptance_criteria),
            "depends_on": [],
            "provides": [],
            "estimated_complexity": self.estimated_complexity,
            "files_to_create": list(self.files_to_create),
            "files_to_modify": list(self.files_to_modify),
            "testing_strategy": self.testing_strategy,
            "sequence_number": 1,
            "guidance": {
                "needs_new_tests": True,
                "estimated_scope": self.estimated_complexity,
                "touches_interfaces": False,
                "needs_deeper_qa": self.needs_deeper_qa,
                "testing_guidance": self.testing_strategy,
                "review_focus": "",
                "risk_rationale": "",
            },
            "target_repo": "",
        }


class IssueBuildConfig(BaseModel):
    """Configuration for a single issue-level build run."""

    model_config = ConfigDict(extra="forbid")

    runtime: Literal["claude_code", "open_code", "codex"] = Field(
        default_factory=_default_runtime
    )
    models: dict[str, str] | None = None
    max_coding_iterations: int = 3
    agent_max_turns: int = 50
    agent_timeout_seconds: int = 1800
    permission_mode: str = ""
    verify: bool = True  # single verifier pass against the acceptance criteria
    enable_github_pr: bool = False  # the caller owns merge/PR/CI by default
    github_pr_base: str = ""
    branch_prefix: str = "issue/"
    keep_worktree: bool = False  # leave the worktree in place for debugging

    @field_validator("models")
    @classmethod
    def _validate_issue_model_keys(
        cls, v: dict[str, str] | None
    ) -> dict[str, str] | None:
        if v is None:
            return v
        unknown = sorted(k for k in v if k not in _ALLOWED_ISSUE_MODEL_KEYS)
        if unknown:
            raise ValueError(
                f"Unknown model keys for implement_issue: "
                f"{', '.join(repr(k) for k in unknown)}. "
                f"Valid keys: {', '.join(sorted(_ALLOWED_ISSUE_MODEL_KEYS))}"
            )
        return v


class IssueBuildResult(BaseModel):
    """Top-level result returned by ``implement_issue``.

    The branch is the deliverable: the caller merges (or discards) it. The
    worktree used to produce it is removed unless ``keep_worktree`` was set.
    """

    success: bool
    outcome: str  # IssueOutcome value, or "error" for unexpected failures
    summary: str
    build_id: str = ""
    branch: str = ""  # empty when no commits were produced (branch deleted)
    base_branch: str = ""
    base_sha: str = ""
    commits: list[str] = []
    files_changed: list[str] = []
    diff_stat: str = ""
    iterations: int = 0
    iteration_history: list[dict] = []
    debt_items: list[dict] = []
    verification: dict | None = None
    pr_url: str = ""
    error_message: str = ""
