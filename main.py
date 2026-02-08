"""AgentField app for the SWE planning and execution pipeline.

Exposes:
  - ``build``: end-to-end plan → execute → verify (single entry point)
  - ``plan``: orchestrates product_manager → architect ↔ tech_lead → sprint_planner
  - ``execute``: runs a planned DAG with self-healing replanning
"""

from __future__ import annotations

import asyncio
import os

from reasoners import router
from reasoners.pipeline import _assign_sequence_numbers, _compute_levels, _validate_file_conflicts
from schemas import PlanResult, ReviewResult

from agentfield import Agent

NODE_ID = os.getenv("NODE_ID", "swe-planner")

app = Agent(
    node_id=NODE_ID,
    version="1.0.0",
    description="Autonomous SWE planning pipeline",
    agentfield_server=os.getenv("AGENTFIELD_SERVER", "http://localhost:8080"),
)

app.include_router(router)


@app.reasoner()
async def build(
    goal: str,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    additional_context: str = "",
    config: dict = {},
    execute_fn_target: str = "",
    ai_provider: str = "claude",
) -> dict:
    """End-to-end: plan → execute → verify → optional fix cycle.

    This is the single entry point. Pass a goal, get working code.
    """
    from execution.schemas import BuildConfig, BuildResult

    cfg = BuildConfig(**config) if config else BuildConfig()
    if execute_fn_target:
        cfg.execute_fn_target = execute_fn_target
    if ai_provider:
        cfg.ai_provider = ai_provider

    app.note("Build starting", tags=["build", "start"])

    # 1. PLAN
    plan_result = await app.call(
        f"{NODE_ID}.plan",
        goal=goal,
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        additional_context=additional_context,
        max_review_iterations=cfg.max_review_iterations,
        pm_model=cfg.pm_model,
        architect_model=cfg.architect_model,
        tech_lead_model=cfg.tech_lead_model,
        sprint_planner_model=cfg.sprint_planner_model,
        issue_writer_model=cfg.issue_writer_model,
        permission_mode=cfg.permission_mode,
        ai_provider=cfg.ai_provider,
    )

    # 1.5 GIT INIT (between plan and execute)
    git_config = None
    app.note("Phase 1.5: Git initialization", tags=["build", "git_init"])
    git_init = await app.call(
        f"{NODE_ID}.run_git_init",
        repo_path=repo_path,
        goal=goal,
        artifacts_dir=plan_result.get("artifacts_dir", ""),
        model=cfg.git_model,
        permission_mode=cfg.permission_mode,
        ai_provider=cfg.ai_provider,
    )
    if git_init.get("success"):
        git_config = {
            "integration_branch": git_init["integration_branch"],
            "original_branch": git_init["original_branch"],
            "initial_commit_sha": git_init["initial_commit_sha"],
            "mode": git_init["mode"],
        }
        app.note(
            f"Git init: mode={git_init['mode']}, branch={git_init['integration_branch']}",
            tags=["build", "git_init", "complete"],
        )
    else:
        app.note(
            f"Git init failed: {git_init.get('error_message', 'unknown')} — "
            "proceeding without git workflow",
            tags=["build", "git_init", "error"],
        )

    # 2. EXECUTE
    exec_config = {
        "max_retries_per_issue": cfg.max_retries_per_issue,
        "max_replans": cfg.max_replans,
        "replan_model": cfg.replan_model,
        "enable_replanning": cfg.enable_replanning,
        "retry_advisor_model": cfg.retry_advisor_model,
        "issue_writer_model": cfg.issue_writer_model,
        "merger_model": cfg.merger_model,
        "integration_tester_model": cfg.integration_tester_model,
        "max_integration_test_retries": cfg.max_integration_test_retries,
        "enable_integration_testing": cfg.enable_integration_testing,
        # Coding loop
        "max_coding_iterations": cfg.max_coding_iterations,
        "coder_model": cfg.coder_model,
        "qa_model": cfg.qa_model,
        "code_reviewer_model": cfg.code_reviewer_model,
        "qa_synthesizer_model": cfg.qa_synthesizer_model,
        "agent_max_turns": cfg.agent_max_turns,
    }

    dag_result = await app.call(
        f"{NODE_ID}.execute",
        plan_result=plan_result,
        repo_path=repo_path,
        execute_fn_target=cfg.execute_fn_target,
        config=exec_config,
        git_config=git_config,
        ai_provider=cfg.ai_provider,
    )

    # 3. VERIFY
    verification = None
    for cycle in range(cfg.max_verify_fix_cycles + 1):
        app.note(f"Verification cycle {cycle}", tags=["build", "verify"])
        verification = await app.call(
            f"{NODE_ID}.run_verifier",
            prd=plan_result["prd"],
            repo_path=repo_path,
            artifacts_dir=plan_result.get("artifacts_dir", artifacts_dir),
            completed_issues=[r for r in dag_result.get("completed_issues", [])],
            failed_issues=[r for r in dag_result.get("failed_issues", [])],
            skipped_issues=dag_result.get("skipped_issues", []),
            model=cfg.verifier_model,
            permission_mode=cfg.permission_mode,
            ai_provider=cfg.ai_provider,
        )

        if verification.get("passed", False) or cycle >= cfg.max_verify_fix_cycles:
            break

        # Verification failed — future: build fix issues from verifier's suggested_fixes
        app.note(
            f"Verification failed, {cfg.max_verify_fix_cycles - cycle} fix cycles remaining",
            tags=["build", "verify", "retry"],
        )
        # TODO: Build targeted fix issues from verification failures
        break

    success = verification.get("passed", False) if verification else False
    completed = len(dag_result.get("completed_issues", []))
    total = len(dag_result.get("all_issues", []))

    app.note(
        f"Build {'succeeded' if success else 'completed with issues'}: "
        f"{completed}/{total} issues, verification={'passed' if success else 'failed'}",
        tags=["build", "complete"],
    )

    return BuildResult(
        plan_result=plan_result,
        dag_state=dag_result,
        verification=verification,
        success=success,
        summary=f"{'Success' if success else 'Partial'}: {completed}/{total} issues completed"
                + (f", verification: {verification.get('summary', '')}" if verification else ""),
    ).model_dump()


