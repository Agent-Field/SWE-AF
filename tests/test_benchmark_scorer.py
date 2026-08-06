"""Tests for the deterministic benchmark scorer.

The scorer's whole claim is that the mechanical dimensions of the README
benchmark (Structure, Hygiene, Git) can be recomputed by anyone from a
project directory. These tests pin that claim: a well-formed project earns
the full deterministic score, each defect docks exactly its own check, and
states the scorer cannot see (no .git, tests not run) are reported as
unscorable rather than silently zeroed — a scorer that can't tell "failed"
from "couldn't look" would rank projects by how much of them it happened
to see.

The scorer is stdlib-only and lives with the benchmark assets it scores:
``examples/agent-comparison/evaluator/score.py``.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_SCORER = (
    Path(__file__).resolve().parents[1]
    / "examples" / "agent-comparison" / "evaluator" / "score.py"
)
_spec = importlib.util.spec_from_file_location("benchmark_scorer", _SCORER)
scorer = importlib.util.module_from_spec(_spec)
# dataclasses resolves string annotations through sys.modules[cls.__module__],
# so the module must be registered before exec_module runs its @dataclass defs.
sys.modules["benchmark_scorer"] = scorer
_spec.loader.exec_module(scorer)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            "PATH": "/usr/bin:/bin:/usr/local/bin",
        },
    )


def _check(report, dimension: str, name: str):
    for c in report.checks:
        if c.dimension == dimension and c.name == name:
            return c
    raise AssertionError(f"no check {dimension}/{name} in report")


@pytest.fixture
def well_formed(tmp_path: Path) -> Path:
    """A project shaped the way the rubric's top scores describe."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "src" / "store.js").write_text("module.exports = {};\n")
    (root / "src" / "commands.js").write_text("module.exports = {};\n")
    (root / "cli.js").write_text("#!/usr/bin/env node\n")
    (root / "tests" / "store.test.js").write_text("test('x', () => {});\n")
    (root / ".gitignore").write_text("node_modules/\ncoverage/\n")
    (root / "package.json").write_text(
        '{"name": "p", "description": "d", "license": "MIT"}\n'
    )
    _git(root, "init", "-q")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feat: initial project scaffold")
    (root / "src" / "utils.js").write_text("module.exports = {};\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "feat: add shared utils module")
    (root / "tests" / "utils.test.js").write_text("test('y', () => {});\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "test: cover utils edge cases")
    return root


def test_well_formed_project_earns_all_deterministic_points(well_formed: Path):
    report = scorer.score(well_formed)
    # Structure 20 + Hygiene 20 + Git 15 — everything scoreable without executing code.
    assert report.scoreable() == 55
    assert report.earned() == 55


def test_flat_layout_docks_structure_only(well_formed: Path):
    # Flatten: move modules to the root, drop the test dir.
    for f in list((well_formed / "src").iterdir()):
        f.rename(well_formed / f.name)
    (well_formed / "src").rmdir()
    for f in list((well_formed / "tests").iterdir()):
        f.rename(well_formed / f.name)
    (well_formed / "tests").rmdir()
    _git(well_formed, "add", "-A")
    _git(well_formed, "commit", "-qm", "refactor: flatten layout for test")

    report = scorer.score(well_formed)
    assert _check(report, "structure", "source in dedicated dir").passed is False
    assert _check(report, "structure", "tests in dedicated dir").passed is False
    # Hygiene and git are untouched by layout.
    assert _check(report, "hygiene", "no junk artifacts in tree").passed is True
    assert _check(report, "git", "history has >= 3 commits").passed is True


def test_junk_artifacts_and_dirty_tree_dock_hygiene(well_formed: Path):
    (well_formed / "coverage").mkdir()
    (well_formed / "coverage" / "lcov.info").write_text("x\n")
    (well_formed / "todos.json").write_text("[]\n")  # persisted runtime data

    report = scorer.score(well_formed)
    junk = _check(report, "hygiene", "no junk artifacts in tree")
    assert junk.passed is False
    assert "coverage/" in junk.evidence and "todos.json" in junk.evidence
    # Untracked junk also means the tree is not clean.
    assert _check(report, "hygiene", "clean git status").passed is False


def test_weak_and_duplicated_commit_subjects_dock_git(well_formed: Path):
    (well_formed / "a.txt").write_text("1")
    _git(well_formed, "add", "-A")
    _git(well_formed, "commit", "-qm", "wip")
    (well_formed / "b.txt").write_text("2")
    _git(well_formed, "add", "-A")
    _git(well_formed, "commit", "-qm", "feat: add shared utils module")  # duplicate

    report = scorer.score(well_formed)
    weak = _check(report, "git", "commit subjects are descriptive")
    assert weak.passed is False
    assert "wip" in weak.evidence
    dupes = _check(report, "git", "no duplicated subjects")
    assert dupes.passed is False
    assert "feat: add shared utils module" in dupes.evidence


def test_missing_git_history_is_unscorable_not_zero(tmp_path: Path):
    """The checked-in benchmark artifacts have their .git stripped; the Git
    dimension must be reported as unscorable there, never as a silent 0 —
    otherwise vendored copies would rank below originals for free."""
    root = tmp_path / "no-git"
    (root / "src").mkdir(parents=True)
    (root / "src" / "a.js").write_text("x\n")
    report = scorer.score(root)
    for name in ("history has >= 3 commits",
                 "commit subjects are descriptive",
                 "no duplicated subjects"):
        assert _check(report, "git", name).passed is None
    assert _check(report, "hygiene", "clean git status").passed is None
    # Unscorable points are excluded from the denominator, not failed.
    assert report.scoreable() == 35  # structure 20 + hygiene 15


def test_functional_not_run_by_default(well_formed: Path):
    report = scorer.score(well_formed)
    fn = _check(report, "functional", "npm test passes")
    assert fn.passed is None
    assert "NOT RUN" in fn.evidence


def test_quality_is_observed_never_scored(well_formed: Path):
    report = scorer.score(well_formed)
    assert all(c.dimension != "quality" for c in report.checks)
    assert any("package.json metadata" in o for o in report.observations)


def test_scores_the_checked_in_sonnet_artifact():
    """Smoke against a real vendored artifact: claude-code-sonnet is flat
    (cli.js / todo.js / todo.test.js at the root), which is exactly what the
    README's Structure column docks it for — the scorer must see it too."""
    artifact = _SCORER.parent.parent / "claude-code-sonnet"
    if not artifact.is_dir():
        pytest.skip("benchmark artifact not present")
    report = scorer.score(artifact)
    assert _check(report, "structure", "source in dedicated dir").passed is False
    assert _check(report, "structure", "tests in dedicated dir").passed is False
