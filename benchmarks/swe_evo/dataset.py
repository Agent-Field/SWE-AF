from __future__ import annotations

from collections.abc import Sequence

from datasets import load_dataset as hf_load_dataset
from pydantic import BaseModel, Field

SWE_EVO_DATASET_NAME = "Fsoft-AIC/SWE-EVO"


class SWEEvoInstance(BaseModel):
    instance_id: str
    repo: str
    base_commit: str
    patch: str
    test_patch: str
    problem_statement: str
    FAIL_TO_PASS: list[str] = Field(default_factory=list)
    PASS_TO_PASS: list[str] = Field(default_factory=list)
    environment_setup_commit: str
    start_version: str
    end_version: str
    image: str
    instance_id_swe: str
    bench: str
    version: str
    test_cmds: str
    log_parser: str


def _to_string_list(value: object) -> list[str]:
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
        return [cleaned]
    return [str(value)]


def _parse_instance(raw: dict) -> SWEEvoInstance:
    data = dict(raw)
    data["FAIL_TO_PASS"] = _to_string_list(data.get("FAIL_TO_PASS"))
    data["PASS_TO_PASS"] = _to_string_list(data.get("PASS_TO_PASS"))
    return SWEEvoInstance.model_validate(data)


def _filter_instances(
    instances: Sequence[SWEEvoInstance],
    instance_ids: set[str] | None,
    repo: str | None,
    index_start: int | None,
    index_end: int | None,
) -> list[SWEEvoInstance]:
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
) -> list[SWEEvoInstance]:
    hf_dataset = hf_load_dataset(SWE_EVO_DATASET_NAME, split=split)
    instances = [_parse_instance(record) for record in hf_dataset]
    ids_set = set(instance_ids) if instance_ids else None
    return _filter_instances(instances, ids_set, repo, index_start, index_end)
