"""AgentField app for the SWE planning and execution pipeline.

Exposes:
  - ``build``: end-to-end plan → execute → verify (single entry point)
  - ``plan``: orchestrates product_manager → architect ↔ tech_lead → sprint_planner
  - ``execute``: runs a planned DAG with self-healing replanning
"""

from __future__ import annotations

import asyncio
import os
import re
import subprocess

from swe_af.reasoners import router
from swe_af.reasoners.pipeline import _assign_sequence_numbers, _compute_levels, _validate_file_conflicts
from swe_af.reasoners.schemas import PlanResult, ReviewResult

from agentfield import Agent
from swe_af.execution.envelope import unwrap_call_result as _unwrap

NODE_ID = os.getenv("NODE_ID", "swe-planner")

app = Agent(
    node_id=NODE_ID,
    version="1.0.0",
    description="Autonomous SWE planning pipeline",
    agentfield_server=os.getenv("AGENTFIELD_SERVER", "http://localhost:8080"),
    api_key=os.getenv("AGENTFIELD_API_KEY"),
)

app.include_router(router)


def _repo_name_from_url(url: str) -> str:
    """Extract repo name from a GitHub URL for auto-deriving repo_path."""
    # https://github.com/user/my-project.git → my-project
    match = re.search(r"/([^/]+?)(?:\.git)?$", url.rstrip("/"))
    return match.group(1) if match else "repo"


