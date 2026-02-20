"""Pydantic schemas for the planning pipeline artifacts."""

from __future__ import annotations

from pydantic import BaseModel


class PRD(BaseModel):
    """Product Requirements Document produced by the product manager."""

    validated_description: str
    acceptance_criteria: list[str]
    must_have: list[str]
    nice_to_have: list[str]
    out_of_scope: list[str]
    assumptions: list[str] = []
    risks: list[str] = []


class ArchitectureComponent(BaseModel):
    """A single component in the architecture."""

    name: str
    responsibility: str
    touches_files: list[str] = []
    depends_on: list[str] = []


class ArchitectureDecision(BaseModel):
    """A key architectural decision with rationale."""

    decision: str
    rationale: str


class Architecture(BaseModel):
    """Architecture document produced by the architect."""

    summary: str
    components: list[ArchitectureComponent]
    interfaces: list[str]
    decisions: list[ArchitectureDecision]
    file_changes_overview: str


class ReviewResult(BaseModel):
    """Tech lead review of the architecture."""

    approved: bool
    feedback: str
    scope_issues: list[str] = []
    complexity_assessment: str = "appropriate"
    summary: str


class IssueGuidance(BaseModel):
    """Per-issue guidance from the sprint planner that shapes downstream agent behavior.

    Structured fields drive loop routing (e.g. needs_deeper_qa selects the flagged
    path). Freeform fields are injected into agent prompts to shape behavior.
    """

    # Structured — drives loop routing
    needs_new_tests: bool = True
    estimated_scope: str = "medium"       # "trivial" | "small" | "medium" | "large"
    touches_interfaces: bool = False
    needs_deeper_qa: bool = False         # True => flagged path (QA + reviewer + synthesizer)

    # Freeform — shapes agent behavior
    testing_guidance: str = ""            # Proportional test instructions
    review_focus: str = ""                # What reviewer should focus on
    risk_rationale: str = ""              # Why this needs (or doesn't need) deep QA


class PlannedIssue(BaseModel):
    """A single issue in the sprint plan."""

    name: str  # Kebab-case slug
    title: str  # Human-readable
    description: str  # Rich, self-contained for coder
    acceptance_criteria: list[str]  # Mapped from PRD
    depends_on: list[str] = []  # Issue names
    provides: list[str] = []  # What becomes available to dependents
    estimated_complexity: str = "medium"
    files_to_create: list[str] = []  # Files this issue is expected to create
    files_to_modify: list[str] = []  # Files this issue is expected to edit
    testing_strategy: str = ""  # Test file paths, framework, coverage expectations
    sequence_number: int | None = None  # Assigned after topo sort, used in file/branch naming
    guidance: IssueGuidance | None = None  # Per-issue guidance from sprint planner
    target_repo: str = ""  # Target repository for multi-repo builds (empty = default/only repo)


class PlanResult(BaseModel):
    """Final output of the planning pipeline."""

    prd: PRD
    architecture: Architecture
    review: ReviewResult
    issues: list[PlannedIssue]
    levels: list[list[str]]  # Parallel execution levels from topo sort
    file_conflicts: list[dict] = []  # Informational only — merger agent handles resolution
    artifacts_dir: str
    rationale: str
