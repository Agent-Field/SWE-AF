"""swe_af.fast.app — FastBuild Agent entry point.

Exposes:
  - ``app``: Agent instance with node_id='swe-fast'
  - ``build``: end-to-end fast build reasoner
  - ``main``: entry point for ``python -m swe_af.fast`` and the ``swe-fast`` console script
"""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess

from dotenv import load_dotenv

load_dotenv()

from agentfield import Agent
from swe_af.execution.envelope import unwrap_call_result as _unwrap
from swe_af.execution.schemas import _workspace_root
from swe_af.fast import fast_router
from swe_af.fast.schemas import FastBuildConfig, FastBuildResult, fast_resolve_models
from swe_af.surface import TAG_ENTRYPOINT

NODE_ID = os.getenv("NODE_ID", "swe-fast")

app = Agent(
    node_id=NODE_ID,
    version="1.0.0",
    description="Speed-optimized SWE agent — single-pass planning, sequential execution",
    agentfield_server=os.getenv("AGENTFIELD_SERVER", "http://localhost:8080"),
    api_key=os.getenv("AGENTFIELD_API_KEY"),
)

app.include_router(fast_router)

# Include the planner's execution router so that router.note() calls inside
# the original execution_agents functions (run_coder, run_verifier, etc.)
# work when delegated to via the thin wrappers.
from swe_af.reasoners import router as _execution_router  # noqa: E402
app.include_router(_execution_router)

# Issue-level entry point (implement_issue) — shared with swe-planner so a
# main harness can delegate scoped issues to either node.
from swe_af.issue import issue_router as _issue_router  # noqa: E402
app.include_router(_issue_router)


def _repo_name_from_url(url: str) -> str:
    """Extract repo name from a GitHub URL."""
    match = re.search(r"/([^/]+?)(?:\.git)?$", url.rstrip("/"))
    return match.group(1) if match else "repo"


def _runtime_to_provider(runtime: str) -> str:
    """Map runtime string to ai_provider string, preserving legacy fast fallback."""
    if runtime == "claude_code":
        return "claude"
    if runtime == "codex":
        return "codex"
    return "opencode"


_CREDENTIAL_URL_RE = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/@\s]+@")


def _redact_credentials(text: str, repo_url: str) -> str:
    """Remove URL userinfo from text without changing the URL passed to git."""
    safe_url = _CREDENTIAL_URL_RE.sub(r"\1***@", repo_url)
    redacted = text.replace(repo_url, safe_url) if repo_url else text

    # Git normally repeats the full URL in diagnostics. Also redact a password
    # if a transport happens to report that component separately.
    userinfo = _CREDENTIAL_URL_RE.search(repo_url)
    if userinfo:
        raw_userinfo = repo_url[len(userinfo.group(1)) : repo_url.find("@")]
        if ":" in raw_userinfo:
            password = raw_userinfo.split(":", 1)[1]
            if password:
                redacted = redacted.replace(password, "***")
    return _CREDENTIAL_URL_RE.sub(r"\1***@", redacted)


def _normalized_remote(url: str) -> str:
    """Normalize only the suffixes the repository-preparation contract allows."""
    return url.rstrip("/").removesuffix(".git")


def _clone_repo(repo_url: str, repo_path: str, *, reclone: bool = False) -> None:
    os.makedirs(os.path.dirname(repo_path) or ".", exist_ok=True)
    clone_result = subprocess.run(
        ["git", "clone", repo_url, repo_path],
        check=False,
        capture_output=True,
        text=True,
    )
    if clone_result.returncode == 0:
        return

    err = _redact_credentials(clone_result.stderr.strip(), repo_url)
    if reclone:
        raise RuntimeError(f"git re-clone failed: {err}")
    app.note(
        f"Clone failed (exit {clone_result.returncode}): {err}",
        tags=["fast_build", "clone", "error"],
    )
    raise RuntimeError(f"git clone failed (exit {clone_result.returncode}): {err}")