@app.reasoner()
async def plan(
    goal: str,
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    additional_context: str = "",
    max_review_iterations: int = 2,
    pm_model: str = "sonnet",
    architect_model: str = "sonnet",
    tech_lead_model: str = "sonnet",
    sprint_planner_model: str = "sonnet",
    issue_writer_model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
) -> dict:
    """Run the full planning pipeline.

    Orchestrates: product_manager → architect ↔ tech_lead → sprint_planner → issue_writers
    """
    app.note("Pipeline starting", tags=["pipeline", "start"])

    # 1. PM scopes the goal into a PRD
    app.note("Phase 1: Product Manager", tags=["pipeline", "pm"])
    prd = await app.call(
        f"{NODE_ID}.run_product_manager",
        goal=goal,
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        additional_context=additional_context,
        model=pm_model,
        permission_mode=permission_mode,
        ai_provider=ai_provider,
    )

    # 2. Architect designs the solution
    app.note("Phase 2: Architect", tags=["pipeline", "architect"])
    arch = await app.call(
        f"{NODE_ID}.run_architect",
        prd=prd,
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        model=architect_model,
        permission_mode=permission_mode,
        ai_provider=ai_provider,
    )

    # 3. Tech Lead review loop
    review = None
    for i in range(max_review_iterations + 1):
        app.note(f"Phase 3: Tech Lead review (iteration {i})", tags=["pipeline", "tech_lead"])
        review = await app.call(
            f"{NODE_ID}.run_tech_lead",
            prd=prd,
            repo_path=repo_path,
            artifacts_dir=artifacts_dir,
            revision_number=i,
            model=tech_lead_model,
            permission_mode=permission_mode,
            ai_provider=ai_provider,
        )
        if review["approved"]:
            break
        if i < max_review_iterations:
            app.note(f"Architecture revision {i + 1}", tags=["pipeline", "revision"])
            arch = await app.call(
                f"{NODE_ID}.run_architect",
                prd=prd,
                repo_path=repo_path,
                artifacts_dir=artifacts_dir,
                feedback=review["feedback"],
                model=architect_model,
                permission_mode=permission_mode,
                ai_provider=ai_provider,
            )

    # Force-approve if we exhausted iterations
    assert review is not None
    if not review["approved"]:
        review = ReviewResult(
            approved=True,
            feedback=review["feedback"],
            scope_issues=review.get("scope_issues", []),
            complexity_assessment=review.get("complexity_assessment", "appropriate"),
            summary=review["summary"] + " [auto-approved after max iterations]",
        ).model_dump()

    # 4. Sprint planner decomposes into issues
    app.note("Phase 4: Sprint Planner", tags=["pipeline", "sprint_planner"])
    sprint_result = await app.call(
        f"{NODE_ID}.run_sprint_planner",
        prd=prd,
        architecture=arch,
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        model=sprint_planner_model,
        permission_mode=permission_mode,
        ai_provider=ai_provider,
    )
    issues = sprint_result["issues"]
    rationale = sprint_result["rationale"]

    # 5. Compute parallel execution levels & assign sequence numbers BEFORE issue writing
    levels = _compute_levels(issues)
    issues = _assign_sequence_numbers(issues, levels)
    file_conflicts = _validate_file_conflicts(issues, levels)

    # 4b. Parallel issue writing (issues now have sequence_number set)
    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)
    issues_dir = os.path.join(base, "plan", "issues")
    prd_path = os.path.join(base, "plan", "prd.md")
    architecture_path = os.path.join(base, "plan", "architecture.md")
    os.makedirs(issues_dir, exist_ok=True)

    prd_summary_str = prd.get("validated_description", "")
    prd_ac = prd.get("acceptance_criteria", [])
    if prd_ac:
        prd_summary_str += "\n\nAcceptance Criteria:\n" + "\n".join(f"- {c}" for c in prd_ac)

    app.note(
        f"Phase 4b: Writing {len(issues)} issue files in parallel",
        tags=["pipeline", "issue_writers"],
    )
    writer_tasks = []
    for issue in issues:
        siblings = [
            {"name": i["name"], "title": i.get("title", ""), "provides": i.get("provides", [])}
            for i in issues if i["name"] != issue["name"]
        ]
        writer_tasks.append(app.call(
            f"{NODE_ID}.run_issue_writer",
            issue=issue,
            prd_summary=prd_summary_str,
            architecture_summary=arch.get("summary", ""),
            issues_dir=issues_dir,
            repo_path=repo_path,
            prd_path=prd_path,
            architecture_path=architecture_path,
            sibling_issues=siblings,
            model=issue_writer_model,
            permission_mode=permission_mode,
            ai_provider=ai_provider,
        ))
    writer_results = await asyncio.gather(*writer_tasks, return_exceptions=True)

    succeeded = sum(1 for r in writer_results if isinstance(r, dict) and r.get("success"))
    failed = len(writer_results) - succeeded
    app.note(
        f"Issue writers complete: {succeeded} succeeded, {failed} failed",
        tags=["pipeline", "issue_writers", "complete"],
    )

    # 6. Write rationale to disk
    rationale_path = os.path.join(base, "rationale.md")
    with open(rationale_path, "w", encoding="utf-8") as f:
        f.write(rationale)

    app.note("Pipeline complete", tags=["pipeline", "complete"])

    return PlanResult(
        prd=prd,
        architecture=arch,
        review=review,
        issues=issues,
        levels=levels,
        file_conflicts=file_conflicts,
        artifacts_dir=base,
        rationale=rationale,
    ).model_dump()


