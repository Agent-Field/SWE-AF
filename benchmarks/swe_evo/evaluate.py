from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable
from pydantic import BaseModel

from .results import BenchmarkResult


@dataclass(frozen=True)
class ParsedPatch:
    files: set[str]
    added: set[str]
    removed: set[str]


class EvalResult(BaseModel):
    instance_id: str
    repo: str
    status: str
    exact_match: bool
    files_overlap: float
    lines_added_match: float
    lines_removed_match: float


class RunEvaluation(BaseModel):
    total: int
    resolve_rate: float
    exact_match_rate: float
    avg_file_overlap: float
    avg_added_overlap: float
    avg_removed_overlap: float


def _jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 1.0
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _parse_patch(patch: str) -> ParsedPatch:
    current_file = ""
    files: set[str] = set()
    added: set[str] = set()
    removed: set[str] = set()

    for raw_line in patch.splitlines():
        line = raw_line.rstrip("\n")
        if line.startswith("+++ b/"):
            current_file = line[6:].strip()
            if current_file:
                files.add(current_file)
            continue
        if line.startswith("diff --git ") or line.startswith("@@"):
            continue
        if line.startswith("--- "):
            continue
        if line.startswith("+") and not line.startswith("+++"):
            normalized = " ".join(line[1:].split())
            added.add(f"{current_file}:{normalized}")
        elif line.startswith("-") and not line.startswith("---"):
            normalized = " ".join(line[1:].split())
            removed.add(f"{current_file}:{normalized}")

    return ParsedPatch(files=files, added=added, removed=removed)


def _normalized_signature(
    parsed: ParsedPatch,
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    return (
        tuple(sorted(parsed.files)),
        tuple(sorted(parsed.added)),
        tuple(sorted(parsed.removed)),
    )


def evaluate_instance(result: BenchmarkResult) -> EvalResult:
    generated = _parse_patch(result.generated_patch)
    ground_truth = _parse_patch(result.ground_truth_patch)

    exact_match = _normalized_signature(generated) == _normalized_signature(
        ground_truth
    )

    return EvalResult(
        instance_id=result.instance_id,
        repo=result.repo,
        status=result.status,
        exact_match=exact_match,
        files_overlap=_jaccard(generated.files, ground_truth.files),
        lines_added_match=_jaccard(generated.added, ground_truth.added),
        lines_removed_match=_jaccard(generated.removed, ground_truth.removed),
    )


def _mean(values: Iterable[float]) -> float:
    data = list(values)
    if not data:
        return 0.0
    return sum(data) / len(data)


def evaluate_run(results: list[BenchmarkResult]) -> RunEvaluation:
    evaluated = [evaluate_instance(result) for result in results]
    total = len(evaluated)

    resolved = sum(1 for item in evaluated if item.status == "completed")
    exact = sum(1 for item in evaluated if item.exact_match)

    divisor = total if total else 1
    return RunEvaluation(
        total=total,
        resolve_rate=resolved / divisor,
        exact_match_rate=exact / divisor,
        avg_file_overlap=_mean(item.files_overlap for item in evaluated),
        avg_added_overlap=_mean(item.lines_added_match for item in evaluated),
        avg_removed_overlap=_mean(item.lines_removed_match for item in evaluated),
    )