@app.reasoner()
async def build(
    goal: str,
    repo_path: str = "",
    repo_url: str = "",
    artifacts_dir: str = ".artifacts",
    additional_context: str = "",
    config: dict | None = None,
    execute_fn_target: str = "",
    max_turns: int = 0,
    permission_mode: str = "",
    enable_learning: bool = False,
) -> dict:
    """End-to-end: plan → execute → verify → optional fix cycle.

    This is the single entry point. Pass a goal, get working code.

    If ``repo_url`` is provided and ``repo_path`` is empty, the repo is cloned
    into ``/workspaces/<repo-name>`` automatically (useful in Docker).
    """
    from swe_af.execution.schemas import BuildConfig, BuildResult

    cfg = BuildConfig(**config) if config else BuildConfig()

    # Allow repo_url from config or direct parameter
    if repo_url:
        cfg.repo_url = repo_url

    # Auto-derive repo_path from repo_url when not specified
    if cfg.repo_url and not repo_path:
        repo_path = f"/workspaces/{_repo_name_from_url(cfg.repo_url)}"

    if not repo_path:
        raise ValueError("Either repo_path or repo_url must be provided")

    # Clone if repo_url is set and target doesn't exist yet
    if cfg.repo_url and not os.path.exists(os.path.join(repo_path, ".git")):
        app.note(f"Cloning {cfg.repo_url} → {repo_path}", tags=["build", "clone"])
        os.makedirs(repo_path, exist_ok=True)
        clone_result = subprocess.run(
            ["git", "clone", cfg.repo_url, repo_path],
            capture_output=True,
            text=True,
        )
        if clone_result.returncode != 0:
            err = clone_result.stderr.strip()
            app.note(f"Clone failed (exit {clone_result.returncode}): {err}", tags=["build", "clone", "error"])
            raise RuntimeError(f"git clone failed (exit {clone_result.returncode}): {err}")
    else:
        # Ensure repo_path exists even when no repo_url is provided (fresh init case)
        # This is needed because planning agents may need to read the repo in parallel with git_init
        os.makedirs(repo_path, exist_ok=True)

    if execute_fn_target:
        cfg.execute_fn_target = execute_fn_target
    if permission_mode:
        cfg.permission_mode = permission_mode
    if enable_learning:
        cfg.enable_learning = True
    if max_turns > 0:
        cfg.agent_max_turns = max_turns

    # Resolve runtime + flat model config once for this build.
    resolved = cfg.resolved_models()

    app.note("Build starting", tags=["build", "start"])

    # Compute absolute artifacts directory path for logging
    abs_artifacts_dir = os.path.join(os.path.abspath(repo_path), artifacts_dir)

    # 1. PLAN + GIT INIT (concurrent — no data dependency between them)
    app.note("Phase 1: Planning + Git init (parallel)", tags=["build", "parallel"])

    plan_coro = app.call(
        f"{NODE_ID}.plan",
        goal=goal,
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        additional_context=additional_context,
        max_review_iterations=cfg.max_review_iterations,
        pm_model=resolved["pm_model"],
        architect_model=resolved["architect_model"],
        tech_lead_model=resolved["tech_lead_model"],
        sprint_planner_model=resolved["sprint_planner_model"],
        issue_writer_model=resolved["issue_writer_model"],
        permission_mode=cfg.permission_mode,
        ai_provider=cfg.ai_provider,
    )

    # Git init with retry logic
    MAX_GIT_INIT_RETRIES = cfg.git_init_max_retries
    git_init = None
    previous_error = None
    raw_plan = None

    for attempt in range(1, MAX_GIT_INIT_RETRIES + 1):
        app.note(
            f"Git init attempt {attempt}/{MAX_GIT_INIT_RETRIES}"
            + (f" (previous error: {previous_error})" if previous_error else ""),
            tags=["build", "git_init", "retry"],
        )

        git_init_coro = app.call(
            f"{NODE_ID}.run_git_init",
            repo_path=repo_path,
            goal=goal,
            artifacts_dir=abs_artifacts_dir,
            model=resolved["git_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=cfg.ai_provider,
            previous_error=previous_error,
        )

        # Run planning only on first attempt, then just git_init on retries
        if attempt == 1:
            raw_plan, raw_git = await asyncio.gather(plan_coro, git_init_coro)
        else:
            raw_git = await git_init_coro

        # git_init failures are non-fatal — unwrap but don't raise
        try:
            git_init = _unwrap(raw_git, "run_git_init")
        except RuntimeError:
            git_init = raw_git if isinstance(raw_git, dict) else {"success": False, "error_message": str(raw_git)}

        if git_init.get("success"):
            app.note(
                f"Git init succeeded on attempt {attempt}",
                tags=["build", "git_init", "success"],
            )
            break
        else:
            previous_error = git_init.get("error_message", "unknown error")
            app.note(
                f"Git init attempt {attempt} failed: {previous_error}",
                tags=["build", "git_init", "failed"],
            )

            if attempt == MAX_GIT_INIT_RETRIES:
                app.note(
                    f"Git init failed after {MAX_GIT_INIT_RETRIES} attempts — "
                    "proceeding without git workflow",
                    tags=["build", "git_init", "exhausted"],
                )

            # Brief delay before retry (except on last attempt)
            if attempt < MAX_GIT_INIT_RETRIES:
                await asyncio.sleep(cfg.git_init_retry_delay)

    # Unwrap plan result (should have been set on first attempt)
    plan_result = _unwrap(raw_plan, "plan")

    git_config = None
    if git_init.get("success"):
        git_config = {
            "integration_branch": git_init["integration_branch"],
            "original_branch": git_init["original_branch"],
            "initial_commit_sha": git_init["initial_commit_sha"],
            "mode": git_init["mode"],
            "remote_url": git_init.get("remote_url", ""),
            "remote_default_branch": git_init.get("remote_default_branch", ""),
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
    exec_config = cfg.to_execution_config_dict()

    dag_result = _unwrap(await app.call(
        f"{NODE_ID}.execute",
        plan_result=plan_result,
        repo_path=repo_path,
        execute_fn_target=cfg.execute_fn_target,
        config=exec_config,
        git_config=git_config,
    ), "execute")

    # 3. VERIFY
    verification = None
    for cycle in range(cfg.max_verify_fix_cycles + 1):
        app.note(f"Verification cycle {cycle}", tags=["build", "verify"])
        verification = _unwrap(await app.call(
            f"{NODE_ID}.run_verifier",
            prd=plan_result["prd"],
            repo_path=repo_path,
            artifacts_dir=plan_result.get("artifacts_dir", artifacts_dir),
            completed_issues=[r for r in dag_result.get("completed_issues", [])],
            failed_issues=[r for r in dag_result.get("failed_issues", [])],
            skipped_issues=dag_result.get("skipped_issues", []),
            model=resolved["verifier_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=cfg.ai_provider,
        ), "run_verifier")

        if verification.get("passed", False) or cycle >= cfg.max_verify_fix_cycles:
            break

        # Verification failed — generate targeted fix issues
        failed_criteria = [
            c for c in verification.get("criteria_results", [])
            if not c.get("passed", True)
        ]

        if not failed_criteria:
            app.note("Verification failed but no specific criteria failures found", tags=["build", "verify"])
            break

        app.note(
            f"Verification failed ({len(failed_criteria)} criteria), "
            f"{cfg.max_verify_fix_cycles - cycle} fix cycles remaining",
            tags=["build", "verify", "retry"],
        )

        # Generate fix issues from failed criteria
        fix_result = _unwrap(await app.call(
            f"{NODE_ID}.generate_fix_issues",
            failed_criteria=failed_criteria,
            dag_state=dag_result,
            prd=plan_result["prd"],
            artifacts_dir=plan_result.get("artifacts_dir", artifacts_dir),
            model=resolved["verifier_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=cfg.ai_provider,
        ), "generate_fix_issues")

        fix_issues = fix_result.get("fix_issues", [])
        fix_debt = fix_result.get("debt_items", [])

        # Record unfixable criteria as debt
        for debt in fix_debt:
            dag_result.setdefault("accumulated_debt", []).append({
                "type": "unmet_acceptance_criterion",
                "criterion": debt.get("criterion", ""),
                "reason": debt.get("reason", ""),
                "severity": debt.get("severity", "high"),
            })

        if fix_issues:
            # Build a mini plan from fix issues and execute them
            fix_plan = {
                "prd": plan_result["prd"],
                "architecture": plan_result.get("architecture", {}),
                "review": plan_result.get("review", {}),
                "issues": fix_issues,
                "levels": [[fi.get("name", f"fix-{i}") for i, fi in enumerate(fix_issues)]],
                "file_conflicts": [],
                "artifacts_dir": plan_result.get("artifacts_dir", artifacts_dir),
                "rationale": f"Fix issues for verification cycle {cycle + 1}",
            }
            dag_result = _unwrap(await app.call(
                f"{NODE_ID}.execute",
                plan_result=fix_plan,
                repo_path=repo_path,
                config=exec_config,
                git_config=git_config,
            ), "execute_fixes")
            continue  # Re-verify
        else:
            app.note("No fixable issues generated — accepting with debt", tags=["build", "verify"])
            break

    success = verification.get("passed", False) if verification else False
    completed = len(dag_result.get("completed_issues", []))
    total = len(dag_result.get("all_issues", []))

    app.note(
        f"Build {'succeeded' if success else 'completed with issues'}: "
        f"{completed}/{total} issues, verification={'passed' if success else 'failed'}",
        tags=["build", "complete"],
    )

    # Capture plan docs before finalize cleans up .artifacts/
    _plan_dir = os.path.join(
        plan_result.get("artifacts_dir", ""), "plan"
    )
    prd_markdown = ""
    architecture_markdown = ""
    for _name, _var in [("prd.md", "prd_markdown"), ("architecture.md", "architecture_markdown")]:
        _fpath = os.path.join(_plan_dir, _name)
        if os.path.isfile(_fpath):
            try:
                with open(_fpath, "r", encoding="utf-8") as _f:
                    if _var == "prd_markdown":
                        prd_markdown = _f.read()
                    else:
                        architecture_markdown = _f.read()
            except OSError:
                pass

    # 3b. FINALIZE — clean up repo artifacts before PR
    app.note("Phase 3b: Repo finalization", tags=["build", "finalize"])
    try:
        finalize_result = _unwrap(await app.call(
            f"{NODE_ID}.run_repo_finalize",
            repo_path=repo_path,
            artifacts_dir=plan_result.get("artifacts_dir", artifacts_dir),
            model=resolved["git_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=cfg.ai_provider,
        ), "run_repo_finalize")
        if finalize_result.get("success"):
            app.note(
                f"Repo finalized: {finalize_result.get('summary', '')}",
                tags=["build", "finalize", "complete"],
            )
        else:
            app.note(
                f"Repo finalize incomplete: {finalize_result.get('summary', '')}",
                tags=["build", "finalize", "warning"],
            )
    except Exception as e:
        app.note(
            f"Repo finalize failed (non-blocking): {e}",
            tags=["build", "finalize", "error"],
        )

    # 4. PUSH & DRAFT PR (if repo has a remote and PR creation is enabled)
    pr_url = ""
    remote_url = git_config.get("remote_url", "") if git_config else ""
    if remote_url and cfg.enable_github_pr:
        app.note("Phase 4: Push + Draft PR", tags=["build", "github_pr"])
        base_branch = (
            cfg.github_pr_base
            or (git_config.get("remote_default_branch") if git_config else "")
            or "main"
        )
        build_summary = (
            f"{'Success' if success else 'Partial'}: {completed}/{total} issues completed"
            + (f", verification: {verification.get('summary', '')}" if verification else "")
        )
        try:
            pr_result = _unwrap(await app.call(
                f"{NODE_ID}.run_github_pr",
                repo_path=repo_path,
                integration_branch=git_config["integration_branch"],
                base_branch=base_branch,
                goal=goal,
                build_summary=build_summary,
                completed_issues=dag_result.get("completed_issues", []),
                accumulated_debt=dag_result.get("accumulated_debt", []),
                artifacts_dir=plan_result.get("artifacts_dir", artifacts_dir),
                model=resolved["git_model"],
                permission_mode=cfg.permission_mode,
                ai_provider=cfg.ai_provider,
            ), "run_github_pr")
            pr_url = pr_result.get("pr_url", "")
            if pr_url:
                app.note(f"Draft PR created: {pr_url}", tags=["build", "github_pr", "complete"])

                # Programmatically append plan docs to PR body
                if prd_markdown or architecture_markdown:
                    try:
                        current_body = subprocess.run(
                            ["gh", "pr", "view", str(pr_result.get("pr_number", 0)),
                             "--json", "body", "--jq", ".body"],
                            cwd=repo_path, capture_output=True, text=True, check=True,
                        ).stdout.strip()

                        plan_sections = "\n\n---\n"
                        if prd_markdown:
                            plan_sections += (
                                "\n<details><summary>📋 PRD (Product Requirements Document)"
                                "</summary>\n\n"
                                + prd_markdown
                                + "\n\n</details>\n"
                            )
                        if architecture_markdown:
                            plan_sections += (
                                "\n<details><summary>🏗️ Architecture</summary>\n\n"
                                + architecture_markdown
                                + "\n\n</details>\n"
                            )

                        new_body = current_body + plan_sections

                        subprocess.run(
                            ["gh", "pr", "edit", str(pr_result.get("pr_number", 0)),
                             "--body", new_body],
                            cwd=repo_path, capture_output=True, text=True, check=True,
                        )
                        app.note(
                            "Plan docs appended to PR body",
                            tags=["build", "github_pr", "plan_docs"],
                        )
                    except subprocess.CalledProcessError as e:
                        app.note(
                            f"Failed to append plan docs to PR (non-fatal): {e}",
                            tags=["build", "github_pr", "plan_docs", "warning"],
                        )
            else:
                app.note(
                    f"PR creation failed: {pr_result.get('error_message', 'unknown')}",
                    tags=["build", "github_pr", "error"],
                )
        except Exception as e:
            app.note(f"PR creation failed: {e}", tags=["build", "github_pr", "error"])

    return BuildResult(
        plan_result=plan_result,
        dag_state=dag_result,
        verification=verification,
        success=success,
        summary=f"{'Success' if success else 'Partial'}: {completed}/{total} issues completed"
                + (f", verification: {verification.get('summary', '')}" if verification else ""),
        pr_url=pr_url,
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
    prd = _unwrap(await app.call(
        f"{NODE_ID}.run_product_manager",
        goal=goal,
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        additional_context=additional_context,
        model=pm_model,
        permission_mode=permission_mode,
        ai_provider=ai_provider,
    ), "run_product_manager")

    # 2. Architect designs the solution
    app.note("Phase 2: Architect", tags=["pipeline", "architect"])
    arch = _unwrap(await app.call(
        f"{NODE_ID}.run_architect",
        prd=prd,
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        model=architect_model,
        permission_mode=permission_mode,
        ai_provider=ai_provider,
    ), "run_architect")

    # 3. Tech Lead review loop
    review = None
    for i in range(max_review_iterations + 1):
        app.note(f"Phase 3: Tech Lead review (iteration {i})", tags=["pipeline", "tech_lead"])
        review = _unwrap(await app.call(
            f"{NODE_ID}.run_tech_lead",
            prd=prd,
            repo_path=repo_path,
            artifacts_dir=artifacts_dir,
            revision_number=i,
            model=tech_lead_model,
            permission_mode=permission_mode,
            ai_provider=ai_provider,
        ), "run_tech_lead")
        if review["approved"]:
            break
        if i < max_review_iterations:
            app.note(f"Architecture revision {i + 1}", tags=["pipeline", "revision"])
            arch = _unwrap(await app.call(
                f"{NODE_ID}.run_architect",
                prd=prd,
                repo_path=repo_path,
                artifacts_dir=artifacts_dir,
                feedback=review["feedback"],
                model=architect_model,
                permission_mode=permission_mode,
                ai_provider=ai_provider,
            ), "run_architect (revision)")

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
    sprint_result = _unwrap(await app.call(
        f"{NODE_ID}.run_sprint_planner",
        prd=prd,
        architecture=arch,
        repo_path=repo_path,
        artifacts_dir=artifacts_dir,
        model=sprint_planner_model,
        permission_mode=permission_mode,
        ai_provider=ai_provider,
    ), "run_sprint_planner")
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
    config: dict | None = None,
    git_config: dict | None = None,
    resume: bool = False,
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
    from swe_af.execution.dag_executor import run_dag
    from swe_af.execution.schemas import ExecutionConfig

    effective_config = dict(config) if config else {}
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
    config: dict | None = None,
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


def main():
    """Entry point for ``python -m swe_af`` and the ``swe-af`` console script."""
    app.run(port=8003, host="0.0.0.0")


if __name__ == "__main__":
    main()
