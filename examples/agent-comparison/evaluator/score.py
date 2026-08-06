#!/usr/bin/env python3
"""Deterministic scorer for the README benchmark rubric.

The benchmark table in the README scores five dimensions (Functional 30,
Structure 20, Hygiene 20, Git 15, Quality 15). Four of them are mechanical
properties of the generated project, so they should not require a human --
or trust in one. This script recomputes them from a project directory and
prints per-check evidence, so anyone can re-run the comparison on any
agent's output and get the same numbers.

What it will and will not do:

- Structure / Hygiene / Git (55 pts) are scored deterministically. Git
  checks need the project's real ``.git`` history; on the checked-in
  artifacts (history stripped when they were copied into examples/) they
  are reported as UNSCORABLE rather than silently zeroed.
- Functional (30 pts) is scored by actually running ``npm install`` and
  ``npm test``, opt-in via ``--run-tests`` because it executes the
  project's code.
- Quality (15 pts) is a judgment call. This script does not fake one: it
  reports observations (README, package metadata, custom error types) and
  assigns no points. A deterministic scorer that pretended to measure
  "quality" would just be an opinion with extra steps.

So a full run reports up to 85 recomputable points and clearly labels the
remaining 15 as judgment. Scores are only comparable when produced from
each agent's original output directory, including its ``.git``.

Usage:
    python score.py <project_dir> [--run-tests] [--json]

Exit code is 0 unless the directory is missing or not a project.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path

SOURCE_DIRS = ("src", "lib", "bin")
TEST_DIRS = ("test", "tests", "__tests__")
JUNK_NAMES = (".DS_Store",)
JUNK_DIRS = ("node_modules", "coverage")
JUNK_GLOBS = ("*.log",)
# Files the todo-app prompt tends to leave behind: persisted runtime data.
JUNK_DATA = ("todos.json", "todo.json", "data.json")

TRIVIAL_SUBJECT = re.compile(r"^(wip|fix|update|tmp|temp|changes|stuff|misc)\.?$", re.I)


@dataclass
class Check:
    dimension: str
    name: str
    points: int
    # True = earned, False = not earned, None = unscorable in this context.
    passed: bool | None
    evidence: str


@dataclass
class Report:
    project: str
    checks: list[Check] = field(default_factory=list)
    observations: list[str] = field(default_factory=list)

    def add(self, dimension: str, name: str, points: int, passed: bool | None, evidence: str) -> None:
        self.checks.append(Check(dimension, name, points, passed, evidence))

    def earned(self) -> int:
        return sum(c.points for c in self.checks if c.passed is True)

    def scoreable(self) -> int:
        return sum(c.points for c in self.checks if c.passed is not None)


def _list_files(root: Path) -> list[Path]:
    out: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "node_modules")]
        for f in filenames:
            out.append(Path(dirpath, f).relative_to(root))
    return out


def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, text=True, timeout=60
    )


# ---------------------------------------------------------------- structure

def score_structure(root: Path, report: Report) -> None:
    files = _list_files(root)
    js_at_root = [f for f in files if f.parent == Path(".") and f.suffix in (".js", ".mjs", ".cjs", ".ts")]
    src_dirs = sorted({f.parts[0] for f in files if f.parts[0] in SOURCE_DIRS})
    test_dirs = sorted({f.parts[0] for f in files if f.parts[0] in TEST_DIRS})

    # Source lives in a dedicated directory, not flat at the project root.
    # A lone root cli.js entry point alongside a src/ dir is conventional and
    # does not count against it; three root modules and no src/ does.
    modular = bool(src_dirs) and len(js_at_root) <= 1
    report.add(
        "structure", "source in dedicated dir", 7, modular,
        f"source dirs: {src_dirs or 'none'}; root-level modules: {[str(f) for f in js_at_root] or 'none'}",
    )

    report.add(
        "structure", "tests in dedicated dir", 7, bool(test_dirs),
        f"test dirs: {test_dirs or 'none'}",
    )

    modules = [f for f in files if f.suffix in (".js", ".mjs", ".cjs", ".ts")
               and not any(part in TEST_DIRS for part in f.parts)]
    report.add(
        "structure", "more than one source module", 6, len(modules) >= 2,
        f"{len(modules)} source modules",
    )


# ------------------------------------------------------------------ hygiene

def score_hygiene(root: Path, report: Report) -> None:
    gitignore = root / ".gitignore"
    report.add("hygiene", ".gitignore exists", 5, gitignore.is_file(),
               "present" if gitignore.is_file() else "absent")

    covers = gitignore.is_file() and "node_modules" in gitignore.read_text(errors="replace")
    report.add(
        "hygiene", ".gitignore covers node_modules", 5,
        covers if gitignore.is_file() else False,
        "listed" if covers else "not listed (or no .gitignore)",
    )

    junk: list[str] = []
    for d in JUNK_DIRS:
        if (root / d).is_dir():
            junk.append(d + "/")
    files = _list_files(root)
    for f in files:
        if f.name in JUNK_NAMES or f.name in JUNK_DATA or any(f.match(g) for g in JUNK_GLOBS):
            junk.append(str(f))
    report.add(
        "hygiene", "no junk artifacts in tree", 5, not junk,
        f"junk found: {junk}" if junk else "clean",
    )

    if (root / ".git").exists():
        status = _git(root, "status", "--porcelain")
        dirty = status.stdout.strip()
        report.add(
            "hygiene", "clean git status", 5, not dirty,
            f"{len(dirty.splitlines())} dirty paths" if dirty else "clean",
        )
    else:
        report.add("hygiene", "clean git status", 5, None, "UNSCORABLE: no .git in this copy")


# ---------------------------------------------------------------------- git

def score_git(root: Path, report: Report) -> None:
    if not (root / ".git").exists():
        for name, pts in (("history has >= 3 commits", 5),
                          ("commit subjects are descriptive", 5),
                          ("no duplicated subjects", 5)):
            report.add("git", name, pts, None,
                       "UNSCORABLE: no .git in this copy — score from the agent's original output")
        return

    log = _git(root, "log", "--format=%s")
    if log.returncode != 0:
        for name, pts in (("history has >= 3 commits", 5),
                          ("commit subjects are descriptive", 5),
                          ("no duplicated subjects", 5)):
            report.add("git", name, pts, None, f"UNSCORABLE: git log failed: {log.stderr.strip()}")
        return

    subjects = [s for s in log.stdout.splitlines() if s.strip()]
    report.add("git", "history has >= 3 commits", 5, len(subjects) >= 3, f"{len(subjects)} commits")

    weak = [s for s in subjects if len(s) < 10 or TRIVIAL_SUBJECT.match(s.strip())]
    report.add(
        "git", "commit subjects are descriptive", 5, bool(subjects) and not weak,
        f"weak subjects: {weak}" if weak else "all subjects >= 10 chars and non-trivial",
    )

    dupes = sorted({s for s in subjects if subjects.count(s) > 1})
    report.add(
        "git", "no duplicated subjects", 5, bool(subjects) and not dupes,
        f"duplicated: {dupes}" if dupes else "all unique",
    )


# --------------------------------------------------------------- functional

def score_functional(root: Path, report: Report, run_tests: bool) -> None:
    if not run_tests:
        report.add("functional", "npm test passes", 30, None,
                   "NOT RUN: pass --run-tests to execute the project's own suite")
        return
    if not (root / "package.json").is_file():
        report.add("functional", "npm test passes", 30, False, "no package.json")
        return
    env = {**os.environ, "CI": "1"}
    install = subprocess.run(["npm", "install", "--no-audit", "--no-fund", "--silent"],
                             cwd=root, capture_output=True, text=True, timeout=600, env=env)
    if install.returncode != 0:
        report.add("functional", "npm test passes", 30, False,
                   f"npm install failed: {install.stderr.strip()[:200]}")
        return
    test = subprocess.run(["npm", "test", "--silent"],
                          cwd=root, capture_output=True, text=True, timeout=600, env=env)
    lines = [ln for ln in (test.stdout + test.stderr).splitlines() if ln.strip()]
    tail = lines[-1] if lines else "(no output)"
    report.add("functional", "npm test passes", 30, test.returncode == 0,
               f"exit {test.returncode}: {tail[:200]}")


# ------------------------------------------------------ quality observations

def observe_quality(root: Path, report: Report) -> None:
    """Quality (15) is judgment. Report inputs to that judgment; score nothing."""
    report.observations.append(
        f"README present: {(root / 'README.md').is_file()}"
    )
    pkg = root / "package.json"
    if pkg.is_file():
        try:
            meta = json.loads(pkg.read_text())
            have = [k for k in ("description", "license", "author") if meta.get(k)]
            report.observations.append(f"package.json metadata present: {have or 'none'}")
        except json.JSONDecodeError:
            report.observations.append("package.json: not valid JSON")
    error_types: set[str] = set()
    for f in _list_files(root):
        if f.suffix in (".js", ".mjs", ".cjs", ".ts"):
            text = (root / f).read_text(errors="replace")
            error_types.update(re.findall(r"class\s+(\w*Error)\s+extends", text))
    report.observations.append(f"custom error types: {sorted(error_types) or 'none'}")


# ------------------------------------------------------------------- output

def render(report: Report) -> str:
    lines = [f"project: {report.project}", ""]
    by_dim: dict[str, list[Check]] = {}
    for c in report.checks:
        by_dim.setdefault(c.dimension, []).append(c)
    for dim, checks in by_dim.items():
        earned = sum(c.points for c in checks if c.passed is True)
        scoreable = sum(c.points for c in checks if c.passed is not None)
        total = sum(c.points for c in checks)
        head = f"{dim} — {earned}/{scoreable} scoreable"
        if scoreable < total:
            head += f" ({total - scoreable} pts unscorable here)"
        lines.append(head)
        for c in checks:
            mark = {True: "PASS", False: "fail", None: "  — "}[c.passed]
            lines.append(f"  [{mark}] ({c.points:>2}) {c.name}: {c.evidence}")
        lines.append("")
    lines.append(f"deterministic total: {report.earned()}/{report.scoreable()} scoreable points")
    lines.append("quality (15 pts): judgment — observations only:")
    for o in report.observations:
        lines.append(f"  - {o}")
    return "\n".join(lines)


def score(root: Path, run_tests: bool = False) -> Report:
    report = Report(project=str(root))
    score_structure(root, report)
    score_hygiene(root, report)
    score_git(root, report)
    score_functional(root, report, run_tests)
    observe_quality(root, report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("project_dir", type=Path)
    parser.add_argument("--run-tests", action="store_true",
                        help="execute npm install + npm test for the Functional dimension")
    parser.add_argument("--json", action="store_true", help="emit the report as JSON")
    args = parser.parse_args(argv)

    root = args.project_dir
    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2
    report = score(root, run_tests=args.run_tests)
    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        print(render(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
