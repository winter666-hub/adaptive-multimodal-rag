"""Benchmark the deterministic rule-based query router without external APIs."""

from __future__ import annotations

import argparse
import csv
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from furiosa_rag.benchmark_dataset import load_benchmark_jsonl
from furiosa_rag.clients import FuriosaClient
from furiosa_rag.config import Settings
from furiosa_rag.router import (
    AdaptiveQueryRouter,
    EnhancedRuleBasedQueryRouter,
    LLMQueryRouter,
    QueryRoute,
    QueryRouter,
    RuleBasedQueryRouter,
)
from furiosa_rag.routing_metrics import compute_routing_metrics

CSV_FIELDS = [
    "id",
    "query_id",
    "question",
    "category",
    "domain",
    "question_type",
    "answer_type",
    "expected_route",
    "actual_route",
    "predicted_route",
    "correct",
    "route_correct",
    "reason",
    "routing_latency_ms",
    "used_llm_router",
    "router_model_id",
    "router_temperature",
    "router_max_tokens",
    "router_thinking_enabled",
    "router_prompt_version",
    "router_prompt_sha256",
    "router_endpoint",
    "benchmark_execution_timestamp",
]


def _non_secret_endpoint_identity(endpoint: str) -> str:
    """Remove credentials, query parameters, and fragments from an endpoint URL."""
    parsed = urlsplit(endpoint)
    hostname = parsed.hostname or ""
    if ":" in hostname and not hostname.startswith("["):
        hostname = f"[{hostname}]"
    netloc = hostname
    if parsed.port is not None:
        netloc = f"{netloc}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _reproducibility_metadata(router: QueryRouter) -> dict[str, Any]:
    provider = getattr(router, "reproducibility_metadata", None)
    if not callable(provider):
        return {}
    provided = provider()
    if not isinstance(provided, dict):
        return {}
    metadata = dict(provided)
    endpoint = metadata.get("router_endpoint")
    if isinstance(endpoint, str):
        metadata["router_endpoint"] = _non_secret_endpoint_identity(endpoint)
    return metadata


def load_jsonl(path: str | Path) -> list[dict[str, Any]]:
    return load_benchmark_jsonl(path)


def evaluate(
    rows: list[dict[str, Any]], router: QueryRouter | None = None
) -> list[dict[str, Any]]:
    active_router = router or RuleBasedQueryRouter()
    execution_timestamp = datetime.now(timezone.utc).isoformat()
    run_metadata = _reproducibility_metadata(active_router)
    results: list[dict[str, Any]] = []
    for row in rows:
        started = time.perf_counter_ns()
        decision = active_router.route(row["question"])
        latency_ms = (time.perf_counter_ns() - started) / 1_000_000
        predicted_route = decision.route.value
        expected_route = row.get("expected_route")
        route_correct = (
            predicted_route == expected_route if expected_route is not None else None
        )
        results.append(
            {
                **row,
                "query_id": row["id"],
                "actual_route": predicted_route,
                "predicted_route": predicted_route,
                "correct": route_correct,
                "route_correct": route_correct,
                "reason": decision.reason,
                "routing_latency_ms": latency_ms,
                "used_llm_router": decision.used_llm_router,
                **run_metadata,
                "benchmark_execution_timestamp": execution_timestamp,
            }
        )
    return results


def summarize(results: list[dict[str, Any]]) -> dict[str, int | float]:
    total = len(results)
    metrics = compute_routing_metrics(results, predicted_field="actual_route")
    correct = int(metrics["tp"]) + int(metrics["tn"])
    category_totals = Counter(
        str(row["category"]) for row in results if row.get("category")
    )
    category_correct = Counter(
        str(row["category"])
        for row in results
        if row.get("category") and bool(row["correct"])
    )

    def category_accuracy(category: str) -> float:
        count = category_totals[category]
        return category_correct[category] / count if count else 0.0

    vision_calls = sum(
        row["actual_route"] == QueryRoute.VISUAL_REQUIRED.value for row in results
    )
    llm_router_calls = sum(bool(row.get("used_llm_router", False)) for row in results)
    summary: dict[str, int | float] = {
        **metrics,
        "total": total,
        "correct": correct,
        "false_positives": int(metrics["fp"]),
        "false_negatives": int(metrics["fn"]),
        "predicted_vision_call_rate": vision_calls / total if total else 0.0,
        "average_routing_latency_ms": (
            sum(float(row["routing_latency_ms"]) for row in results) / total
            if total
            else 0.0
        ),
        "llm_router_calls": llm_router_calls,
        "llm_router_call_rate": llm_router_calls / total if total else 0.0,
    }
    if category_totals:
        summary.update(
            {
                "text_only_accuracy": category_accuracy("text"),
                "explicit_visual_accuracy": category_accuracy("explicit_visual"),
                "implicit_visual_accuracy": category_accuracy("implicit_visual"),
            }
        )
    return summary


def export_csv(results: list[dict[str, Any]], path: str | Path) -> None:
    output_path = Path(path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="") as output:
        writer = csv.DictWriter(output, fieldnames=CSV_FIELDS)
        writer.writeheader()
        for row in results:
            exported = {field: row.get(field, "") for field in CSV_FIELDS}
            exported["routing_latency_ms"] = f"{float(row['routing_latency_ms']):.6f}"
            writer.writerow(exported)


def main() -> int:
    parser = argparse.ArgumentParser(description="Benchmark a deterministic query router")
    parser.add_argument("dataset", help="JSONL router evaluation dataset")
    parser.add_argument(
        "--router", choices=("rule", "enhanced", "llm", "adaptive"), default="rule",
        help="router implementation to benchmark (default: rule)",
    )
    parser.add_argument("--output", help="optional CSV result path")
    args = parser.parse_args()

    if args.router == "rule":
        router: QueryRouter = RuleBasedQueryRouter()
    elif args.router == "enhanced":
        router = EnhancedRuleBasedQueryRouter()
    else:
        settings = Settings.from_env()
        endpoint = next(item for item in settings.endpoints if item.name == "llm")
        llm_router = LLMQueryRouter(
            endpoint,
            FuriosaClient(api_key=settings.api_key, timeout=settings.request_timeout),
        )
        router = llm_router if args.router == "llm" else AdaptiveQueryRouter(llm_router)
    results = evaluate(load_jsonl(args.dataset), router)
    for row in results:
        print(json.dumps(row, ensure_ascii=False))

    summary = summarize(results)
    print("\nSummary")
    for key, value in summary.items():
        if key.endswith(("accuracy", "rate")):
            print(f"{key}: {float(value):.2%}")
        else:
            print(f"{key}: {value}")

    if args.output:
        export_csv(results, args.output)
        print(f"output: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
