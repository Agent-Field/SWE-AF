"""Per-issue coding loop: coder → reviewer (or QA/reviewer/synthesizer).

This is the INNER loop in the three-nested-loop architecture:
  - INNER (this): coder → review → approve/fix/block
  - MIDDLE: issue advisor diagnoses failures → adapt ACs/approach/scope
  - OUTER: replanner restructures DAG after unrecoverable failures

Two execution paths:
  - DEFAULT (most issues): coder → reviewer (2 LLM calls)
  - FLAGGED (complex/risky): coder → QA + reviewer → synthesizer (4 LLM calls)

The sprint planner sets `guidance.needs_deeper_qa = true` to select the flagged path.
"""

from __future__ import annotations

import asyncio
import json
import os
import traceback
import uuid
from typing import Callable


from swe_af.execution.fatal_error import FatalHarnessError
from swe_af.execution.schemas import (
    DAGState,
    ExecutionConfig,
    IssueOutcome,
    IssueResult,
)


async def _call_with_timeout(coro, timeout: int = 2700, label: str = ""):
    """Wrap a coroutine with asyncio.wait_for timeout."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError as exc:
        raise TimeoutError(f"Agent call '{label}' timed out after {timeout}s") from exc


# ---------------------------------------------------------------------------
# Iteration-level checkpoint helpers
# ---------------------------------------------------------------------------


def _iteration_state_key(issue_name: str, advisor_round: int = 0) -> str:
    """Checkpoint key for one coding-loop window.

    Each advisor round runs its own coding loop, so its iteration counter must
    start fresh. Keying the checkpoint per round keeps crash-resume inside a
    round working while stopping a RETRY from resuming an exhausted window
    (which would run zero coder iterations and still report a completion).
    Round 0 keeps the bare issue name so pre-existing checkpoints still resume.
    """
    return f"{issue_name}.r{advisor_round}" if advisor_round else issue_name


def _iteration_state_path(artifacts_dir: str, state_key: str, build_id: str = "") -> str:
    if not artifacts_dir:
        return ""
    if build_id:
        # Scope iteration checkpoints by build_id so parallel/sequential builds
        # against the same repo do not resume stale state from prior runs.
        return os.path.join(
            artifacts_dir, "execution", "iterations", build_id, f"{state_key}.json",
        )
    return os.path.join(artifacts_dir, "execution", "iterations", f"{state_key}.json")


def _save_iteration_state(artifacts_dir: str, state_key: str, state: dict, build_id: str = "") -> None:
    path = _iteration_state_path(artifacts_dir, state_key, build_id=build_id)
    if not path:
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        json.dump(state, f, indent=2, default=str)


def _load_iteration_state(artifacts_dir: str, state_key: str, build_id: str = "") -> dict | None:
    path = _iteration_state_path(artifacts_dir, state_key, build_id=build_id)
    if not path or not os.path.exists(path):
        return None
    with open(path, "r") as f:
        return json.load(f)


def _save_artifact(artifacts_dir: str, iteration_id: str, name: str, data: dict) -> str:
    """Save a structured result as a JSON artifact. Returns the file path."""
    if not artifacts_dir:
        return ""
    artifact_dir = os.path.join(artifacts_dir, "coding-loop", iteration_id)
    os.makedirs(artifact_dir, exist_ok=True)
    path = os.path.join(artifact_dir, f"{name}.json")
    with open(path, "w") as f:
        json.dump(data, f, indent=2, default=str)
    return path


# ---------------------------------------------------------------------------
# Memory helpers
# ---------------------------------------------------------------------------


async def _memory_get(memory_fn: Callable | None, key: str) -> any:
    """Read from shared memory, or return None if memory not available."""
    if memory_fn is None:
        return None
    try:
        return await memory_fn("get", key)
    except Exception:
        return None


async def _memory_set(memory_fn: Callable | None, key: str, value: any) -> None:
    """Write to shared memory, silently skip if memory not available."""
    if memory_fn is None:
        return
    try:
        await memory_fn("set", key, value)
    except Exception:
        pass


async def _read_memory_context(memory_fn: Callable | None, issue: dict) -> dict:
    """Read relevant shared memory for injection into agent prompts."""
    if memory_fn is None:
        return {}

    context = {}

    conventions = await _memory_get(memory_fn, "codebase_conventions")
    if conventions:
        context["codebase_conventions"] = conventions

    failure_patterns = await _memory_get(memory_fn, "failure_patterns")
    if failure_patterns:
        context["failure_patterns"] = failure_patterns

    bug_patterns = await _memory_get(memory_fn, "bug_patterns")
    if bug_patterns:
        context["bug_patterns"] = bug_patterns

    # Read interfaces from completed dependencies
    dep_interfaces = []
    for dep_name in issue.get("depends_on", []):
        iface = await _memory_get(memory_fn, f"interfaces/{dep_name}")
        if iface:
            dep_interfaces.append({**iface, "issue": dep_name})
    if dep_interfaces:
        context["dependency_interfaces"] = dep_interfaces

    return context


async def _write_memory_on_approve(
    memory_fn: Callable | None,
    issue: dict,
    coder_result: dict,
    is_first_success: bool,
    note_fn: Callable | None = None,
) -> None:
    """Write shared memory after a successful issue completion."""
    if memory_fn is None:
        return

    issue_name = issue.get("name", "unknown")

    # 3A: Codebase conventions — written by the first successful coder
    if is_first_success:
        learnings = coder_result.get("codebase_learnings", [])
        if learnings:
            conventions = {}
            for learning in learnings:
                conventions[f"note_{len(conventions)}"] = learning
            await _memory_set(memory_fn, "codebase_conventions", conventions)
            if note_fn:
                note_fn(
                    f"Memory: wrote codebase_conventions from {issue_name}",
                    tags=["memory", "conventions"],
                )

    # 3C: Interface registry
    iface = {
        "module": issue_name,
        "exports": issue.get("provides", []),
        "files_created": [
            f for f in coder_result.get("files_changed", [])
        ],
        "tests_passing": coder_result.get("tests_passed", None),
        "summary": coder_result.get("summary", ""),
    }
    await _memory_set(memory_fn, f"interfaces/{issue_name}", iface)

    # 3E: Agent retro
    retro = coder_result.get("agent_retro", {})
    if retro:
        await _memory_set(memory_fn, f"retros/{issue_name}", retro)

    # 3F: Build health — accumulate
    health = await _memory_get(memory_fn, "build_health") or {
        "modules_passing": [],
        "modules_failing": [],
        "total_tests_reported": 0,
        "known_risks": [],
        "issues_completed": 0,
        "issues_failed": 0,
        "debt_items": [],
    }
    health["issues_completed"] = health.get("issues_completed", 0) + 1
    if issue_name not in health.get("modules_passing", []):
        health.setdefault("modules_passing", []).append(issue_name)
    await _memory_set(memory_fn, "build_health", health)


async def _write_memory_on_failure(
    memory_fn: Callable | None,
    issue: dict,
    feedback_summary: str,
    review_result: dict | None = None,
    note_fn: Callable | None = None,
) -> None:
    """Write shared memory after a failed iteration."""
    if memory_fn is None:
        return

    issue_name = issue.get("name", "unknown")

    # 3B: Failure pattern feed-forward
    patterns = await _memory_get(memory_fn, "failure_patterns") or []
    patterns.append({
        "issue": issue_name,
        "pattern": "iteration_failure",
        "description": feedback_summary[:200],
    })
    await _memory_set(memory_fn, "failure_patterns", patterns[-10:])  # keep last 10

    # 3D: Bug patterns — extract from reviewer debt items
    if review_result:
        debt_items = review_result.get("debt_items", [])
        if debt_items:
            bug_patterns = await _memory_get(memory_fn, "bug_patterns") or []
            for d in debt_items:
                bug_type = d.get("title", d.get("type", "unknown"))
                # Check if pattern already exists
                existing = next((bp for bp in bug_patterns if bp.get("type") == bug_type), None)
                if existing:
                    existing["frequency"] = existing.get("frequency", 1) + 1
                else:
                    bug_patterns.append({
                        "type": bug_type,
                        "frequency": 1,
                        "modules": [issue_name],
                    })
            await _memory_set(memory_fn, "bug_patterns", bug_patterns[-20:])

    # 3F: Build health — track failure
    health = await _memory_get(memory_fn, "build_health") or {
        "modules_passing": [],
        "modules_failing": [],
        "total_tests_reported": 0,
        "known_risks": [],
        "issues_completed": 0,
        "issues_failed": 0,
        "debt_items": [],
    }
    health["issues_failed"] = health.get("issues_failed", 0) + 1
    if issue_name not in health.get("modules_failing", []):
        health.setdefault("modules_failing", []).append(issue_name)
    await _memory_set(memory_fn, "build_health", health)


# ---------------------------------------------------------------------------
# Stuck-loop detection
# ---------------------------------------------------------------------------


def _review_findings_key(review_result: dict | None) -> str:
    """Stable fingerprint of a review's blocking findings.

    Stuck detection needs to know whether consecutive fix iterations are still
    fighting the *same* finding.  The reviewer itemises findings in
    ``debt_items``; when it itemises nothing, the summary is the only signal.
    """
    if not review_result:
        return ""
    titles = sorted({
        str(finding.get("title", "")).strip().lower()
        for finding in review_result.get("debt_items") or []
        if isinstance(finding, dict)
        and finding.get("severity") == "blocking"
        and str(finding.get("title", "")).strip()
    })
    if titles:
        return " | ".join(titles)
    return " ".join(str(review_result.get("summary", "")).split()).lower()


def _debt_items_from_review(
    review_result: dict | None,
    issue_name: str,
    iteration: int,
    reason: str,
) -> list[dict]:
    """Build debt items from the last review's blocking and should-fix findings.

    ``COMPLETED_WITH_DEBT`` must always record at least one item, otherwise the
    debt register silently drops reviewer-confirmed findings (the production
    failure that produced ``debt_items: []`` for a blocked issue).  Itemised
    findings are carried as-is; when the reviewer itemised nothing, the review
    summary becomes the item.
    """
    items: list[dict] = []
    for finding in (review_result or {}).get("debt_items") or []:
        if not isinstance(finding, dict):
            continue
        severity = str(finding.get("severity", "")).lower()
        if severity not in ("blocking", "should_fix"):
            continue
        items.append({
            "type": "review_finding",
            "issue_name": issue_name,
            "severity": severity,
            "title": finding.get("title", ""),
            "description": finding.get("description", ""),
            "file_path": finding.get("file_path", ""),
            "iteration": iteration,
            "reason": reason,
        })
    if items:
        return items

    review = review_result or {}
    return [{
        "type": "unresolved_review_feedback",
        "issue_name": issue_name,
        "severity": "blocking" if review.get("blocking") else "should_fix",
        "title": "Reviewer feedback not resolved",
        "description": str(review.get("summary", "")).strip(),
        "file_path": "",
        "iteration": iteration,
        "reason": reason,
    }]


def _detect_stuck_loop(iteration_history: list[dict], window: int = 3) -> bool:
    """Return True if the last ``window`` iterations made no review progress.

    Two failure modes count as stuck:

    - consecutive non-blocking "fix" cycles: the reviewer keeps asking for
      polish without converging (the original default-path failure mode), and
    - consecutive blocking "fix" cycles on the *same* findings: the coder has
      had three passes at one blocking finding and did not fix it, so another
      pass is not going to help and the loop should escalate.
    """
    if len(iteration_history) < window:
        return False
    recent = iteration_history[-window:]
    if not all(entry.get("action") == "fix" for entry in recent):
        return False
    blocking = [bool(entry.get("review_blocking", False)) for entry in recent]
    if not any(blocking):
        return True
    if not all(blocking):
        return False
    findings = {entry.get("review_findings", "") for entry in recent}
    return len(findings) == 1 and "" not in findings


# ---------------------------------------------------------------------------
# Path routing helpers
# ---------------------------------------------------------------------------


async def _run_default_path(
    call_fn: Callable,
    node_id: str,
    worktree_path: str,
    coder_result: dict,
    issue: dict,
    iteration_id: str,
    project_context: dict,
    memory_context: dict,
    config: ExecutionConfig,
    timeout: int,
    issue_name: str,
    note_fn: Callable | None = None,
    workspace_manifest: dict | None = None,
    target_repo: str = "",
) -> tuple[str, str, dict | None]:
    """Default path: reviewer only (2 LLM calls total including coder).

    Returns (action, summary, review_result).
    """
    permission_mode = config.permission_mode

    try:
        review_result = await _call_with_timeout(
            call_fn(
                f"{node_id}.run_code_reviewer",
                worktree_path=worktree_path,
                coder_result=coder_result,
                issue=issue,
                iteration_id=iteration_id,
                project_context=project_context,
                qa_ran=False,
                memory_context=memory_context,
                model=config.code_reviewer_model,
                permission_mode=permission_mode,
                ai_provider=config.ai_provider,
                workspace_manifest=workspace_manifest,
                target_repo=target_repo,
            ),
            timeout=timeout,
            label=f"review:{issue_name}:default",
        )
    except FatalHarnessError:
        raise
    except Exception as e:
        if note_fn:
            note_fn(
                f"Reviewer failed: {issue_name}: {e}",
                tags=["coding_loop", "review_error", issue_name],
            )
        review_result = {"approved": True, "blocking": False, "summary": f"Review unavailable: {e}"}

    if note_fn:
        note_fn(
            f"Reviewer: approved={review_result.get('approved')}, "
            f"blocking={review_result.get('blocking')}",
            tags=["coding_loop", "feedback", issue_name],
        )

    # Reviewer is sole gatekeeper on default path.  "blocking" means "must be
    # fixed before merge" (the reviewer's own severity definition), not "stop
    # now": it becomes a fix iteration, exactly like the flagged path's
    # synthesizer FIX.  Terminal outcomes stay with loops that cannot converge
    # (three passes on the same blocking finding, or exhausted iterations),
    # which is where escalation to the issue advisor belongs.
    approved = review_result.get("approved", False)
    blocking = review_result.get("blocking", False)
    summary = review_result.get("summary", "")

    if approved and not blocking:
        action = "approve"
    else:
        action = "fix"

    return action, summary, review_result


async def _run_flagged_path(
    call_fn: Callable,
    node_id: str,
    worktree_path: str,
    coder_result: dict,
    issue: dict,
    iteration: int,
    iteration_id: str,
    iteration_history: list[dict],
    project_context: dict,
    memory_context: dict,
    config: ExecutionConfig,
    timeout: int,
    issue_name: str,
    note_fn: Callable | None = None,
    workspace_manifest: dict | None = None,
    target_repo: str = "",
) -> tuple[str, str, dict | None, dict | None, dict | None]:
    """Flagged path: QA + reviewer parallel → synthesizer (4 LLM calls).

    Returns (action, summary, review_result, qa_result, synthesis_result).
    """
    permission_mode = config.permission_mode

    # QA + reviewer in parallel
    try:
        qa_coro = _call_with_timeout(
            call_fn(
                f"{node_id}.run_qa",
                worktree_path=worktree_path,
                coder_result=coder_result,
                issue=issue,
                iteration_id=iteration_id,
                project_context=project_context,
                model=config.qa_model,
                permission_mode=permission_mode,
                ai_provider=config.ai_provider,
                workspace_manifest=workspace_manifest,
                target_repo=target_repo,
            ),
            timeout=timeout,
            label=f"qa:{issue_name}:iter{iteration}",
        )

        review_coro = _call_with_timeout(
            call_fn(
                f"{node_id}.run_code_reviewer",
                worktree_path=worktree_path,
                coder_result=coder_result,
                issue=issue,
                iteration_id=iteration_id,
                project_context=project_context,
                qa_ran=True,
                memory_context=memory_context,
                model=config.code_reviewer_model,
                permission_mode=permission_mode,
                ai_provider=config.ai_provider,
                workspace_manifest=workspace_manifest,
                target_repo=target_repo,
            ),
            timeout=timeout,
            label=f"review:{issue_name}:iter{iteration}",
        )

        qa_result, review_result = await asyncio.gather(
            qa_coro, review_coro, return_exceptions=True,
        )

        if isinstance(qa_result, Exception):
            if note_fn:
                note_fn(
                    f"QA agent failed: {issue_name}: {qa_result}",
                    tags=["coding_loop", "qa_error", issue_name],
                )
            qa_result = {"passed": False, "summary": f"QA agent failed: {qa_result}"}
        if isinstance(review_result, Exception):
            if note_fn:
                note_fn(
                    f"Review agent failed: {issue_name}: {review_result}",
                    tags=["coding_loop", "review_error", issue_name],
                )
            review_result = {"approved": True, "blocking": False, "summary": f"Review unavailable: {review_result}"}
    except FatalHarnessError:
        raise
    except Exception as e:
        if note_fn:
            note_fn(
                f"QA+Review both failed: {issue_name}: {e}",
                tags=["coding_loop", "qa_review_error", issue_name],
            )
        qa_result = {"passed": False, "summary": f"QA unavailable: {e}"}
        review_result = {"approved": True, "blocking": False, "summary": "Review unavailable"}

    if note_fn:
        note_fn(
            f"QA: passed={qa_result.get('passed')}, "
            f"Review: approved={review_result.get('approved')}, "
            f"blocking={review_result.get('blocking')}",
            tags=["coding_loop", "feedback", issue_name],
        )

    # Synthesizer
    try:
        synthesis_result = await _call_with_timeout(
            call_fn(
                f"{node_id}.run_qa_synthesizer",
                qa_result=qa_result,
                review_result=review_result,
                iteration_history=iteration_history,
                iteration_id=iteration_id,
                worktree_path=worktree_path,
                issue_summary={
                    "name": issue.get("name", ""),
                    "title": issue.get("title", ""),
                    "acceptance_criteria": issue.get("acceptance_criteria", []),
                },
                artifacts_dir=project_context.get("artifacts_dir", ""),
                model=config.qa_synthesizer_model,
                permission_mode=permission_mode,
                ai_provider=config.ai_provider,
                workspace_manifest=workspace_manifest,
                target_repo=target_repo,
            ),
            timeout=timeout,
            label=f"synthesizer:{issue_name}:iter{iteration}",
        )
    except FatalHarnessError:
        raise
    except Exception as e:
        if note_fn:
            note_fn(
                f"Synthesizer failed: {issue_name}: {e} — using fallback",
                tags=["coding_loop", "synthesizer_error", issue_name],
            )
        qa_passed = qa_result.get("passed", False)
        review_approved = review_result.get("approved", False)
        review_blocking = review_result.get("blocking", False)
        if qa_passed and review_approved and not review_blocking:
            synthesis_result = {"action": "approve", "summary": "Auto-approved (synthesizer unavailable)"}
        elif review_blocking:
            synthesis_result = {"action": "block", "summary": f"Blocked by review (synthesizer unavailable): {review_result.get('summary', '')}"}
        else:
            synthesis_result = {"action": "fix", "summary": f"Auto-fix (synthesizer unavailable): QA={qa_result.get('summary','')}, Review={review_result.get('summary','')}"}

    action = synthesis_result.get("action", "fix")
    summary = synthesis_result.get("summary", "")

    return action, summary, review_result, qa_result, synthesis_result


# ---------------------------------------------------------------------------
# Main coding loop
# ---------------------------------------------------------------------------


async def run_coding_loop(
    issue: dict,
    dag_state: DAGState,
    call_fn: Callable,
    node_id: str,
    config: ExecutionConfig,
    note_fn: Callable | None = None,
    memory_fn: Callable | None = None,
    advisor_round: int = 0,
) -> IssueResult:
    """Run the coding loop for a single issue.

    Execution path is determined by the sprint planner's guidance:
      - Default (guidance.needs_deeper_qa == false or no guidance):
        coder → reviewer (2 LLM calls). Reviewer is sole gatekeeper.
      - Flagged (guidance.needs_deeper_qa == true):
        coder → QA + reviewer → synthesizer (4 LLM calls).

    ``advisor_round`` is the issue advisor's round index (0 for the first
    attempt). Each round gets a full iteration window and its own checkpoint
    key, so an advisor RETRY runs coder iterations instead of resuming an
    exhausted counter.

    Each iteration:
      1. Read shared memory context
      2. Coder writes code, runs tests, commits
      3. Path branch: default (reviewer only) or flagged (QA+reviewer+synthesizer)
      4. Write to shared memory: conventions, failure patterns, bug patterns
      5. Branch on action: approve/fix/block

    Returns an IssueResult with the final outcome, including iteration_history.
    """
    issue_name = issue.get("name", "unknown")
    worktree_path = issue.get("worktree_path", dag_state.repo_path)
    branch_name = issue.get("branch_name", "")
    max_iterations = config.max_coding_iterations
    timeout = config.agent_timeout_seconds
    permission_mode = config.permission_mode
    # Each advisor round is a separate window: its own iteration budget and its
    # own checkpoint key, so a RETRY actually runs coder iterations.
    state_key = _iteration_state_key(issue_name, advisor_round)

    # Multi-repo context (None for single-repo builds)
    target_repo = issue.get("target_repo", "")
    ws_manifest_dict = dag_state.workspace_manifest  # dict | None

    # Warn if multi-repo issue is missing worktree_path (falling back to primary repo)
    if ws_manifest_dict and not issue.get("worktree_path"):
        if note_fn:
            note_fn(
                f"WARNING: issue '{issue_name}' has no worktree_path in multi-repo mode. "
                f"Falling back to primary repo: {dag_state.repo_path}. "
                f"target_repo='{target_repo}'",
                tags=["coding_loop", "warning", "multi_repo_fallback"],
            )

    # Extract guidance — determines execution path
    guidance = issue.get("guidance") or {}
    needs_deeper_qa = guidance.get("needs_deeper_qa", False)

    # Slim project context — paths only, agents read files if needed
    project_context = {
        "prd_path": dag_state.prd_path,
        "architecture_path": dag_state.architecture_path,
        "artifacts_dir": dag_state.artifacts_dir,
        "issues_dir": dag_state.issues_dir,
        "repo_path": dag_state.repo_path,
    }

    if note_fn:
        path_label = "FLAGGED (QA+reviewer+synth)" if needs_deeper_qa else "DEFAULT (reviewer only)"
        note_fn(
            f"Coding loop starting: {issue_name} [{path_label}] (max {max_iterations} iterations)",
            tags=["coding_loop", "start", issue_name],
        )

    feedback = ""
    iteration_history: list[dict] = []
    files_changed: list[str] = []
    start_iteration = 1
    iterations_run = 0
    review_result: dict | None = None
    is_first_success = len(dag_state.completed_issues) == 0

    # Resume from iteration checkpoint if available
    existing_state = _load_iteration_state(dag_state.artifacts_dir, state_key, build_id=dag_state.build_id)
    if existing_state:
        start_iteration = existing_state.get("iteration", 0) + 1
        feedback = existing_state.get("feedback", "")
        files_changed = existing_state.get("files_changed", [])
        iteration_history = existing_state.get("iteration_history", [])
        if note_fn:
            note_fn(
                f"Resuming {issue_name} from iteration {start_iteration}",
                tags=["coding_loop", "resume", issue_name],
            )

    for iteration in range(start_iteration, max_iterations + 1):
        iterations_run += 1
        iteration_id = str(uuid.uuid4())[:8]

        if note_fn:
            note_fn(
                f"Coding loop iteration {iteration}/{max_iterations}: {issue_name}",
                tags=["coding_loop", "iteration", issue_name],
            )

        # --- Read shared memory context ---
        memory_context = await _read_memory_context(memory_fn, issue)

        # --- 1. CODER ---
        try:
            coder_result = await _call_with_timeout(
                call_fn(
                    f"{node_id}.run_coder",
                    issue=issue,
                    worktree_path=worktree_path,
                    feedback=feedback,
                    iteration=iteration,
                    iteration_id=iteration_id,
                    project_context=project_context,
                    memory_context=memory_context,
                    model=config.coder_model,
                    permission_mode=permission_mode,
                    ai_provider=config.ai_provider,
                    workspace_manifest=ws_manifest_dict,
                    target_repo=target_repo,
                ),
                timeout=timeout,
                label=f"coder:{issue_name}:iter{iteration}",
            )
        except FatalHarnessError:
            raise
        except Exception as e:
            if note_fn:
                note_fn(
                    f"Coder agent failed: {issue_name} iter {iteration}: {e}",
                    tags=["coding_loop", "coder_error", issue_name],
                )
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                error_message=f"Coder agent failed on iteration {iteration}: {e}",
                error_context=traceback.format_exc(),
                files_changed=files_changed,
                branch_name=branch_name,
                attempts=iteration,
                iteration_history=iteration_history,
            )

        # Track files changed across iterations
        for f in coder_result.get("files_changed", []):
            if f not in files_changed:
                files_changed.append(f)

        _save_artifact(dag_state.artifacts_dir, iteration_id, "coder", coder_result)

        # --- 2. PATH BRANCH ---
        if needs_deeper_qa:
            # FLAGGED PATH: QA + reviewer parallel → synthesizer
            action, summary, review_result, qa_result, synthesis_result = await _run_flagged_path(
                call_fn=call_fn,
                node_id=node_id,
                worktree_path=worktree_path,
                coder_result=coder_result,
                issue=issue,
                iteration=iteration,
                iteration_id=iteration_id,
                iteration_history=iteration_history,
                project_context=project_context,
                memory_context=memory_context,
                config=config,
                timeout=timeout,
                issue_name=issue_name,
                note_fn=note_fn,
                workspace_manifest=ws_manifest_dict,
                target_repo=target_repo,
            )
            _save_artifact(dag_state.artifacts_dir, iteration_id, "qa", qa_result)
            _save_artifact(dag_state.artifacts_dir, iteration_id, "review", review_result)
            _save_artifact(dag_state.artifacts_dir, iteration_id, "synthesis", synthesis_result)

            # Stuck detection from synthesizer
            stuck = synthesis_result.get("stuck", False) if synthesis_result else False
        else:
            # DEFAULT PATH: reviewer only
            action, summary, review_result = await _run_default_path(
                call_fn=call_fn,
                node_id=node_id,
                worktree_path=worktree_path,
                coder_result=coder_result,
                issue=issue,
                iteration_id=iteration_id,
                project_context=project_context,
                memory_context=memory_context,
                config=config,
                timeout=timeout,
                issue_name=issue_name,
                note_fn=note_fn,
                workspace_manifest=ws_manifest_dict,
                target_repo=target_repo,
            )
            qa_result = None
            synthesis_result = None
            _save_artifact(dag_state.artifacts_dir, iteration_id, "review", review_result)

            stuck = False

        # Record iteration for history
        iteration_history.append({
            "iteration": iteration,
            "action": action,
            "summary": summary,
            "qa_passed": qa_result.get("passed", None) if qa_result else None,
            "review_approved": review_result.get("approved", False) if review_result else False,
            "review_blocking": review_result.get("blocking", False) if review_result else False,
            "review_findings": _review_findings_key(review_result),
            "path": "flagged" if needs_deeper_qa else "default",
        })

        if note_fn:
            note_fn(
                f"Decision: {action} — {summary[:100]}",
                tags=["coding_loop", "decision", issue_name],
            )

        # Save iteration-level checkpoint
        _save_iteration_state(dag_state.artifacts_dir, state_key, {
            "iteration": iteration,
            "feedback": summary,
            "files_changed": files_changed,
            "iteration_history": iteration_history,
        }, build_id=dag_state.build_id)

        # --- 3. WRITE TO MEMORY ---
        if action == "approve":
            await _write_memory_on_approve(
                memory_fn, issue, coder_result, is_first_success, note_fn,
            )
        elif action == "fix":
            await _write_memory_on_failure(
                memory_fn, issue, summary, review_result, note_fn,
            )

        # --- 4. BRANCH ON ACTION ---
        if action == "approve":
            if note_fn:
                note_fn(
                    f"Coding loop APPROVED: {issue_name} after {iteration} iteration(s)",
                    tags=["coding_loop", "complete", issue_name],
                )
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.COMPLETED,
                result_summary=summary,
                files_changed=files_changed,
                branch_name=branch_name,
                attempts=iteration,
                iteration_history=iteration_history,
                repo_name=coder_result.get("repo_name", ""),
            )

        if action == "block":
            if note_fn:
                note_fn(
                    f"Coding loop BLOCKED: {issue_name} — {summary}",
                    tags=["coding_loop", "blocked", issue_name],
                )
            await _write_memory_on_failure(
                memory_fn, issue, summary, review_result, note_fn,
            )
            return IssueResult(
                issue_name=issue_name,
                outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                error_message=summary,
                files_changed=files_changed,
                branch_name=branch_name,
                attempts=iteration,
                iteration_history=iteration_history,
            )

        # action == "fix" — build rich feedback for the coder.  Blocking
        # findings come first: they are what must change before merge.
        if action == "fix":
            feedback_parts: list[str] = []
            if review_result:
                debt = review_result.get("debt_items", [])
                blocking_debt = [
                    d for d in debt
                    if isinstance(d, dict) and d.get("severity") == "blocking"
                ]
                if blocking_debt:
                    feedback_parts.append("### Blocking Review Issues (fix before merge)")
                    for d in blocking_debt:
                        feedback_parts.append(
                            f"- [{d.get('severity')}] {d.get('title', '?')}: {d.get('description', '')}"
                        )
                    feedback_parts.append("")
            feedback_parts.append(summary)
            if qa_result:
                test_failures = qa_result.get("test_failures", [])
                if test_failures:
                    feedback_parts.append("\n### Specific Test Failures")
                    for f in test_failures:
                        feedback_parts.append(
                            f"- `{f.get('test_name', '?')}` in `{f.get('file', '?')}`: {f.get('error', '')}"
                        )
            feedback = "\n".join(feedback_parts)
        else:
            feedback = summary

        # Stuck detection — default path uses history-based detection since it
        # has no synthesizer to set the stuck flag.
        if not stuck and not needs_deeper_qa:
            stuck = _detect_stuck_loop(iteration_history)

        if stuck:
            last_blocking = bool(iteration_history[-1].get("review_blocking", False))
            if not last_blocking and files_changed:
                # Non-blocking stuck loop with code changes → accept with debt
                debt_items = _debt_items_from_review(
                    review_result, issue_name, iteration, reason="stuck_loop_non_blocking",
                )
                if note_fn:
                    note_fn(
                        f"Coding loop STUCK (non-blocking): {issue_name} — "
                        f"accepting with debt after {iteration} iterations "
                        f"({len(debt_items)} debt item(s))",
                        tags=["coding_loop", "stuck", "accept_debt", issue_name],
                    )
                return IssueResult(
                    issue_name=issue_name,
                    outcome=IssueOutcome.COMPLETED_WITH_DEBT,
                    result_summary=(
                        f"Accepted with debt (stuck loop, non-blocking): {summary} "
                        f"[{len(debt_items)} debt item(s)]"
                    ),
                    files_changed=files_changed,
                    branch_name=branch_name,
                    attempts=iteration,
                    iteration_history=iteration_history,
                    debt_items=debt_items,
                )
            else:
                if note_fn:
                    note_fn(
                        f"Coding loop STUCK: {issue_name} — breaking after {iteration} iterations",
                        tags=["coding_loop", "stuck", issue_name],
                    )
                await _write_memory_on_failure(
                    memory_fn, issue, summary, review_result, note_fn,
                )
                return IssueResult(
                    issue_name=issue_name,
                    outcome=IssueOutcome.FAILED_UNRECOVERABLE,
                    error_message=f"Stuck loop detected: {summary}",
                    files_changed=files_changed,
                    branch_name=branch_name,
                    attempts=iteration,
                    iteration_history=iteration_history,
                )

    # Loop exhausted without approval — check if we can accept with debt.
    # `review_result` holds the last review of this window (None if the loop body
    # never ran); `last_blocking` is derived from the recorded history so a
    # missing review can never be read as a non-blocking one.
    last_review = review_result
    last_entry = iteration_history[-1] if iteration_history else None
    last_blocking = bool(last_entry.get("review_blocking", False)) if last_entry else False
    last_iteration = int(last_entry.get("iteration", max_iterations)) if last_entry else 0

    if iterations_run == 0:
        # A window that never ran an iteration cannot be a completion: this is
        # the shape that reported a blocked issue as "accepted with debt" with
        # an empty debt list.  Fail it so the advisor sees a real failure.
        if note_fn:
            note_fn(
                f"Coding loop ran zero iterations: {issue_name}",
                tags=["coding_loop", "exhausted", "zero_iterations", issue_name],
            )
        return IssueResult(
            issue_name=issue_name,
            outcome=IssueOutcome.FAILED_UNRECOVERABLE,
            error_message=(
                f"Coding loop ran zero iterations for {issue_name}: the iteration "
                f"window was already exhausted when the loop started"
            ),
            files_changed=files_changed,
            branch_name=branch_name,
            attempts=0,
            iteration_history=iteration_history,
        )

    if not last_blocking and files_changed:
        # Reviewer was never blocking and coder produced changes — accept with debt
        # rather than failing entirely.  This prevents trivial tasks from stalling
        # the whole DAG when the reviewer keeps requesting minor polish.  The
        # review's blocking/should-fix findings become recorded debt.
        debt_items = _debt_items_from_review(
            last_review, issue_name, last_iteration, reason="exhausted_non_blocking",
        )
        if note_fn:
            note_fn(
                f"Coding loop exhausted (non-blocking): {issue_name} — "
                f"accepting with debt after {last_iteration} iterations "
                f"({len(debt_items)} debt item(s))",
                tags=["coding_loop", "exhausted", "accept_debt", issue_name],
            )
        return IssueResult(
            issue_name=issue_name,
            outcome=IssueOutcome.COMPLETED_WITH_DEBT,
            result_summary=(
                f"Accepted with debt after {last_iteration} iterations "
                f"(reviewer non-blocking, code changes present, "
                f"{len(debt_items)} debt item(s))"
            ),
            files_changed=files_changed,
            branch_name=branch_name,
            attempts=last_iteration,
            iteration_history=iteration_history,
            debt_items=debt_items,
        )

    # Truly unrecoverable — reviewer was blocking or no code was produced
    if note_fn:
        note_fn(
            f"Coding loop exhausted: {issue_name} after {last_iteration} iterations",
            tags=["coding_loop", "exhausted", issue_name],
        )

    await _write_memory_on_failure(
        memory_fn, issue, "Loop exhausted", last_review, note_fn,
    )

    return IssueResult(
        issue_name=issue_name,
        outcome=IssueOutcome.FAILED_UNRECOVERABLE,
        error_message=f"Coding loop exhausted after {last_iteration} iterations without approval",
        files_changed=files_changed,
        branch_name=branch_name,
        attempts=last_iteration,
        iteration_history=iteration_history,
    )
