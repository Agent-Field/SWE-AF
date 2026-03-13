from __future__ import annotations

import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

BenchmarkStatus = Literal["completed", "failed", "error", "timeout"]


class BenchmarkResult(BaseModel):
    instance_id: str
    repo: str
    status: BenchmarkStatus
    execution_id: str = ""
    start_time: str
    end_time: str
    duration_seconds: float
    build_result: dict | None = None
    error_message: str = ""
    generated_patch: str = ""
    ground_truth_patch: str = ""


class ResultStore:
    def __init__(self, results_dir: str) -> None:
        self.results_dir = Path(results_dir)
        self.results_dir.mkdir(parents=True, exist_ok=True)
        self.jsonl_path = self.results_dir / "results.jsonl"
        self.raw_dir = self.results_dir / "raw"
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    def save(self, result: BenchmarkResult) -> None:
        with self.jsonl_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(result.model_dump(), ensure_ascii=False) + "\n")

        raw_payload = {
            "instance_id": result.instance_id,
            "execution_id": result.execution_id,
            "status": result.status,
            "duration_seconds": result.duration_seconds,
            "build_result": result.build_result,
            "error_message": result.error_message,
            "generated_patch": result.generated_patch,
        }
        raw_path = self.raw_dir / f"{result.instance_id}.json"
        raw_path.write_text(
            json.dumps(raw_payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

    def load_all(self) -> list[BenchmarkResult]:
        if not self.jsonl_path.exists():
            return []

        results: list[BenchmarkResult] = []
        with self.jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                results.append(BenchmarkResult.model_validate_json(line))
        return results

    def load_latest_by_instance(self) -> dict[str, BenchmarkResult]:
        seen: dict[str, BenchmarkResult] = {}
        for result in self.load_all():
            seen[result.instance_id] = result
        return seen

    def get_completed_ids(self) -> set[str]:
        return {
            instance_id
            for instance_id, result in self.load_latest_by_instance().items()
            if result.status in {"completed", "failed", "error", "timeout"}
        }

    def summary(self) -> dict[str, int | float]:
        latest = self.load_latest_by_instance()
        counts: dict[str, int] = {
            "total": 0,
            "completed": 0,
            "failed": 0,
            "error": 0,
            "timeout": 0,
        }
        total_duration = 0.0
        for result in latest.values():
            counts["total"] += 1
            counts[result.status] = counts.get(result.status, 0) + 1
            total_duration += result.duration_seconds

        return {
            **counts,
            "avg_duration_seconds": (
                total_duration / counts["total"] if counts["total"] else 0.0
            ),
        }
