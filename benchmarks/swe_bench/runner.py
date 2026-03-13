from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import httpx

from benchmarks.swe_evo.evaluate import evaluate_instance
from benchmarks.swe_evo.results import BenchmarkResult, ResultStore

from .dataset import SWEBenchInstance

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "error", "cancelled", "timeout"})
_POLL_INTERVAL_SECONDS = 30.0
_POLL_BACKOFF_MAX = 120.0
_DEFAULT_MODEL = "openrouter/qwen/qwen3-coder-next"


class ControlPlaneError(Exception):
    pass


class BenchmarkRunner:
    def __init__(
        self,
        *,
        server: str = "http://localhost:8080",
        api_key: str | None = None,
        node_id: str = "swe-planner",
        results_dir: str = "./swe_bench_results",
        model: str = _DEFAULT_MODEL,
        config_overrides: dict[str, Any] | None = None,
        timeout_seconds: float = 3600,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
        repo_path: str | None = None,
    ) -> None:
        self.server = server.rstrip("/")
        self.node_id = node_id
        self.model = model
        self.result_store = ResultStore(results_dir)
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self.config_overrides = config_overrides or {}
        self.repo_path = repo_path

        headers: dict[str, str] = {"Content-Type": "application/json"}
        if api_key:
            headers["X-API-Key"] = api_key

        self._client = httpx.AsyncClient(
            base_url=self.server,
            headers=headers,
            timeout=httpx.Timeout(60.0, connect=15.0),
        )

    async def __aenter__(self) -> BenchmarkRunner:
        return self

    async def __aexit__(self, exc_type: object, exc: object, tb: object) -> None:
        await self.close()

    async def close(self) -> None:
        await self._client.aclose()

    async def _submit(self, instance: SWEBenchInstance) -> str:
        config: dict[str, Any] = {
            "enable_github_pr": False,
            "runtime": "open_code",
            "models": {"default": self.model},
            **self.config_overrides,
        }

        input_data: dict[str, Any] = {
            "goal": instance.problem_statement,
            "config": config,
        }

        if self.repo_path:
            # Use pre-staged local repo — no cloning, no reset
            input_data["repo_path"] = self.repo_path
        else:
            # Fallback: clone from GitHub (original behavior)
            input_data["repo_url"] = f"https://github.com/{instance.repo}"
            input_data["additional_context"] = (
                f"CRITICAL: Before starting work, checkout commit {instance.base_commit}. "
                f"Run: git checkout {instance.base_commit}. "
                "This ensures you are working on the exact version where this bug exists."
            )

        payload = {"input": input_data}

        url = f"/api/v1/execute/async/{self.node_id}.build"
        resp = await self._client.post(url, json=payload)

        if resp.status_code not in {200, 202}:
            raise ControlPlaneError(f"Submit failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        execution_id = data.get("execution_id")
        if not execution_id:
            raise ControlPlaneError(f"No execution_id in response: {data}")

        logger.info(
            "Submitted %s → execution_id=%s (model=%s)",
            instance.instance_id,
            execution_id,
            self.model,
        )
        return execution_id

    async def _poll_until_done(self, execution_id: str) -> dict[str, Any]:
        url = f"/api/v1/executions/{execution_id}"
        elapsed = 0.0
        interval = self.poll_interval

        while elapsed < self.timeout_seconds:
            await asyncio.sleep(interval)
            elapsed += interval

            try:
                resp = await self._client.get(url)
                if resp.status_code != 200:
                    logger.warning(
                        "Poll %s returned %d, retrying...",
                        execution_id,
                        resp.status_code,
                    )
                    interval = min(interval * 1.5, _POLL_BACKOFF_MAX)
                    continue

                data = resp.json()
                status = data.get("status", "")

                if status in _TERMINAL_STATUSES:
                    logger.info(
                        "Execution %s finished with status=%s", execution_id, status
                    )
                    return data

                if int(elapsed) % 120 == 0:
                    logger.info(
                        "Execution %s still %s (%.0fs elapsed)",
                        execution_id,
                        status,
                        elapsed,
                    )

                interval = self.poll_interval

            except httpx.HTTPError as exc:
                logger.warning("Poll error for %s: %s", execution_id, exc)
                interval = min(interval * 1.5, _POLL_BACKOFF_MAX)

        logger.warning("Execution %s timed out after %.0fs", execution_id, elapsed)
        try:
            await self._client.post(f"/api/v1/executions/{execution_id}/cancel")
        except Exception:
            pass
        return {"execution_id": execution_id, "status": "timeout"}

    @staticmethod
    def _extract_generated_patch(result: dict[str, Any] | None) -> str:
        if not result:
            return ""

        direct = result.get("generated_patch")
        if isinstance(direct, str):
            return direct

        for key in ("output", "result"):
            nested = result.get(key)
            if isinstance(nested, dict):
                patch = nested.get("generated_patch")
                if isinstance(patch, str):
                    return patch

        dag_state = result.get("dag_state")
        if not isinstance(dag_state, dict):
            for key in ("output", "result"):
                nested = result.get(key)
                if isinstance(nested, dict):
                    dag_state = nested.get("dag_state")
                    if isinstance(dag_state, dict):
                        break

        if isinstance(dag_state, dict):
            completed = dag_state.get("completed_issues")
            if isinstance(completed, list):
                summaries: list[str] = []
                for issue in completed:
                    if isinstance(issue, dict):
                        summary = issue.get("result_summary", "")
                        if isinstance(summary, str) and summary.strip():
                            summaries.append(summary.strip())
                if summaries:
                    return "\n\n".join(summaries)

        return ""

    async def run_instance(self, instance: SWEBenchInstance) -> BenchmarkResult:
        start_iso = datetime.now(tz=UTC).isoformat()
        timer_start = perf_counter()
        status = "completed"
        error_message = ""
        execution_id = ""
        build_result: dict[str, Any] | None = None

        try:
            execution_id = await self._submit(instance)
            build_result = await self._poll_until_done(execution_id)

            poll_status = build_result.get("status", "")
            if poll_status in {"failed", "error", "cancelled"}:
                status = "failed"
            elif poll_status == "timeout":
                status = "timeout"

        except ControlPlaneError as exc:
            status = "error"
            error_message = str(exc)
            logger.error("Instance %s submit error: %s", instance.instance_id, exc)
        except Exception as exc:
            status = "error"
            error_message = str(exc)
            logger.error("Instance %s unexpected error: %s", instance.instance_id, exc)

        duration = perf_counter() - timer_start
        end_iso = datetime.now(tz=UTC).isoformat()

        result = BenchmarkResult(
            instance_id=instance.instance_id,
            repo=instance.repo,
            status=status,
            execution_id=execution_id,
            start_time=start_iso,
            end_time=end_iso,
            duration_seconds=duration,
            build_result=build_result,
            error_message=error_message,
            generated_patch=self._extract_generated_patch(build_result),
            ground_truth_patch=instance.patch,
        )
        self.result_store.save(result)
        return result

    async def run_batch(
        self,
        instances: list[SWEBenchInstance],
        concurrency: int = 3,
    ) -> list[BenchmarkResult]:
        if not instances:
            return []

        semaphore = asyncio.Semaphore(max(1, concurrency))
        results: list[BenchmarkResult] = []
        completed_count = 0
        total = len(instances)

        async def _run_one(inst: SWEBenchInstance) -> BenchmarkResult:
            nonlocal completed_count
            async with semaphore:
                result = await self.run_instance(inst)
                completed_count += 1
                logger.info(
                    "Progress: %d/%d — %s → %s (%.1fs)",
                    completed_count,
                    total,
                    inst.instance_id,
                    result.status,
                    result.duration_seconds,
                )
                return result

        tasks = [asyncio.create_task(_run_one(inst)) for inst in instances]

        for coro in asyncio.as_completed(tasks):
            result = await coro
            results.append(result)

        return results

    async def run_all(
        self,
        instances: list[SWEBenchInstance],
        batch_size: int = 5,
        concurrency: int = 3,
    ) -> list[BenchmarkResult]:
        if not instances:
            return []

        batch_size = max(1, batch_size)
        all_results: list[BenchmarkResult] = []
        total_batches = (len(instances) + batch_size - 1) // batch_size

        for batch_idx in range(total_batches):
            start = batch_idx * batch_size
            end = start + batch_size
            batch = instances[start:end]

            logger.info(
                "=== Batch %d/%d (%d instances) ===",
                batch_idx + 1,
                total_batches,
                len(batch),
            )

            try:
                batch_results = await self.run_batch(batch, concurrency=concurrency)
                all_results.extend(batch_results)
            except KeyboardInterrupt:
                logger.warning("Interrupted. Completed results are persisted.")
                raise

            statuses: dict[str, int] = {}
            for r in batch_results:
                statuses[r.status] = statuses.get(r.status, 0) + 1
            logger.info("Batch %d summary: %s", batch_idx + 1, statuses)

        return all_results


async def compare_models(
    instances: list[SWEBenchInstance],
    models: list[str],
    *,
    server: str = "http://localhost:8080",
    api_key: str | None = None,
    node_id: str = "swe-planner",
    results_base_dir: str = "./swe_bench_results",
    config_overrides: dict[str, Any] | None = None,
    timeout_seconds: float = 3600,
    poll_interval: float = 30.0,
    staged_dir_container: str | None = None,
) -> dict[str, list[BenchmarkResult]]:
    """Run the same instances against multiple models in parallel, return per-model results."""

    async def _run_model(model: str) -> tuple[str, list[BenchmarkResult]]:
        model_slug = model.replace("/", "_")
        results_dir = f"{results_base_dir}/{model_slug}"

        repo_path: str | None = None
        if staged_dir_container:
            repo_path = f"{staged_dir_container}/django_{model_slug}"

        async with BenchmarkRunner(
            server=server,
            api_key=api_key,
            node_id=node_id,
            results_dir=results_dir,
            model=model,
            config_overrides=config_overrides,
            timeout_seconds=timeout_seconds,
            poll_interval=poll_interval,
            repo_path=repo_path,
        ) as runner:
            results = await runner.run_batch(instances, concurrency=len(instances))
        return model, results

    tasks = [asyncio.create_task(_run_model(m)) for m in models]
    model_results: dict[str, list[BenchmarkResult]] = {}

    for coro in asyncio.as_completed(tasks):
        model, results = await coro
        model_results[model] = results

    return model_results


def print_comparison_table(
    model_results: dict[str, list[BenchmarkResult]],
) -> None:
    """Print a comparison table across models."""
    print("\n" + "=" * 90)
    print("MODEL COMPARISON")
    print("=" * 90)

    header = f"{'Model':<45} {'Status':<12} {'Duration':<12} {'Files':<8} {'Added':<8} {'Removed':<8}"
    print(header)
    print("-" * 90)

    for model, results in sorted(model_results.items()):
        for result in results:
            eval_result = evaluate_instance(result)
            duration_str = f"{result.duration_seconds:.0f}s"
            print(
                f"{model:<45} {result.status:<12} {duration_str:<12} "
                f"{eval_result.files_overlap:<8.2f} {eval_result.lines_added_match:<8.2f} "
                f"{eval_result.lines_removed_match:<8.2f}"
            )

    print("=" * 90)
    print("Overlap scores: 1.0 = perfect match with ground truth, 0.0 = no overlap")
    print()
