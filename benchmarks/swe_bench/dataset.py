from __future__ import annotations

import json
from collections import Counter
from collections.abc import Sequence

from datasets import load_dataset as hf_load_dataset
from pydantic import BaseModel, Field

SWE_BENCH_MINI_DATASET = "MariusHobbhahn/swe-bench-verified-mini"


class SWEBenchInstance(BaseModel):
    instance_id: str
    repo: str
    base_commit: str
    patch: str
    test_patch: str
    problem_statement: str
    hints_text: str = ""
    created_at: str = ""
    version: str = ""
    FAIL_TO_PASS: list[str] = Field(default_factory=list)
    PASS_TO_PASS: list[str] = Field(default_factory=list)
    environment_setup_commit: str = ""


def _parse_json_list(value: object) -> list[str]:
    """Parse a field that may be a list, a JSON string encoding a list, or a scalar."""
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, tuple):
        return [str(item) for item in value]
    if isinstance(value, str):
        cleaned = value.strip()
        if not cleaned:
            return []
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, list):
                return [str(item) for item in parsed]
        except (json.JSONDecodeError, ValueError):
            pass
        return [cleaned]
    return [str(value)]


def _parse_instance(raw: dict) -> SWEBenchInstance:
    data = dict(raw)
    data["FAIL_TO_PASS"] = _parse_json_list(data.get("FAIL_TO_PASS"))
    data["PASS_TO_PASS"] = _parse_json_list(data.get("PASS_TO_PASS"))
    # Drop unknown fields that aren't in our model
    known_fields = set(SWEBenchInstance.model_fields.keys())
    data = {k: v for k, v in data.items() if k in known_fields}
    return SWEBenchInstance.model_validate(data)


def _filter_instances(
    instances: Sequence[SWEBenchInstance],
    instance_ids: set[str] | None,
    repo: str | None,
    index_start: int | None,
    index_end: int | None,
) -> list[SWEBenchInstance]:
    filtered = list(instances)

    if instance_ids:
        filtered = [item for item in filtered if item.instance_id in instance_ids]

    if repo:
        filtered = [item for item in filtered if item.repo == repo]

    start = index_start or 0
    if start < 0:
        start = 0

    end = index_end
    if end is not None and end < start:
        end = start

    return filtered[start:end]


def load_dataset(
    split: str = "test",
    instance_ids: list[str] | None = None,
    repo: str | None = None,
    index_start: int | None = None,
    index_end: int | None = None,
) -> list[SWEBenchInstance]:
    hf_dataset = hf_load_dataset(SWE_BENCH_MINI_DATASET, split=split)
    instances = [_parse_instance(record) for record in hf_dataset]
    ids_set = set(instance_ids) if instance_ids else None
    return _filter_instances(instances, ids_set, repo, index_start, index_end)


def list_repos(instances: list[SWEBenchInstance]) -> None:
    """Print available repos and instance counts."""
    counts: Counter[str] = Counter()
    for inst in instances:
        counts[inst.repo] += 1

    print(f"\nTotal instances: {len(instances)}")
    print(f"Repos: {len(counts)}\n")
    for repo, count in counts.most_common():
        print(f"  {repo}: {count}")
    print()
