"""Discovery-surface tests: which reasoners a caller may start, and which are internal.

The control plane publishes each reasoner's tags and description
(``af ls --entrypoints`` / ``GET /api/v1/discovery/capabilities``), and a coding
agent picks what to invoke from exactly that payload. These tests pin it:

* every ``run_*`` role reasoner is tagged ``internal`` and describes itself as
  an orchestrator-driven stage — a bare, undescribed ``run_product_manager`` is
  what led callers to invoke it directly and get a context-less failure;
* the ``entrypoint`` set is exactly the reasoners a caller can legitimately
  start a run from.

Each node's surface is collected in a subprocess, like ``test_node_id_isolation``:
both apps include the same shared routers, and an ``AgentRouter`` hands its
entries to the first app that includes them — so in-process the surface depends
on which app another test imported (or reloaded) first. A fresh interpreter is
what the control plane actually sees at node startup.

The Go node registers the same surface under the same identity — the mirror of
this module is ``go/internal/node/discovery_surface_test.go``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from swe_af.surface import TAG_ENTRYPOINT, TAG_INTERNAL, internal_role_description

# Independent checklist of the pipeline area each internal role belongs to.
# Written from the role's job — NOT read back out of the registrations — so a
# role that changes area without its description following fails here.
ROLE_AREAS: dict[str, str] = {
    # planning
    "run_product_manager": "planning",
    "run_environment_scout": "planning",
    "run_architect": "planning",
    "run_tech_lead": "planning",
    "run_sprint_planner": "planning",
    # coding
    "run_coder": "coding",
    "run_qa": "coding",
    "run_code_reviewer": "coding",
    "run_qa_synthesizer": "coding",
    # git / workspace
    "run_git_init": "gitops",
    "run_workspace_setup": "gitops",
    "run_workspace_cleanup": "gitops",
    "run_merger": "gitops",
    "run_integration_tester": "gitops",
    "run_repo_finalize": "gitops",
    "run_github_pr": "gitops",
    # advisor / verify
    "run_retry_advisor": "advisor",
    "run_issue_advisor": "advisor",
    "run_replanner": "advisor",
    "run_issue_writer": "advisor",
    "run_verifier": "advisor",
    "generate_fix_issues": "advisor",
    # CI / resolve
    "run_ci_watcher": "ci",
    "run_ci_fixer": "ci",
    "run_pr_resolver": "ci",
}

# The exact set of swe-planner reasoners a caller may start a run from.
# ``execute`` is deliberately absent: its ``plan_result`` input is produced by a
# prior ``plan`` call, not written by hand.
PLANNER_ENTRYPOINTS = {"build", "implement_issue", "plan", "resolve", "resume_build"}

# The fast node has no plan/execute/resolve/resume_build — its own build plus
# the shared issue-level entry point.
FAST_ENTRYPOINTS = {"build", "implement_issue"}

_DUMP = (
    "import json, importlib; "
    "m = importlib.import_module('{module}'); "
    "print(json.dumps({{r['id']: {{'tags': r.get('tags') or [], "
    "'description': r.get('description', '')}} for r in m.app.reasoners}}))"
)


def _node_surface(module: str) -> dict[str, dict]:
    """Registered metadata of *module*'s agent, collected in a fresh interpreter."""
    env = {k: v for k, v in os.environ.items() if k != "NODE_ID"}
    env["AGENTFIELD_SERVER"] = "http://localhost:9999"
    result = subprocess.run(
        [sys.executable, "-c", _DUMP.format(module=module)],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"collecting {module} surface failed: {result.stderr}"
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def planner_surface() -> dict[str, dict]:
    return _node_surface("swe_af.app")


@pytest.fixture(scope="module")
def fast_surface() -> dict[str, dict]:
    return _node_surface("swe_af.fast.app")


def test_role_reasoners_are_marked_internal(planner_surface: dict[str, dict]) -> None:
    """Every role reasoner carries the ``internal`` tag and the shared warning."""
    for name, area in ROLE_AREAS.items():
        meta = planner_surface.get(name)
        assert meta is not None, f"role {name!r} is not registered"

        tags = meta["tags"]
        assert TAG_INTERNAL in tags, f"role {name!r} tags={tags} lost the internal marker"
        assert "swe-planner" in tags, f"role {name!r} tags={tags} lost the group tag"
        assert TAG_ENTRYPOINT not in tags, f"role {name!r} must not be an entry point"

        assert meta["description"] == internal_role_description(area), (
            f"role {name!r} description={meta['description']!r} — expected the "
            f"shared {area} pipeline-stage warning"
        )


def test_internal_tag_is_exactly_the_roles(planner_surface: dict[str, dict]) -> None:
    """Nothing but the role reasoners hides behind the ``internal`` marker."""
    tagged = {name for name, meta in planner_surface.items() if TAG_INTERNAL in meta["tags"]}
    assert tagged == set(ROLE_AREAS)


def test_entrypoint_tag_is_exact_set(planner_surface: dict[str, dict]) -> None:
    """Only the reasoners a caller can start a run from carry ``entrypoint``."""
    tagged = {name for name, meta in planner_surface.items() if TAG_ENTRYPOINT in meta["tags"]}
    assert tagged == PLANNER_ENTRYPOINTS

    # The tag routes a caller to the reasoner; the description tells them
    # whether to pick it. Both are required.
    for name in PLANNER_ENTRYPOINTS:
        assert planner_surface[name]["description"], f"entrypoint {name!r} has no description"


def test_execute_is_not_an_entrypoint_and_says_where_plan_result_comes_from(
    planner_surface: dict[str, dict],
) -> None:
    """execute stays discoverable but tells callers not to hand-write plan_result."""
    meta = planner_surface["execute"]
    assert TAG_ENTRYPOINT not in meta["tags"]
    assert "plan_result comes from a prior plan call" in meta["description"]
    assert "prefer build" in meta["description"]


def test_fast_node_surface_is_marked_the_same_way(fast_surface: dict[str, dict]) -> None:
    """The fast node registers the same roles, marked internal the same way."""
    entrypoints = {name for name, meta in fast_surface.items() if TAG_ENTRYPOINT in meta["tags"]}
    assert entrypoints == FAST_ENTRYPOINTS

    for name, area in ROLE_AREAS.items():
        meta = fast_surface.get(name)
        assert meta is not None, f"role {name!r} is not registered on swe-fast"
        assert TAG_INTERNAL in meta["tags"], f"role {name!r} tags={meta['tags']} on swe-fast"
        assert meta["description"] == internal_role_description(area)
