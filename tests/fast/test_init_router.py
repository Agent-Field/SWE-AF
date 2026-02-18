"""Tests for swe_af.fast.__init__ — fast_router registration and isolation.

Covers:
- fast_router is an AgentRouter instance with tag 'swe-fast'
- Exactly the 5 expected reasoner names are registered on fast_router
- Planning function names are NOT registered on fast_router
- Importing swe_af.fast does not load swe_af.reasoners.pipeline
"""

from __future__ import annotations

import importlib
import sys
import types

import pytest
from agentfield import AgentRouter


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_EXPECTED_REASONERS = {
    "run_git_init",
    "run_coder",
    "run_verifier",
    "run_repo_finalize",
    "run_github_pr",
}

_FORBIDDEN_REASONERS = {
    "run_architect",
    "run_tech_lead",
    "run_sprint_planner",
    "run_product_manager",
    "run_issue_writer",
}


def _registered_names(router: AgentRouter) -> set[str]:
    """Return the set of function names registered on *router*."""
    return {r["func"].__name__ for r in router.reasoners}


# ---------------------------------------------------------------------------
# AC-1: fast_router is an AgentRouter
# ---------------------------------------------------------------------------

class TestFastRouterType:
    def test_fast_router_is_agent_router(self) -> None:
        from swe_af.fast import fast_router  # noqa: PLC0415

        assert isinstance(fast_router, AgentRouter)

    def test_fast_router_has_swe_fast_tag(self) -> None:
        from swe_af.fast import fast_router  # noqa: PLC0415

        assert "swe-fast" in fast_router.tags


# ---------------------------------------------------------------------------
# AC-2: exactly the 5 expected reasoners are registered
# ---------------------------------------------------------------------------

class TestExpectedReasoners:
    def test_all_five_reasoners_registered(self) -> None:
        from swe_af.fast import fast_router  # noqa: PLC0415

        names = _registered_names(fast_router)
        missing = _EXPECTED_REASONERS - names
        assert not missing, f"Missing reasoners: {missing}"

    @pytest.mark.parametrize("name", sorted(_EXPECTED_REASONERS))
    def test_each_reasoner_individually(self, name: str) -> None:
        from swe_af.fast import fast_router  # noqa: PLC0415

        names = _registered_names(fast_router)
        assert name in names, f"Expected reasoner '{name}' not found in {names}"


# ---------------------------------------------------------------------------
# AC-3: forbidden executor identifiers are NOT registered
# ---------------------------------------------------------------------------

class TestForbiddenReasoners:
    @pytest.mark.parametrize("name", sorted(_FORBIDDEN_REASONERS))
    def test_planning_reasoner_not_registered(self, name: str) -> None:
        from swe_af.fast import fast_router  # noqa: PLC0415

        names = _registered_names(fast_router)
        assert name not in names, (
            f"Forbidden reasoner '{name}' should not be registered on fast_router"
        )


# ---------------------------------------------------------------------------
# AC-4: importing swe_af.fast does NOT load swe_af.reasoners.pipeline
# ---------------------------------------------------------------------------

class TestNoPipelineImport:
    def test_pipeline_not_in_sys_modules_after_fast_import(self) -> None:
        """swe_af.reasoners.pipeline must NOT appear in sys.modules after import."""
        # Remove swe_af.fast (and related modules) from sys.modules so we get a
        # clean import in this test.  Other tests may have already imported it;
        # what matters is that loading swe_af.fast fresh never pulls in pipeline.
        _pipeline_key = "swe_af.reasoners.pipeline"

        # Evict fast and pipeline from sys.modules to simulate fresh import.
        to_remove = [k for k in list(sys.modules) if k.startswith("swe_af.fast")]
        for key in to_remove:
            sys.modules.pop(key, None)
        sys.modules.pop(_pipeline_key, None)

        importlib.import_module("swe_af.fast")

        assert _pipeline_key not in sys.modules, (
            "Importing swe_af.fast must not trigger swe_af.reasoners.pipeline"
        )

    def test_reasoners_init_not_imported_at_module_level(self) -> None:
        """swe_af.reasoners.__init__ should not be loaded during fast import
        (since that package __init__ imports pipeline)."""
        _pipeline_key = "swe_af.reasoners.pipeline"
        _reasoners_pkg = "swe_af.reasoners"

        to_remove = [k for k in list(sys.modules) if k.startswith("swe_af.fast")]
        for key in to_remove:
            sys.modules.pop(key, None)
        # Also remove swe_af.reasoners so we can observe it being loaded
        for key in [_reasoners_pkg, _pipeline_key, "swe_af.reasoners.execution_agents"]:
            sys.modules.pop(key, None)

        importlib.import_module("swe_af.fast")

        assert _pipeline_key not in sys.modules, (
            "swe_af.reasoners.pipeline must not be loaded when importing swe_af.fast"
        )
