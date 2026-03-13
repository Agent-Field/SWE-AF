from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter
from typing import Any

import httpx

from .dataset import SWEEvoInstance
from .results import BenchmarkResult, ResultStore

logger = logging.getLogger(__name__)

_TERMINAL_STATUSES = frozenset({"completed", "failed", "error", "cancelled", "timeout"})
_POLL_INTERVAL_SECONDS = 30.0
_POLL_BACKOFF_MAX = 120.0


class ControlPlaneError(Exception):
    pass


class BenchmarkRunner:
    def __init__(
        self,
        *,
        server: str = "http://localhost:8080",
        api_key: str | None = None,
        node_id: str = "swe-planner",
        results_dir: str = "./benchmark_results",
        config_overrides: dict[str, Any] | None = None,
        timeout_seconds: float = 3600,
        poll_interval: float = _POLL_INTERVAL_SECONDS,
    ) -> None:
        self.server = server.rstrip("/")
        self.node_id = node_id
        self.result_store = ResultStore(results_dir)
        self.timeout_seconds = timeout_seconds
        self.poll_interval = poll_interval
        self.config_overrides = config_overrides or {}

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

    async def _submit(self, instance: SWEEvoInstance) -> str:
        config: dict[str, Any] = {
            "enable_github_pr": False,
            "runtime": "open_code",
            "models": {"default": "openrouter/minimax/minimax-m2.5"},
            **self.config_overrides,
        }

        payload = {
            "input": {
                "goal": instance.problem_statement,
                "repo_url": f"https://github.com/{instance.repo}",
                "config": config,
            }
        }

        url = f"/api/v1/execute/async/{self.node_id}.build"
        resp = await self._client.post(url, json=payload)

        if resp.status_code not in {200, 202}:
            raise ControlPlaneError(f"Submit failed ({resp.status_code}): {resp.text}")

        data = resp.json()
        execution_id = data.get("execution_id")
        if not execution_id:
            raise ControlPlaneError(f"No execution_id in response: {data}")

        logger.info(
            "Submitted %s → execution_id=%s", instance.instance_id, execution_id
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

    async def _cancel(self, execution_id: str) -> None:
        try:
            await self._client.post(f"/api/v1/executions/{execution_id}/cancel")
        except Exception:
            pass

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

    async def run_instance(self, instance: SWEEvoInstance) -> BenchmarkResult:
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
        instances: list[SWEEvoInstance],
        concurrency: int = 3,
    ) -> list[BenchmarkResult]:
        if not instances:
            return []

        semaphore = asyncio.Semaphore(max(1, concurrency))
        results: list[BenchmarkResult] = []
        completed_count = 0
        total = len(instances)

        async def _run_one(inst: SWEEvoInstance) -> BenchmarkResult:
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
        instances: list[SWEEvoInstance],
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

            statuses = {}
            for r in batch_results:
                statuses[r.status] = statuses.get(r.status, 0) + 1
            logger.info("Batch %d summary: %s", batch_idx + 1, statuses)

        return all_results
