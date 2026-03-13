from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from benchmarks.swe_evo.evaluate import evaluate_run
from benchmarks.swe_evo.results import ResultStore

from .dataset import SWEBenchInstance, list_repos, load_dataset
from .runner import BenchmarkRunner, compare_models, print_comparison_table

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("swe_bench")


def _parse_value(raw: str) -> Any:
    lowered = raw.lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        pass
    return raw


def _parse_config_overrides(items: list[str]) -> dict[str, Any]:
    overrides: dict[str, Any] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid --config value '{item}'. Expected KEY=VALUE.")
        key, value = item.split("=", 1)
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid --config value '{item}'. Key cannot be empty.")
        overrides[key] = _parse_value(value.strip())
    return overrides


def _select_instances(args: argparse.Namespace) -> list[SWEBenchInstance]:
    instance_ids = None
    if hasattr(args, "instances") and args.instances:
        instance_ids = [
            item.strip() for item in args.instances.split(",") if item.strip()
        ]

    repo = getattr(args, "repo", None)

    instances = load_dataset(split="test", instance_ids=instance_ids, repo=repo)

    limit = getattr(args, "limit", None)
    if limit is not None:
        instances = instances[: max(0, limit)]

    return instances


async def _cmd_run(args: argparse.Namespace) -> int:
    store = ResultStore(args.results_dir)
    config_overrides = _parse_config_overrides(args.config)
    instances = _select_instances(args)

    if args.resume:
        completed_ids = store.get_completed_ids()
        before = len(instances)
        instances = [
            item for item in instances if item.instance_id not in completed_ids
        ]
        logger.info(
            "Resume: skipping %d already-completed, %d remaining",
            before - len(instances),
            len(instances),
        )

    logger.info("Selected %d SWE-Bench instances", len(instances))

    if not instances:
        logger.info("Nothing to run.")
        return 0

    async with BenchmarkRunner(
        server=args.server,
        api_key=args.api_key,
        node_id=args.node_id,
        results_dir=args.results_dir,
        model=args.model,
        config_overrides=config_overrides,
        timeout_seconds=args.timeout,
        poll_interval=args.poll_interval,
        repo_path=getattr(args, "repo_path", None),
    ) as runner:
        await runner.run_all(
            instances=instances,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
        )

    logger.info("Run completed.")
    print(json.dumps(store.summary(), indent=2))
    return 0