@app.reasoner()
async def execute(
    plan_result: dict,
    repo_path: str,
    execute_fn_target: str = "",
    config: dict = {},
    git_config: dict | None = None,
    resume: bool = False,
    ai_provider: str = "claude",
) -> dict:
    """Execute a planned DAG with self-healing replanning.

    Args:
        plan_result: Output from the ``plan`` reasoner.
        repo_path: Path to the target repository.
        execute_fn_target: Optional remote agent target (e.g. "coder-agent.code_issue").
            If empty, uses the built-in coding loop (coder → QA/review → synthesizer).
        config: ExecutionConfig overrides as a dict.
        git_config: Optional git configuration from ``run_git_init``. Enables
            branch-per-issue workflow when provided.
        resume: If True, attempt to resume from a checkpoint file.
    """
    from execution.dag_executor import run_dag
    from execution.schemas import ExecutionConfig

    effective_config = dict(config) if config else {}
    if ai_provider and "ai_provider" not in effective_config:
        effective_config["ai_provider"] = ai_provider
    exec_config = ExecutionConfig(**effective_config) if effective_config else ExecutionConfig()

    if execute_fn_target:
        # External coder agent (existing path)
        async def execute_fn(issue, dag_state):
            return await app.call(
                execute_fn_target,
                issue=issue,
                repo_path=dag_state.repo_path,
            )
    else:
        # Built-in coding loop — dag_executor will use call_fn + coding_loop
        execute_fn = None

    state = await run_dag(
        plan_result=plan_result,
        repo_path=repo_path,
        execute_fn=execute_fn,
        config=exec_config,
        note_fn=app.note,
        call_fn=app.call,
        node_id=NODE_ID,
        git_config=git_config,
        resume=resume,
    )
    return state.model_dump()


@app.reasoner()
async def resume_build(
    repo_path: str,
    artifacts_dir: str = ".artifacts",
    config: dict = {},
    git_config: dict | None = None,
) -> dict:
    """Resume a crashed build from the last checkpoint.

    Loads the plan result from artifacts and calls execute with resume=True.
    """
    import json

    base = os.path.join(os.path.abspath(repo_path), artifacts_dir)

    # Reconstruct plan_result from saved artifacts
    plan_path = os.path.join(base, "execution", "checkpoint.json")
    if not os.path.exists(plan_path):
        raise RuntimeError(
            f"No checkpoint found at {plan_path}. Cannot resume."
        )

    # Load the original plan artifacts to reconstruct plan_result
    prd_path = os.path.join(base, "plan", "prd.md")
    arch_path = os.path.join(base, "plan", "architecture.md")
    rationale_path = os.path.join(base, "rationale.md")

    # We need the plan_result dict — reconstruct from checkpoint's DAGState
    with open(plan_path, "r") as f:
        checkpoint = json.load(f)

    plan_result = {
        "prd": {},  # Not needed for resume — DAGState has summaries
        "architecture": {},
        "review": {},
        "issues": checkpoint.get("all_issues", []),
        "levels": checkpoint.get("levels", []),
        "file_conflicts": [],
        "artifacts_dir": checkpoint.get("artifacts_dir", base),
        "rationale": checkpoint.get("original_plan_summary", ""),
    }

    app.note("Resuming build from checkpoint", tags=["build", "resume"])

    result = await app.call(
        f"{NODE_ID}.execute",
        plan_result=plan_result,
        repo_path=repo_path,
        config=config,
        git_config=git_config,
        resume=True,
    )

    return result


if __name__ == "__main__":
    app.run(port=8003, host="0.0.0.0")
