"""AgentField app for the SWE planning and execution pipeline.

Exposes:
  - ``build``: end-to-end plan → execute → verify (single entry point)
  - ``plan``: orchestrates product_manager → architect ↔ tech_lead → sprint_planner
  - ``execute``: runs a planned DAG with self-healing replanning
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import uuid

from swe_af.reasoners import router
from swe_af.reasoners.pipeline import _assign_sequence_numbers, _compute_levels, _validate_file_conflicts
from swe_af.reasoners.schemas import PlanResult, ReviewResult

from agentfield import Agent
from swe_af.execution.envelope import unwrap_call_result as _unwrap
from swe_af.execution.schemas import (
    BuildConfig,
    BuildResult,
    RepoPRResult,
    WorkspaceManifest,
    WorkspaceRepo,
    _derive_repo_name as _repo_name_from_url,
)

NODE_ID = os.getenv("NODE_ID", "swe-planner")

app = Agent(
    node_id=NODE_ID,
    version="1.0.0",
    description="Autonomous SWE planning pipeline",
    agentfield_server=os.getenv("AGENTFIELD_SERVER", "http://localhost:8080"),
    api_key=os.getenv("AGENTFIELD_API_KEY"),
)

app.include_router(router)


async def _clone_repos(
    cfg: BuildConfig,
    artifacts_dir: str,
) -> WorkspaceManifest:
    """Clone all repos from cfg.repos concurrently. Returns a WorkspaceManifest.

    Parameters:
        cfg: BuildConfig with .repos list populated. len(cfg.repos) >= 1.
        artifacts_dir: Absolute path used to derive workspace_root as its parent.

    Returns:
        WorkspaceManifest with one WorkspaceRepo per RepoSpec.
        All WorkspaceRepo.git_init_result fields are None at this stage
        (populated later by _init_all_repos in dag_executor.py).

    Raises:
        RuntimeError: If any git clone subprocess fails. Partially-cloned
            directories are removed (shutil.rmtree) before raising, so no
            orphaned workspace directories remain.

    Concurrency model:
        asyncio.gather([asyncio.to_thread(blocking_clone), ...]) for all N repos.
        Branch resolution also runs concurrently via asyncio.to_thread.
    """
    import shutil

    workspace_root = os.path.join(os.path.dirname(artifacts_dir), "workspace")
    os.makedirs(workspace_root, exist_ok=True)

    cloned_paths: list[str] = []

    async def _clone_single(spec: WorkspaceRepo) -> tuple[str, str]:  # type: ignore[type-arg]
        """Clone or resolve one repo. Returns (repo_name, absolute_path)."""
        name = (
            spec.mount_point
            or (_repo_name_from_url(spec.repo_url) if spec.repo_url
                else os.path.basename(spec.repo_path.rstrip("/")))
        )
        dest = os.path.join(workspace_root, name)

        # If repo_path given, use it directly — no clone needed
        if spec.repo_path:
            return name, spec.repo_path

        git_dir = os.path.join(dest, ".git")
        if spec.repo_url and not os.path.exists(git_dir):
            os.makedirs(dest, exist_ok=True)
            cmd = ["git", "clone", spec.repo_url, dest]
            if spec.branch:
                cmd += ["--branch", spec.branch]

            def _run() -> subprocess.CompletedProcess:  # type: ignore[type-arg]
                return subprocess.run(cmd, capture_output=True, text=True)

            proc = await asyncio.to_thread(_run)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"git clone {spec.repo_url!r} failed "
                    f"(exit {proc.returncode}): {proc.stderr.strip()}"
                )
            cloned_paths.append(dest)

        return name, dest

    async def _resolve_branch(spec: WorkspaceRepo, path: str) -> str:  # type: ignore[type-arg]
        """Resolve actual checked-out branch via git rev-parse.

        Falls back to spec.branch or 'HEAD' on error.
        """
        def _run() -> str:
            r = subprocess.run(
                ["git", "-C", path, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True, text=True,
            )
            if r.returncode == 0 and r.stdout.strip():
                return r.stdout.strip()
            return spec.branch or "HEAD"
        return await asyncio.to_thread(_run)

    # Clone all repos concurrently
    clone_tasks = [_clone_single(spec) for spec in cfg.repos]
    clone_results = await asyncio.gather(*clone_tasks, return_exceptions=True)

    # Check for failures, cleanup partial clones
    errors = [
        (i, r) for i, r in enumerate(clone_results) if isinstance(r, Exception)
    ]
    if errors:
        for p in cloned_paths:
            shutil.rmtree(p, ignore_errors=True)
        msgs = "; ".join(str(r) for _, r in errors)
        raise RuntimeError(f"Multi-repo clone failed: {msgs}")

    # Resolve branches concurrently
    branch_tasks = [
        _resolve_branch(cfg.repos[i], clone_results[i][1])  # type: ignore[index]
        for i in range(len(cfg.repos))
    ]
    branches = await asyncio.gather(*branch_tasks, return_exceptions=True)

    # Build WorkspaceRepo list
    repos: list[WorkspaceRepo] = []
    primary_repo_name = ""

    for i, spec in enumerate(cfg.repos):
        name, path = clone_results[i]  # type: ignore[misc]
        branch = branches[i] if isinstance(branches[i], str) else (spec.branch or "HEAD")
        ws_repo = WorkspaceRepo(
            repo_name=name,
            repo_url=spec.repo_url,
            role=spec.role,
            absolute_path=path,
            branch=branch,
            sparse_paths=spec.sparse_paths,
            create_pr=spec.create_pr,
            git_init_result=None,
        )
        repos.append(ws_repo)
        if spec.role == "primary":
            primary_repo_name = name

    return WorkspaceManifest(
        workspace_root=workspace_root,
        repos=repos,
        primary_repo_name=primary_repo_name,
    )


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
    git_dir = os.path.join(repo_path, ".git")
    if cfg.repo_url and not os.path.exists(git_dir):
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
    elif cfg.repo_url and os.path.exists(git_dir):
        # Repo already cloned by a prior build — reset to remote default branch
        # so git_init creates the integration branch from a clean baseline.
        default_branch = cfg.github_pr_base or "main"
        app.note(
            f"Repo already exists at {repo_path} — resetting to origin/{default_branch}",
            tags=["build", "clone", "reset"],
        )

        # Remove stale worktrees on disk before touching branches
        worktrees_dir = os.path.join(repo_path, ".worktrees")
        if os.path.isdir(worktrees_dir):
            import shutil
            shutil.rmtree(worktrees_dir, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_path, capture_output=True, text=True,
        )

        # Fetch latest remote state
        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if fetch.returncode != 0:
            app.note(f"git fetch failed: {fetch.stderr.strip()}", tags=["build", "clone", "error"])

        # Force-checkout default branch (handles dirty working tree from crashed builds)
        subprocess.run(
            ["git", "checkout", "-f", default_branch],
            cwd=repo_path, capture_output=True, text=True,
        )
        reset = subprocess.run(
            ["git", "reset", "--hard", f"origin/{default_branch}"],
            cwd=repo_path, capture_output=True, text=True,
        )
        if reset.returncode != 0:
            # Hard reset failed — nuke and re-clone as last resort
            app.note(
                f"Reset to origin/{default_branch} failed — re-cloning",
                tags=["build", "clone", "reclone"],
            )
            import shutil
            shutil.rmtree(repo_path, ignore_errors=True)
            os.makedirs(repo_path, exist_ok=True)
            clone_result = subprocess.run(
                ["git", "clone", cfg.repo_url, repo_path],
                capture_output=True, text=True,
            )
            if clone_result.returncode != 0:
                err = clone_result.stderr.strip()
                raise RuntimeError(f"git re-clone failed: {err}")
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

    # Unique ID for this build — namespaces git branches/worktrees to prevent
    # collisions when multiple builds run concurrently on the same repository.
    build_id = uuid.uuid4().hex[:8]

    app.note(f"Build starting (build_id={build_id})", tags=["build", "start"])

    # Compute absolute artifacts directory path for logging
    abs_artifacts_dir = os.path.join(os.path.abspath(repo_path), artifacts_dir)

    # Multi-repo path: clone all repos concurrently
    manifest: WorkspaceManifest | None = None
    if len(cfg.repos) > 1:
        app.note(
            f"Cloning {len(cfg.repos)} repos concurrently",
            tags=["build", "clone", "multi-repo"],
        )
        manifest = await _clone_repos(cfg, abs_artifacts_dir)
        # Use primary repo as the canonical repo_path
        repo_path = manifest.primary_repo.absolute_path
        app.note(
            f"Multi-repo workspace ready: {manifest.workspace_root}",
            tags=["build", "clone", "multi-repo", "complete"],
        )

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
            build_id=build_id,
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
        build_id=build_id,
        workspace_manifest=manifest.model_dump() if manifest else None,
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
    pr_results: list[RepoPRResult] = []
    build_summary = (
        f"{'Success' if success else 'Partial'}: {completed}/{total} issues completed"
        + (f", verification: {verification.get('summary', '')}" if verification else "")
    )

    if manifest and len(manifest.repos) > 1:
        # Multi-repo: one PR per repo where create_pr=True
        app.note("Phase 4: Multi-repo Push + Draft PRs", tags=["build", "github_pr", "multi-repo"])
        for ws_repo in manifest.repos:
            if not ws_repo.create_pr or not cfg.enable_github_pr:
                continue
            repo_git_init = ws_repo.git_init_result or {}
            repo_remote_url = repo_git_init.get("remote_url", "") or ws_repo.repo_url
            if not repo_remote_url:
                continue
            repo_integration_branch = repo_git_init.get("integration_branch", "")
            if not repo_integration_branch:
                continue
            repo_base_branch = (
                cfg.github_pr_base
                or repo_git_init.get("remote_default_branch", "")
                or "main"
            )
            try:
                pr_r = _unwrap(await app.call(
                    f"{NODE_ID}.run_github_pr",
                    repo_path=ws_repo.absolute_path,
                    integration_branch=repo_integration_branch,
                    base_branch=repo_base_branch,
                    goal=goal,
                    build_summary=build_summary,
                    completed_issues=[
                        r for r in dag_result.get("completed_issues", [])
                        if not r.get("repo_name") or r.get("repo_name") == ws_repo.repo_name
                    ],
                    accumulated_debt=dag_result.get("accumulated_debt", []),
                    artifacts_dir=plan_result.get("artifacts_dir", artifacts_dir),
                    model=resolved["git_model"],
                    permission_mode=cfg.permission_mode,
                    ai_provider=cfg.ai_provider,
                ), "run_github_pr")
                pr_results.append(RepoPRResult(
                    repo_name=ws_repo.repo_name,
                    repo_url=ws_repo.repo_url,
                    success=pr_r.get("success", False),
                    pr_url=pr_r.get("pr_url", ""),
                    pr_number=pr_r.get("pr_number", 0),
                    error_message=pr_r.get("error_message", ""),
                ))
                if pr_r.get("pr_url"):
                    app.note(
                        f"Draft PR created for {ws_repo.repo_name}: {pr_r.get('pr_url')}",
                        tags=["build", "github_pr", "complete"],
                    )
            except Exception as e:
                pr_results.append(RepoPRResult(
                    repo_name=ws_repo.repo_name,
                    repo_url=ws_repo.repo_url,
                    success=False,
                    error_message=str(e),
                ))
                app.note(
                    f"PR creation failed for {ws_repo.repo_name}: {e}",
                    tags=["build", "github_pr", "error"],
                )
    else:
        # Single-repo: existing PR logic, wrap result in RepoPRResult
        remote_url = git_config.get("remote_url", "") if git_config else ""
        if remote_url and cfg.enable_github_pr:
            app.note("Phase 4: Push + Draft PR", tags=["build", "github_pr"])
            base_branch = (
                cfg.github_pr_base
                or (git_config.get("remote_default_branch") if git_config else "")
                or "main"
            )
            pr_url = ""
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
                if pr_url:
                    pr_results.append(RepoPRResult(
                        repo_name=_repo_name_from_url(cfg.repo_url) if cfg.repo_url else "repo",
                        repo_url=cfg.repo_url,
                        success=True,
                        pr_url=pr_url,
                        pr_number=pr_result.get("pr_number", 0),
                    ))
            except Exception as e:
                app.note(f"PR creation failed: {e}", tags=["build", "github_pr", "error"])

    return BuildResult(
        plan_result=plan_result,
        dag_state=dag_result,
        verification=verification,
        success=success,
        summary=f"{'Success' if success else 'Partial'}: {completed}/{total} issues completed"
                + (f", verification: {verification.get('summary', '')}" if verification else ""),
        pr_results=pr_results,
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
    build_id: str = "",
    workspace_manifest: dict | None = None,
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
        workspace_manifest: Optional WorkspaceManifest.model_dump() for multi-repo builds.
            None for single-repo builds (backward compat). When provided, enables
            per-repo git init and merger dispatch.
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
        build_id=build_id,
        workspace_manifest=workspace_manifest,
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
