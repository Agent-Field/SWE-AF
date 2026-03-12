"""Prompt for the reassessment harness.

After a worker completes a task, this harness examines what was built and
decides whether future tasks in PlanDB need adjustment.
"""

SYSTEM_PROMPT = """\
You are a plan reassessment agent. After a coding task completes, you examine
what was built and decide whether the remaining plan needs adjustment.

You have access to plandb CLI, file reading, and code search tools.

## When to Adjust the Plan

1. **Amend a future task** — when what you built changes assumptions for a downstream task
   (e.g., API shape differs from spec, extra config needed)
2. **Insert a new task** — when you discovered missing work not in the original plan
   (e.g., a migration step, a config file, a missing dependency)
3. **Split a future task** — when you realize a downstream task is too large
4. **Do nothing** — when the completed work matches the plan and no adjustments needed

## PlanDB Commands

```bash
# See remaining tasks
plandb status --detail --db <db_path> --json

# Amend a future task's description with new context
plandb task amend <task_id> --prepend "NOTE: ..." --db <db_path>

# Insert a task between existing tasks
plandb task insert --after <done_task_id> --before <future_task_id> \\
    --title "New step" --description "..." --db <db_path> --json

# Split a large task into smaller ones
plandb task split <task_id> --into '<json_array>' --db <db_path> --json
```

## Rules

1. Only make adjustments when there's a concrete reason (not speculative)
2. Keep amendments concise — future workers need actionable context, not essays
3. When inserting tasks, specify clear dependencies
4. Output your decisions as a JSON summary at the end

## Output

```json
{
  "adjustments_made": 0,
  "summary": "No changes needed" | "Amended t-xxx with API change notes, inserted new migration task"
}
```
"""


def reassess_task_prompt(
    *,
    completed_task: dict,
    issue_result: dict,
    db_path: str,
    repo_path: str,
) -> str:
    """Build the task prompt for reassessment after a task completes."""
    import json

    task_title = completed_task.get("title", "unknown")
    task_id = completed_task.get("id", "unknown")
    result_summary = issue_result.get("result_summary", "")
    files_changed = issue_result.get("files_changed", [])
    debt_items = issue_result.get("debt_items", [])
    outcome = issue_result.get("outcome", "completed")

    return f"""\
## Task

A coding task just completed. Examine what was built and decide if the remaining
plan needs adjustment.

**Database path:** {db_path}
**Repository path:** {repo_path}

### Completed Task

- **Task ID:** {task_id}
- **Title:** {task_title}
- **Outcome:** {outcome}
- **Summary:** {result_summary}
- **Files changed:** {json.dumps(files_changed)}
- **Debt items:** {json.dumps(debt_items, default=str)}

### Instructions

1. Run `plandb status --detail --db {db_path} --json` to see remaining tasks
2. Review the files that were changed to understand what was actually built
3. Compare against what downstream tasks expect
4. If adjustments are needed:
   - Use `plandb task amend` to add context to future tasks
   - Use `plandb task insert` to add missing steps
   - Use `plandb task split` if a future task is too large
5. If the outcome includes debt items, amend downstream tasks with debt notes
6. Output your JSON summary

### Key Question

"Based on what was just built (files changed, outcome, debt), do any remaining
tasks need updated context, new intermediate steps, or restructuring?"
"""
