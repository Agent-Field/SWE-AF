"""Validation contract for the empty-build guard in ``build()`` (issue #82, Gap 2).

Behavior under test (caller-observable, not implementation):

- A build that did NOT pass verification and never completed an issue or merged
  a branch (across the original run and every fix cycle) shipped nothing real,
  so the execution must be reported as failed — not surfaced as ``succeeded``.
- A build that completed at least one issue, or merged at least one branch, has
  shipped real code (and an open PR worth surfacing); it must NOT be flagged
  empty even when verification later failed.
- A build that passed verification is never empty.

``build()`` enforces this by raising ``ReasonerFailed`` when ``_is_empty_build``
is true, which the agentfield SDK maps to a ``failed`` execution status while
preserving the structured ``BuildResult``.
"""

import pytest

from swe_af.app import _is_empty_build


@pytest.mark.parametrize(
    ("success", "ever_completed", "ever_merged", "expected_empty"),
    [
        # Verification failed and nothing was ever shipped → empty (must fail).
        (False, 0, 0, True),
        # Verification failed but an issue completed → real work shipped.
        (False, 1, 0, False),
        # Verification failed but a branch merged (e.g. fix cycle merged) → shipped.
        (False, 0, 1, False),
        (False, 3, 2, False),
        # Verification passed → never empty, regardless of counts.
        (True, 0, 0, False),
        (True, 5, 4, False),
    ],
)
def test_empty_build_truth_table(success, ever_completed, ever_merged, expected_empty):
    assert _is_empty_build(success, ever_completed, ever_merged) is expected_empty


def test_reporter_scenario_is_empty():
    """The exact shape from issue #82: 0 completed, 0 merged, verification failed
    (12 skipped, 1 failed foundation issue, all acceptance criteria → debt).
    Only a lone .gitignore change reached the base — nothing was implemented."""
    assert _is_empty_build(success=False, ever_completed=0, ever_merged=0) is True


def test_partial_build_with_debt_is_not_empty():
    """A build that completed some issues but failed verification (and accrued
    debt) is a partial success with an open PR — it must keep returning normally
    so the execution stays succeeded and the rich result surfaces."""
    assert _is_empty_build(success=False, ever_completed=2, ever_merged=2) is False
