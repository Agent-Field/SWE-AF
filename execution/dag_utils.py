"""Pure DAG manipulation helpers for the execution engine."""

from __future__ import annotations

from collections import defaultdict, deque

from execution.schemas import DAGState, ReplanAction, ReplanDecision


def recompute_levels(
    remaining_issues: list[dict],
    completed_names: set[str],
) -> list[list[str]]:
    """Topological sort (Kahn's algorithm) treating completed issues as resolved.

    Args:
        remaining_issues: Issue dicts that still need execution (each has
            ``name`` and ``depends_on`` keys).
        completed_names: Set of issue names already successfully completed.
            Dependencies on these are treated as satisfied.

    Returns:
        List of levels, where each level is a list of issue names that can
        execute concurrently.

    Raises:
        ValueError: If remaining issues contain a dependency cycle.
    """
    name_set = {i["name"] for i in remaining_issues}
    in_degree: dict[str, int] = {i["name"]: 0 for i in remaining_issues}
    dependents: dict[str, list[str]] = defaultdict(list)

    for issue in remaining_issues:
        for dep in issue.get("depends_on", []):
            # Only count deps that are in the remaining set (not completed)
            if dep in name_set and dep not in completed_names:
                in_degree[issue["name"]] += 1
                dependents[dep].append(issue["name"])

    queue: deque[str] = deque(n for n, d in in_degree.items() if d == 0)
    levels: list[list[str]] = []
    processed = 0

    while queue:
        level = list(queue)
        levels.append(level)
        processed += len(level)
        queue.clear()
        for name in level:
            for dep_name in dependents[name]:
                in_degree[dep_name] -= 1
                if in_degree[dep_name] == 0:
                    queue.append(dep_name)

    if processed != len(remaining_issues):
        cycle_nodes = [n for n, d in in_degree.items() if d > 0]
        raise ValueError(f"Dependency cycle detected among issues: {cycle_nodes}")

    return levels


def find_downstream(issue_name: str, all_issues: list[dict]) -> set[str]:
    """Find all issues transitively dependent on ``issue_name``.

    Returns:
        Set of issue names that directly or indirectly depend on the given issue.
        Does NOT include ``issue_name`` itself.
    """
    # Build adjacency: issue -> list of issues that depend on it
    dependents: dict[str, list[str]] = defaultdict(list)
    for issue in all_issues:
        for dep in issue.get("depends_on", []):
            dependents[dep].append(issue["name"])

    # BFS from issue_name
    visited: set[str] = set()
    queue = deque(dependents.get(issue_name, []))
    while queue:
        name = queue.popleft()
        if name in visited:
            continue
        visited.add(name)
        queue.extend(dependents.get(name, []))

    return visited


def apply_replan(dag_state: DAGState, decision: ReplanDecision) -> DAGState:
    """Apply a replan decision to the DAG state.

    Removes, modifies, and adds issues as directed by the replanner,
    then recomputes execution levels for the remaining work.

    Raises:
        ValueError: If the resulting DAG contains a cycle (replan is rejected).
    """
    if decision.action == ReplanAction.ABORT:
        dag_state.replan_count += 1
        dag_state.replan_history.append(decision)
        return dag_state

    if decision.action == ReplanAction.CONTINUE:
        dag_state.replan_count += 1
        dag_state.replan_history.append(decision)
        return dag_state

    completed_names = {r.issue_name for r in dag_state.completed_issues}
    failed_names = {r.issue_name for r in dag_state.failed_issues}

    # Build a working copy of issues (exclude completed and failed)
    remaining_by_name: dict[str, dict] = {}
    for issue in dag_state.all_issues:
        if issue["name"] not in completed_names and issue["name"] not in failed_names:
            remaining_by_name[issue["name"]] = dict(issue)

    # 1. Remove issues
    removed = set(decision.removed_issue_names)
    for name in removed:
        remaining_by_name.pop(name, None)

    # 2. Skip issues (mark as skipped, remove from remaining)
    skipped = set(decision.skipped_issue_names)
    for name in skipped:
        remaining_by_name.pop(name, None)
        if name not in dag_state.skipped_issues:
            dag_state.skipped_issues.append(name)

    # 3. Update existing issues
    for updated in decision.updated_issues:
        name = updated.get("name", "")
        if name in remaining_by_name:
            remaining_by_name[name].update(updated)

    # 4. Add new issues (with next-available sequence numbers)
    max_seq = max((i.get("sequence_number") or 0 for i in dag_state.all_issues), default=0)
    for new_issue in decision.new_issues:
        name = new_issue.get("name", "")
        if name and name not in remaining_by_name:
            if not new_issue.get("sequence_number"):
                max_seq += 1
                new_issue["sequence_number"] = max_seq
            remaining_by_name[name] = new_issue

    remaining = list(remaining_by_name.values())

    # Recompute levels (raises ValueError on cycle)
    new_levels = recompute_levels(remaining, completed_names)

    # Update DAG state
    dag_state.all_issues = (
        [i for i in dag_state.all_issues if i["name"] in completed_names or i["name"] in failed_names]
        + remaining
    )
    dag_state.levels = new_levels
    dag_state.current_level = 0  # reset to start of recomputed levels
    dag_state.replan_count += 1
    dag_state.replan_history.append(decision)

    return dag_state
