from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from typing import Any

from .dataset import SWEEvoInstance, load_dataset
from .evaluate import evaluate_run
from .results import ResultStore
from .runner import BenchmarkRunner

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("swe_evo")


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


def _select_instances(args: argparse.Namespace) -> list[SWEEvoInstance]:
    instance_ids = None
    if args.instances:
        instance_ids = [
            item.strip() for item in args.instances.split(",") if item.strip()
        ]

    instances = load_dataset(split="test", instance_ids=instance_ids)

    if args.limit is not None:
        instances = instances[: max(0, args.limit)]

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

    logger.info("Selected %d SWE-EVO instances", len(instances))

    if not instances:
        logger.info("Nothing to run.")
        return 0

    async with BenchmarkRunner(
        server=args.server,
        api_key=args.api_key,
        node_id=args.node_id,
        results_dir=args.results_dir,
        config_overrides=config_overrides,
        timeout_seconds=args.timeout,
        poll_interval=args.poll_interval,
    ) as runner:
        await runner.run_all(
            instances=instances,
            batch_size=args.batch_size,
            concurrency=args.concurrency,
        )

    logger.info("Run completed.")
    print(json.dumps(store.summary(), indent=2))
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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m benchmarks.swe_evo",
        description="SWE-EVO benchmark for SWE-AF (direct HTTP, no SDK)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run_p = sub.add_parser("run", help="Run benchmark instances")
    run_p.add_argument("--server", default="http://localhost:8080")
    run_p.add_argument("--api-key", default=os.getenv("AGENTFIELD_API_KEY"))
    run_p.add_argument("--node-id", default="swe-planner")
    run_p.add_argument("--results-dir", default="./benchmark_results")
    run_p.add_argument("--batch-size", type=int, default=5)
    run_p.add_argument("--concurrency", type=int, default=3)
    run_p.add_argument("--timeout", type=float, default=3600)
    run_p.add_argument("--poll-interval", type=float, default=30.0)
    run_p.add_argument("--instances", default="", help="Comma-separated instance IDs")
    run_p.add_argument("--limit", type=int, help="Max instances to run")
    run_p.add_argument("--resume", action="store_true", help="Skip already-completed")
    run_p.add_argument(
        "--config",
        action="append",
        default=[],
        help="Config override KEY=VALUE (repeatable)",
    )

    eval_p = sub.add_parser("evaluate", help="Evaluate stored results")
    eval_p.add_argument("--results-dir", default="./benchmark_results")

    status_p = sub.add_parser("status", help="Show run summary")
    status_p.add_argument("--results-dir", default="./benchmark_results")

    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    handlers = {
        "run": _cmd_run,
        "evaluate": _cmd_evaluate,
        "status": _cmd_status,
    }

    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        return 1

    return asyncio.run(handler(args))


if __name__ == "__main__":
    raise SystemExit(main())
