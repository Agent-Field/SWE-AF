"""Discovery surface metadata — the tags and descriptions callers route on.

Two tags are load-bearing on both nodes, on top of the routers' grouping tags
(``swe-planner`` / ``swe-fast`` / ``swe-issue``):

* ``entrypoint`` marks a reasoner a caller may legitimately start a run from —
  ``build``, ``implement_issue``, ``plan``, ``resolve``, ``resume_build``.
  ``af ls --entrypoints`` and ``GET /api/v1/discovery/capabilities`` filter on
  it. ``execute`` is deliberately NOT one: its ``plan_result`` input is
  produced by a prior ``plan`` call, not written by hand.
* ``internal`` marks the pipeline stages an orchestrator drives and nothing
  else should call — every ``run_*`` role reasoner plus ``generate_fix_issues``.
  Each also carries :data:`INTERNAL_ROLE_DESCRIPTION`, because a coding agent
  that discovers a bare name like ``run_product_manager`` will otherwise invoke
  it directly and get a failure that reads like a broken node.

:func:`internal_role` is the single place both pieces of metadata are attached,
so the 25 role registrations share one string instead of 25 copies. The Go port
mirrors this file in ``go/internal/node/register.go`` (``internalRoleOpts`` and
``orchestratorEntrypoints``) — the two nodes register under the same identity,
so their tags and descriptions must stay identical.
"""

from __future__ import annotations

from typing import Callable

#: Tag marking a reasoner a caller may start a run from.
TAG_ENTRYPOINT = "entrypoint"

#: Tag marking an orchestrator-driven pipeline stage.
TAG_INTERNAL = "internal"

#: Description every internal pipeline stage registers with. ``area`` is the
#: stage's domain: planning, coding, gitops, advisor or ci.
INTERNAL_ROLE_DESCRIPTION = (
    "Internal {area} pipeline stage invoked by the orchestrators "
    "(build/plan/execute) — do not call directly."
)


def internal_role_description(area: str) -> str:
    """Return the one-line description for an internal *area* pipeline stage."""
    return INTERNAL_ROLE_DESCRIPTION.format(area=area)


def internal_role(router, area: str) -> Callable[[Callable], Callable]:
    """Register one internal pipeline stage on *router*.

    Drop-in replacement for ``@router.reasoner()``: adds the ``internal`` tag
    (``AgentRouter.reasoner`` merges it with the router's own group tag) and the
    shared do-not-call-directly description, which takes the place of the
    docstring summary the SDK would otherwise publish.
    """
    return router.reasoner(
        tags=[TAG_INTERNAL],
        description=internal_role_description(area),
    )