def _stage_repos_for_models(
    instances: list[SWEBenchInstance],
    models: list[str],
    staged_dir: str,
) -> None:
    """Pre-clone repos at the correct base_commit, one copy per model."""
    staged_path = Path(staged_dir)
    staged_path.mkdir(parents=True, exist_ok=True)

    for instance in instances:
        repo_url = f"https://github.com/{instance.repo}"
        template_dir = staged_path / "_template"

        # Clone template if not already present
        if not template_dir.exists():
            logger.info("Cloning %s into template...", instance.repo)
            subprocess.run(
                ["git", "clone", "--quiet", repo_url, str(template_dir)],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(template_dir), "checkout", instance.base_commit],
                check=True,
            )
            logger.info("Template at commit %s", instance.base_commit)

        # Create per-model copies
        for model in models:
            model_slug = model.replace("/", "_")
            dest = staged_path / f"django_{model_slug}"
            if dest.exists():
                # Verify commit
                result = subprocess.run(
                    ["git", "-C", str(dest), "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                )
                if result.stdout.strip().startswith(instance.base_commit[:12]):
                    logger.info("Repo for %s already staged at correct commit", model)
                    continue
                logger.warning("Repo for %s at wrong commit, re-staging...", model)
                shutil.rmtree(dest)

            logger.info("Copying template -> %s", dest.name)
            shutil.copytree(str(template_dir), str(dest), symlinks=True)

        # Clean up template
        if template_dir.exists():
            shutil.rmtree(template_dir)


async def _cmd_compare(args: argparse.Namespace) -> int:
    config_overrides = _parse_config_overrides(args.config)
    instances = _select_instances(args)

    if not instances:
        logger.error("No instances selected.")
        return 1

    if not args.models or len(args.models) < 2:
        logger.error("Compare requires at least 2 --models.")
        return 1

    staged_dir_container: str | None = None
    staged_dir: str | None = getattr(args, "staged_dir", None)

    if staged_dir:
        logger.info("Staging repos in %s ...", staged_dir)
        _stage_repos_for_models(instances, args.models, staged_dir)
        staged_dir_container = "/workspaces/staged"

        # Verify staging
        for model in args.models:
            model_slug = model.replace("/", "_")
            repo_dir = Path(staged_dir) / f"django_{model_slug}"
            result = subprocess.run(
                ["git", "-C", str(repo_dir), "log", "--oneline", "-1"],
                capture_output=True,
                text=True,
            )
            logger.info("Staged %s: %s", model, result.stdout.strip())

    logger.info(
        "Comparing %d models on %d instances: %s",
        len(args.models),
        len(instances),
        args.models,
    )

    model_results = await compare_models(
        instances=instances,
        models=args.models,
        server=args.server,
        api_key=args.api_key,
        node_id=args.node_id,
        results_base_dir=args.results_dir,
        config_overrides=config_overrides,
        timeout_seconds=args.timeout,
        poll_interval=args.poll_interval,
        staged_dir_container=staged_dir_container,
    )

    print_comparison_table(model_results)
    return 0


async def _cmd_evaluate(args: argparse.Namespace) -> int:
    store = ResultStore(args.results_dir)
    results = list(store.load_latest_by_instance().values())

    if not results:
        logger.error("No results found in %s", args.results_dir)
        return 1

    evaluation = evaluate_run(results)
    print(evaluation.model_dump_json(indent=2))
    return 0


async def _cmd_status(args: argparse.Namespace) -> int:
    store = ResultStore(args.results_dir)
    summary = store.summary()

    if summary.get("total", 0) == 0:
        logger.info("No results found in %s", args.results_dir)
        return 0

    print(json.dumps(summary, indent=2))
    return 0


async def _cmd_list_instances(args: argparse.Namespace) -> int:
    instances = load_dataset(split="test")
    list_repos(instances)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.swe_bench",
        description="SWE-Bench verified-mini benchmark for SWE-AF",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # --- run ---
    run_p = sub.add_parser("run", help="Run benchmark instances")
    run_p.add_argument("--server", default="http://localhost:8080")
    run_p.add_argument("--api-key", default=os.getenv("AGENTFIELD_API_KEY"))
    run_p.add_argument("--node-id", default="swe-planner")
    run_p.add_argument("--results-dir", default="./swe_bench_results")
    run_p.add_argument("--model", default="openrouter/qwen/qwen3-coder-next")
    run_p.add_argument("--batch-size", type=int, default=5)
    run_p.add_argument("--concurrency", type=int, default=3)
    run_p.add_argument("--timeout", type=float, default=3600)
    run_p.add_argument("--poll-interval", type=float, default=30.0)
    run_p.add_argument("--instances", default="", help="Comma-separated instance IDs")
    run_p.add_argument("--repo", default=None, help="Filter by repo (e.g. django/django)")
    run_p.add_argument("--limit", type=int, help="Max instances to run")
    run_p.add_argument("--resume", action="store_true", help="Skip already-completed")
    run_p.add_argument(
        "--config",
        action="append",
        default=[],
        help="Config override KEY=VALUE (repeatable)",
    )
    run_p.add_argument(
        "--repo-path",
        default=None,
        help="Container path to pre-staged repo (skips cloning)",
    )

    # --- compare ---
    cmp_p = sub.add_parser("compare", help="Compare models on same instances")
    cmp_p.add_argument("--server", default="http://localhost:8080")
    cmp_p.add_argument("--api-key", default=os.getenv("AGENTFIELD_API_KEY"))
    cmp_p.add_argument("--node-id", default="swe-planner")
    cmp_p.add_argument("--results-dir", default="./swe_bench_results")
    cmp_p.add_argument(
        "--models",
        nargs="+",
        required=True,
        help="Models to compare (space-separated OpenRouter IDs)",
    )
    cmp_p.add_argument("--instances", default="", help="Comma-separated instance IDs")
    cmp_p.add_argument("--repo", default=None, help="Filter by repo")
    cmp_p.add_argument("--limit", type=int, help="Max instances")
    cmp_p.add_argument("--timeout", type=float, default=3600)
    cmp_p.add_argument("--poll-interval", type=float, default=30.0)
    cmp_p.add_argument(
        "--config",
        action="append",
        default=[],
        help="Config override KEY=VALUE (repeatable)",
    )
    cmp_p.add_argument(
        "--staged-dir",
        default=None,
        help="Host directory for pre-staged repos (clones at base_commit, one per model)",
    )

    # --- evaluate ---
    eval_p = sub.add_parser("evaluate", help="Evaluate stored results")
    eval_p.add_argument("--results-dir", default="./swe_bench_results")

    # --- status ---
    status_p = sub.add_parser("status", help="Show run summary")
    status_p.add_argument("--results-dir", default="./swe_bench_results")

    # --- list-instances ---
    sub.add_parser("list-instances", help="Show available instances grouped by repo")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {
        "run": _cmd_run,
        "compare": _cmd_compare,
        "evaluate": _cmd_evaluate,
        "status": _cmd_status,
        "list-instances": _cmd_list_instances,
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
