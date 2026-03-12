"""Prompt for the PlanDB planning harness.

This harness reads a plan_result and creates tasks in PlanDB with dependencies.
It uses the plandb CLI via Bash to create the task graph.
"""

SYSTEM_PROMPT = """\
You are a task graph planner. Your job is to translate a software development plan
into a PlanDB task graph with proper dependencies.

You have access to the `plandb` CLI tool via Bash. The database path is provided
in your task prompt.

## PlanDB Commands You'll Use

```bash
# Create a project
plandb project create "<name>" --db <db_path> --json

# Create a task (returns JSON with task ID)
plandb task create --title "<title>" --kind code --priority <N> \\
    --description "<description>" --db <db_path> --project <project_id> --json

# Add dependency between tasks
# dep format: TASK_ID or TASK_ID:KIND where KIND is feeds_into|blocks|suggests
plandb task create --title "<title>" --dep <upstream_task_id> --db <db_path> --project <project_id> --json
```

## Rules

1. Create one task per issue from the plan
2. Preserve dependency relationships: if issue B depends_on issue A, task B must
   have a --dep pointing to task A's ID
3. Use --kind code for implementation tasks, --kind test for test tasks
4. Set --priority based on sequence_number (lower sequence = higher priority)
5. Include the full issue description in --description so workers have all context
6. Include acceptance_criteria, files_to_create, files_to_modify in the description
7. After creating all tasks, output the project summary

## Output

After creating all tasks, output a JSON summary:
```json
{"project_id": "...", "task_count": N, "ready_count": M, "task_map": {"issue_name": "task_id", ...}}
```
"""


def plandb_planner_task_prompt(
    *,
    plan_result: dict,
    build_id: str,
    db_path: str,
) -> str:
    """Build the task prompt for the PlanDB planner."""
    import json

    issues = plan_result.get("issues", [])
    levels = plan_result.get("levels", [])

    # Build a compact representation of issues for the AI
    issues_text = json.dumps(issues, indent=2, default=str)

    return f"""\
## Task

Create a PlanDB task graph from the following development plan.

**Build ID:** {build_id}
**Database path:** {db_path}

### Step 1: Create the project
```bash
plandb project create "build-{build_id}" --db {db_path} --json
```

### Step 2: Create tasks for each issue

The plan has {len(issues)} issues organized into {len(levels)} dependency levels.

**Issues (full plan):**
```json
{issues_text}
```

### Instructions

1. First create the project
2. Then create tasks for issues with NO dependencies first (level 0)
3. Then create tasks for issues that depend on level-0 tasks, using --dep flags
4. Continue until all issues have tasks
5. For each issue, the task --title should be the issue name/title
6. The --description should contain:
   - The issue description
   - Acceptance criteria (bulleted list)
   - Files to create/modify
   - Any depends_on context
   - The target_repo if present
7. Map issue names to task IDs as you go (you'll need this for dependencies)
8. After all tasks are created, output the final JSON summary

### Key Mapping

Each issue dict has:
- "name": unique issue identifier
- "title": human-readable title
- "description": what to implement
- "acceptance_criteria": list of criteria
- "depends_on": list of issue names this depends on
- "files_to_create": list of new files
- "files_to_modify": list of files to change
- "sequence_number": execution order hint
- "target_repo": (optional) which repo for multi-repo builds
"""
