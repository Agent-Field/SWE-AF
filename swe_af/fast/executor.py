"""swe_af.fast.executor — fast_execute_tasks reasoner with per-task asyncio.wait_for timeouts."""

from __future__ import annotations

import asyncio
import os

from swe_af.fast import fast_router
from swe_af.fast.schemas import FastExecutionResult, FastTaskResult
from swe_af.execution.envelope import unwrap_call_result as _unwrap

NODE_ID = os.getenv("NODE_ID", "swe-fast")


@fast_router.reasoner()
async def fast_execute_tasks(
    tasks: list[dict],
    repo_path: str,
    coder_model: str = "haiku",
    permission_mode: str = "",
    ai_provider: str = "claude",
    task_timeout_seconds: int = 300,
    artifacts_dir: str = "",
    agent_max_turns: int = 50,
) -> dict:
    """Sequential single-coder-pass execution over tasks.

    One run_coder call per task, wrapped in asyncio.wait_for(task_timeout_seconds).
    On per-task timeout: outcome='timeout', continue to next task.
    On per-task failure: outcome='failed', continue to next task.
    No QA, no code-reviewer, no synthesizer, no replanning, no worktrees.

    Returns FastExecutionResult.model_dump().
    """
    import swe_af.fast.app as _app_module  # lazy import — avoids circular at module load

    task_results: list[FastTaskResult] = []

    for task_dict in tasks:
        task_name = task_dict.get("name", "unknown")
        fast_router.note(
            f"Fast executor: starting task {task_name}",
            tags=["fast_executor", "task_start"],
        )

        # Construct the issue dict compatible with run_coder's expectations
        issue = {
            "name": task_name,
            "title": task_dict.get("title", task_name),
            "description": task_dict.get("description", ""),
            "acceptance_criteria": task_dict.get("acceptance_criteria", []),
            "files_to_create": task_dict.get("files_to_create", []),
            "files_to_modify": task_dict.get("files_to_modify", []),
            "testing_strategy": "",
        }

        project_context = {
            "artifacts_dir": artifacts_dir,
            "repo_path": repo_path,
        }

        try:
            coro = _app_module.app.call(
                f"{NODE_ID}.run_coder",
                issue=issue,
                worktree_path=repo_path,   # no worktrees — coder works in repo_path
                iteration=1,
                iteration_id=task_name,
                project_context=project_context,
                model=coder_model,
                permission_mode=permission_mode,
                ai_provider=ai_provider,
            )
            raw = await asyncio.wait_for(coro, timeout=task_timeout_seconds)
            coder_result = _unwrap(raw, f"run_coder:{task_name}")
            task_results.append(FastTaskResult(
                task_name=task_name,
                outcome="completed" if coder_result.get("complete", False) else "failed",
                files_changed=coder_result.get("files_changed", []),
                summary=coder_result.get("summary", ""),
            ))
            fast_router.note(
                f"Fast executor: task {task_name} done, "
                f"outcome={task_results[-1].outcome}",
                tags=["fast_executor", "task_done"],
            )
        except asyncio.TimeoutError:
            fast_router.note(
                f"Fast executor: task {task_name} timed out after {task_timeout_seconds}s",
                tags=["fast_executor", "timeout"],
            )
            task_results.append(FastTaskResult(
                task_name=task_name,
                outcome="timeout",
                error=f"Timed out after {task_timeout_seconds}s",
            ))
        except Exception as e:
            fast_router.note(
                f"Fast executor: task {task_name} failed: {e}",
                tags=["fast_executor", "error"],
            )
            task_results.append(FastTaskResult(
                task_name=task_name,
                outcome="failed",
                error=str(e),
            ))

    completed = sum(1 for r in task_results if r.outcome == "completed")
    failed = len(task_results) - completed

    return FastExecutionResult(
        task_results=task_results,
        completed_count=completed,
        failed_count=failed,
    ).model_dump()
