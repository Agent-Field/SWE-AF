"""swe_af.fast.verifier — FastBuild single-pass verification reasoner.

Registers ``fast_verify`` on the shared ``fast_router``.  The function
performs exactly one verification pass — there are no fix cycles.
"""

from __future__ import annotations

import logging
from typing import Any

from swe_af.fast import fast_router
from swe_af.fast.schemas import FastVerificationResult

logger = logging.getLogger(__name__)


@fast_router.reasoner()
async def fast_verify(
    prd: dict[str, Any],
    repo_path: str,
    task_results: list[dict[str, Any]],
    verifier_model: str = "sonnet",
    permission_mode: str = "",
    ai_provider: str = "claude",
    artifacts_dir: str = "",
) -> dict[str, Any]:
    """Run a single verification pass against the built repository.

    Adapts fast task_results into the completed/failed/skipped split that
    ``run_verifier`` expects, then delegates to a single verification pass.
    No fix cycles are attempted.
    """
    try:
        import swe_af.fast.app as _app  # noqa: PLC0415

        # Split task_results into completed/failed for run_verifier's interface
        completed_issues: list[dict] = []
        failed_issues: list[dict] = []
        for tr in task_results:
            entry = {
                "issue_name": tr.get("task_name", ""),
                "result_summary": tr.get("summary", ""),
            }
            if tr.get("outcome") == "completed":
                completed_issues.append(entry)
            else:
                failed_issues.append(entry)

        result: dict[str, Any] = await _app.app.call(
            f"{_app.NODE_ID}.run_verifier",
            prd=prd,
            repo_path=repo_path,
            artifacts_dir=artifacts_dir,
            completed_issues=completed_issues,
            failed_issues=failed_issues,
            skipped_issues=[],
            model=verifier_model,
            permission_mode=permission_mode,
            ai_provider=ai_provider,
        )
        verification = FastVerificationResult(
            passed=result.get("passed", False),
            summary=result.get("summary", ""),
            criteria_results=result.get("criteria_results", []),
            suggested_fixes=result.get("suggested_fixes", []),
        )
        return verification.model_dump()
    except Exception as exc:  # noqa: BLE001
        logger.exception("fast_verify: verification agent raised an exception")
        fallback = FastVerificationResult(
            passed=False,
            summary=f"Verification agent failed: {exc}",
        )
        return fallback.model_dump()


__all__ = ["fast_verify"]