def _prepare_repo(
    repo_url: str,
    repo_path: str,
    default_branch: str,
    path_was_derived: bool,
) -> None:
    """Clone or reset a remote repository, or create a local-only workspace."""
    git_dir = os.path.join(repo_path, ".git")
    if repo_url and not os.path.exists(git_dir):
        # Leftovers the node itself made — a workspace from a build that died
        # before git init, or from before this node cloned at all — would make
        # `git clone` refuse a non-empty destination. Derived paths are ours to
        # clear; a path the caller chose is not (the clone then fails loudly).
        if path_was_derived and os.path.isdir(repo_path) and os.listdir(repo_path):
            app.note(
                f"Clearing stale workspace at {repo_path} (no git repo) before cloning",
                tags=["fast_build", "clone", "reclone"],
            )
            shutil.rmtree(repo_path, ignore_errors=True)
        app.note(
            f"Cloning {_redact_credentials(repo_url, repo_url)} → {repo_path}",
            tags=["fast_build", "clone"],
        )
        # Create only the parent; git clone creates the leaf itself.
        # Pre-creating the leaf makes git refuse it as "already exists and is
        # not an empty directory" on Windows (issue #107).
        _clone_repo(repo_url, repo_path)
    elif repo_url and os.path.exists(git_dir):
        # A caller-supplied checkout may deliberately be a fork, have local-only
        # branches, or contain untracked work. Repository preparation must not
        # mutate it; the build pipeline receives it exactly as supplied.
        if not path_was_derived:
            return

        origin = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            check=False,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        same_remote = origin.returncode == 0 and _normalized_remote(
            origin.stdout.strip()
        ) == _normalized_remote(repo_url)
        if not same_remote:
            # Derived paths are keyed on the repo name alone, so two repos with
            # the same name (different orgs) land here. Never build on the other
            # repo's clone — it would edit and open a PR against the wrong one.
            app.note(
                f"Workspace at {repo_path} is a clone of a different remote — "
                f"re-cloning",
                tags=["fast_build", "clone", "reclone"],
            )
            shutil.rmtree(repo_path, ignore_errors=True)
            _clone_repo(repo_url, repo_path, reclone=True)
            return

        app.note(
            f"Repo already exists at {repo_path} — resetting to "
            f"origin/{default_branch}",
            tags=["fast_build", "clone", "reset"],
        )

        # Remove stale worktrees on disk before touching branches.
        worktrees_dir = os.path.join(repo_path, ".worktrees")
        if os.path.isdir(worktrees_dir):
            shutil.rmtree(worktrees_dir, ignore_errors=True)
        subprocess.run(
            ["git", "worktree", "prune"],
            check=False,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )

        # Fetch latest remote state. A fetch failure is noted but reset decides
        # whether the existing clone can still provide a valid baseline.
        fetch = subprocess.run(
            ["git", "fetch", "origin"],
            check=False,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if fetch.returncode != 0:
            app.note(
                f"git fetch failed: "
                f"{_redact_credentials(fetch.stderr.strip(), repo_url)}",
                tags=["fast_build", "clone", "error"],
            )

        subprocess.run(
            ["git", "checkout", "-f", default_branch],
            check=False,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        reset = subprocess.run(
            ["git", "reset", "--hard", f"origin/{default_branch}"],
            check=False,
            cwd=repo_path,
            capture_output=True,
            text=True,
        )
        if reset.returncode != 0:
            app.note(
                f"Reset to origin/{default_branch} failed — re-cloning",
                tags=["fast_build", "clone", "reclone"],
            )
            shutil.rmtree(repo_path, ignore_errors=True)
            _clone_repo(repo_url, repo_path, reclone=True)
    else:
        os.makedirs(repo_path, exist_ok=True)


@app.reasoner(
    tags=[TAG_ENTRYPOINT],
    description=(
        "Fast-mode build: one planning pass into a small task list, then code and "
        "verify with tight timeouts. Same goal/repo_path interface as "
        "swe-planner.build, but lighter and cheaper — suited to small features "
        "where full DAG planning is overkill."
    ),
)
async def build(
    goal: str,
    repo_path: str = "",
    repo_url: str = "",
    artifacts_dir: str = ".artifacts",
    additional_context: str = "",
    config: dict | None = None,
) -> dict:
    """Speed-optimized build: git_init → fast_plan → fast_execute → fast_verify → finalize → PR.

    Accepts the same interface as swe-planner's build().
    Per-task timeout: cfg.task_timeout_seconds (default 300s).
    Build timeout (plan+execute): cfg.build_timeout_seconds (default 600s).
    """
    cfg = FastBuildConfig(**(config or {}))

    # Allow repo_url from direct parameter (overrides config)
    effective_repo_url = repo_url or cfg.repo_url

    # Auto-derive repo_path from repo_url when not specified
    path_was_derived = not repo_path
    if effective_repo_url and not repo_path:
        repo_path = os.path.join(
            _workspace_root(), _repo_name_from_url(effective_repo_url)
        )
    if not repo_path:
        raise ValueError("Either repo_path or repo_url must be provided")

    await asyncio.to_thread(
        _prepare_repo,
        effective_repo_url,
        repo_path,
        cfg.github_pr_base or "main",
        path_was_derived,
    )

    resolved = fast_resolve_models(cfg)
    ai_provider = _runtime_to_provider(cfg.runtime)
    abs_artifacts_dir = os.path.join(os.path.abspath(repo_path), artifacts_dir)

    # ── 1. GIT INIT (1 attempt, non-fatal) ─────────────────────────────────
    app.note("Fast build: git init", tags=["fast_build", "git_init"])
    git_config = None
    try:
        raw_git = await app.call(
            f"{NODE_ID}.run_git_init",
            repo_path=repo_path,
            goal=goal,
            artifacts_dir=abs_artifacts_dir,
            model=resolved["git_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=ai_provider,
            build_id="",
        )
        git_init = _unwrap(raw_git, "run_git_init")
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
                f"Git init: mode={git_init['mode']}, "
                f"branch={git_init['integration_branch']}",
                tags=["fast_build", "git_init", "complete"],
            )
        else:
            app.note(
                f"Git init failed (non-fatal): {git_init.get('error_message', 'unknown')}",
                tags=["fast_build", "git_init", "error"],
            )
    except Exception as e:
        app.note(
            f"Git init exception (non-fatal): {e}",
            tags=["fast_build", "git_init", "error"],
        )

    # ── 2. PLAN + EXECUTE (wrapped in build_timeout) ────────────────────────
    app.note(
        f"Fast build: plan + execute (timeout={cfg.build_timeout_seconds}s)",
        tags=["fast_build", "plan_execute"],
    )

    async def _plan_and_execute() -> tuple[dict, dict]:
        # 2a. PLAN
        raw_plan = await app.call(
            f"{NODE_ID}.fast_plan_tasks",
            goal=goal,
            repo_path=repo_path,
            max_tasks=cfg.max_tasks,
            pm_model=resolved["pm_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=ai_provider,
            additional_context=additional_context,
            artifacts_dir=abs_artifacts_dir,
        )
        plan_result = _unwrap(raw_plan, "fast_plan_tasks")
        tasks = plan_result.get("tasks", [])
        app.note(
            f"Plan complete: {len(tasks)} tasks",
            tags=["fast_build", "plan", "complete"],
        )

        # 2b. EXECUTE
        raw_exec = await app.call(
            f"{NODE_ID}.fast_execute_tasks",
            tasks=tasks,
            repo_path=repo_path,
            coder_model=resolved["coder_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=ai_provider,
            task_timeout_seconds=cfg.task_timeout_seconds,
            artifacts_dir=abs_artifacts_dir,
            agent_max_turns=cfg.agent_max_turns,
        )
        execution_result = _unwrap(raw_exec, "fast_execute_tasks")
        return plan_result, execution_result

    try:
        plan_result, execution_result = await asyncio.wait_for(
            _plan_and_execute(),
            timeout=cfg.build_timeout_seconds,
        )
    except asyncio.TimeoutError:
        app.note(
            f"Build timed out after {cfg.build_timeout_seconds}s",
            tags=["fast_build", "timeout"],
        )
        return FastBuildResult(
            plan_result={},
            execution_result={
                "timed_out": True,
                "task_results": [],
                "completed_count": 0,
                "failed_count": 0,
            },
            success=False,
            summary=f"Build timed out after {cfg.build_timeout_seconds}s",
        ).model_dump()

    # ── 3. VERIFY (one pass, no fix cycles) ─────────────────────────────────
    app.note("Fast build: verify", tags=["fast_build", "verify"])
    # Use a minimal PRD dict if the planner didn't produce one (fast path has no PM)
    prd_dict = plan_result.get("prd") or {
        "validated_description": goal,
        "acceptance_criteria": [],
        "must_have": [],
        "nice_to_have": [],
        "out_of_scope": [],
    }
    verification: dict = {}
    try:
        raw_verify = await app.call(
            f"{NODE_ID}.fast_verify",
            prd=prd_dict,
            repo_path=repo_path,
            task_results=execution_result.get("task_results", []),
            verifier_model=resolved["verifier_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=ai_provider,
            artifacts_dir=abs_artifacts_dir,
        )
        verification = _unwrap(raw_verify, "fast_verify")
    except Exception as e:
        app.note(
            f"Verify failed (non-fatal): {e}",
            tags=["fast_build", "verify", "error"],
        )
        verification = {"passed": False, "summary": f"Verification failed: {e}"}
    success = verification.get("passed", False)

    # ── 4. REPO FINALIZE ────────────────────────────────────────────────────
    app.note("Fast build: finalize", tags=["fast_build", "finalize"])
    try:
        raw_fin = await app.call(
            f"{NODE_ID}.run_repo_finalize",
            repo_path=repo_path,
            artifacts_dir=abs_artifacts_dir,
            model=resolved["git_model"],
            permission_mode=cfg.permission_mode,
            ai_provider=ai_provider,
        )
        _unwrap(raw_fin, "run_repo_finalize")
    except Exception as e:
        app.note(
            f"Finalize failed (non-fatal): {e}",
            tags=["fast_build", "finalize", "error"],
        )

    # ── 5. GITHUB PR (if enabled and remote present) ─────────────────────────
    pr_url = ""
    remote_url = git_config.get("remote_url", "") if git_config else ""
    if remote_url and cfg.enable_github_pr:
        app.note("Fast build: draft PR", tags=["fast_build", "github_pr"])
        base_branch = (
            cfg.github_pr_base
            or (git_config.get("remote_default_branch") if git_config else "")
            or "main"
        )
        completed_count = execution_result.get("completed_count", 0)
        total_count = len(execution_result.get("task_results", []))
        build_summary = (
            f"{'Success' if success else 'Partial'}: "
            f"{completed_count}/{total_count} tasks completed"
            f", verification: {verification.get('summary', '')}"
        )
        try:
            raw_pr = await app.call(
                f"{NODE_ID}.run_github_pr",
                repo_path=repo_path,
                integration_branch=git_config["integration_branch"],
                base_branch=base_branch,
                goal=goal,
                build_summary=build_summary,
                completed_issues=[
                    {
                        "issue_name": r.get("task_name", ""),
                        "result_summary": r.get("summary", ""),
                    }
                    for r in execution_result.get("task_results", [])
                    if r.get("outcome") == "completed"
                ],
                accumulated_debt=[],
                artifacts_dir=abs_artifacts_dir,
                model=resolved["git_model"],
                permission_mode=cfg.permission_mode,
                ai_provider=ai_provider,
            )
            pr_result = _unwrap(raw_pr, "run_github_pr")
            pr_url = pr_result.get("pr_url", "")
            if pr_url:
                app.note(
                    f"Draft PR: {pr_url}",
                    tags=["fast_build", "github_pr", "complete"],
                )
        except Exception as e:
            app.note(
                f"PR creation failed (non-fatal): {e}",
                tags=["fast_build", "github_pr", "error"],
            )

    completed_count = execution_result.get("completed_count", 0)
    total_count = len(execution_result.get("task_results", []))
    return FastBuildResult(
        plan_result=plan_result,
        execution_result=execution_result,
        verification=verification,
        success=success,
        summary=(
            f"{'Success' if success else 'Partial'}: "
            f"{completed_count}/{total_count} tasks completed"
            + (f", verification: {verification.get('summary', '')}" if verification else "")
        ),
        pr_url=pr_url,
    ).model_dump()


def main() -> None:
    """Entry point for ``python -m swe_af.fast`` and the ``swe-fast`` console script."""
    app.run(port=int(os.getenv("PORT", "8004")), host="0.0.0.0")


if __name__ == "__main__":
    main()
