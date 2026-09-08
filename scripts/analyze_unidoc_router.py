"""Analyze Pure LLM and Adaptive router CSV results for UniDoc-Bench."""

from __future__ import annotations

import argparse
import csv
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

TEXT_ONLY = "TEXT_ONLY"
VISUAL_REQUIRED = "VISUAL_REQUIRED"
VALID_ROUTES = {TEXT_ONLY, VISUAL_REQUIRED}
DEFAULT_LLM_CSV = Path("results/unidoc_router_llm.csv")
DEFAULT_ADAPTIVE_CSV = Path("results/unidoc_router_adaptive.csv")
DEFAULT_OUTPUT_DIR = Path("results")


def parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    return None


def _first(row: Mapping[str, Any], names: Iterable[str]) -> str:
    for name in names:
        value = row.get(name)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def canonicalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    expected = _first(row, ("expected_route",))
    predicted = _first(row, ("predicted_route", "actual_route", "route"))
    correct_value = _first(row, ("route_correct", "correct"))
    parsed_correct = parse_bool(correct_value)
    route_correct = (
        parsed_correct
        if parsed_correct is not None
        else expected in VALID_ROUTES and predicted in VALID_ROUTES and expected == predicted
    )
    try:
        latency = float(_first(row, ("routing_latency_ms",)) or 0.0)
    except ValueError:
        latency = 0.0
    return {
        "query_id": _first(row, ("query_id", "id")),
        "question": _first(row, ("question",)),
        "category": _first(row, ("category",)),
        "domain": _first(row, ("domain",)),
        "question_type": _first(row, ("question_type",)),
        "answer_type": _first(row, ("answer_type",)),
        "expected_route": expected,
        "predicted_route": predicted,
        "route_correct": route_correct,
        "routing_reason": _first(row, ("routing_reason", "reason")),
        "routing_latency_ms": latency,
        "used_llm_router": parse_bool(row.get("used_llm_router")),
    }


def load_results(path: str | Path) -> list[dict[str, Any]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        reader = csv.DictReader(source)
        if not reader.fieldnames:
            raise ValueError(f"CSV has no header: {path}")
        rows = [canonicalize_row(row) for row in reader]
    if not rows:
        raise ValueError(f"CSV has no result rows: {path}")
    missing_ids = sum(not row["query_id"] for row in rows)
    if missing_ids:
        raise ValueError(f"CSV has {missing_ids} row(s) without id/query_id: {path}")
    duplicate_ids = len(rows) - len({row["query_id"] for row in rows})
    if duplicate_ids:
        raise ValueError(f"CSV has {duplicate_ids} duplicate query_id row(s): {path}")
    return rows


def confusion_metrics(rows: Sequence[Mapping[str, Any]]) -> dict[str, int | float | None]:
    tp = tn = fp = fn = 0
    for row in rows:
        expected = row.get("expected_route")
        predicted = row.get("predicted_route")
        if expected == VISUAL_REQUIRED and predicted == VISUAL_REQUIRED:
            tp += 1
        elif expected == TEXT_ONLY and predicted == TEXT_ONLY:
            tn += 1
        elif expected == TEXT_ONLY and predicted == VISUAL_REQUIRED:
            fp += 1
        elif expected == VISUAL_REQUIRED and predicted == TEXT_ONLY:
            fn += 1
    evaluated = tp + tn + fp + fn

    def divide(numerator: float, denominator: float) -> float:
        return numerator / denominator if denominator else 0.0

    accuracy = divide(tp + tn, evaluated)
    visual_precision = divide(tp, tp + fp)
    visual_recall = divide(tp, tp + fn)
    text_recall = divide(tn, tn + fp)
    visual_f1 = divide(2 * visual_precision * visual_recall, visual_precision + visual_recall)
    balanced = (
        (visual_recall + text_recall) / 2
        if tp + fn > 0 and tn + fp > 0
        else None
    )
    return {
        "total": len(rows),
        "evaluated": evaluated,
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "accuracy": accuracy,
        "balanced_accuracy": balanced,
        "visual_precision": visual_precision,
        "visual_recall": visual_recall,
        "visual_f1": visual_f1,
        "text_recall": text_recall,
        "fn_rate": divide(fn, tp + fn),
        "fp_rate": divide(fp, tn + fp),
    }


def summarize_router(rows: Sequence[Mapping[str, Any]]) -> dict[str, int | float | None]:
    summary = confusion_metrics(rows)
    total = len(rows)
    summary.update(
        {
            "average_routing_latency_ms": (
                sum(float(row.get("routing_latency_ms", 0.0)) for row in rows) / total
                if total
                else 0.0
            ),
            "llm_router_call_count": sum(row.get("used_llm_router") is True for row in rows),
            "llm_router_call_rate": (
                sum(row.get("used_llm_router") is True for row in rows) / total
                if total
                else 0.0
            ),
            "predicted_visual_required_rate": (
                sum(row.get("predicted_route") == VISUAL_REQUIRED for row in rows) / total
                if total
                else 0.0
            ),
        }
    )
    return summary


def group_analysis(
    rows: Sequence[Mapping[str, Any]], field: str, router: str
) -> list[dict[str, Any]]:
    groups: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        value = str(row.get(field) or "<missing>")
        groups.setdefault(value, []).append(row)
    output: list[dict[str, Any]] = []
    for value in sorted(groups):
        group = groups[value]
        metrics = confusion_metrics(group)
        count = len(group)
        output.append(
            {
                "router": router,
                field: value,
                "count": count,
                "predicted_text_only": sum(
                    row.get("predicted_route") == TEXT_ONLY for row in group
                ),
                "predicted_visual_required": sum(
                    row.get("predicted_route") == VISUAL_REQUIRED for row in group
                ),
                "correct": int(metrics["tp"]) + int(metrics["tn"]),
                **{key: metrics[key] for key in metrics if key not in {"total", "evaluated"}},
                "predicted_vision_rate": (
                    sum(row.get("predicted_route") == VISUAL_REQUIRED for row in group) / count
                    if count
                    else 0.0
                ),
                "average_routing_latency_ms": (
                    sum(float(row.get("routing_latency_ms", 0.0)) for row in group) / count
                    if count
                    else 0.0
                ),
            }
        )
    return output


def adaptive_path_analysis(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    groups = {
        "rule_shortcut": [row for row in rows if row.get("used_llm_router") is False],
        "llm_fallback": [row for row in rows if row.get("used_llm_router") is True],
        "unknown": [row for row in rows if row.get("used_llm_router") is None],
    }
    output: list[dict[str, Any]] = []
    for name, group in groups.items():
        if not group:
            continue
        metrics = confusion_metrics(group)
        expected = Counter(str(row.get("expected_route") or "<missing>") for row in group)
        predicted = Counter(str(row.get("predicted_route") or "<missing>") for row in group)
        output.append(
            {
                "group": name,
                "count": len(group),
                "correct": int(metrics["tp"]) + int(metrics["tn"]),
                "accuracy": metrics["accuracy"],
                "tp": metrics["tp"],
                "tn": metrics["tn"],
                "fp": metrics["fp"],
                "fn": metrics["fn"],
                "expected_text_only": expected[TEXT_ONLY],
                "expected_visual_required": expected[VISUAL_REQUIRED],
                "predicted_text_only": predicted[TEXT_ONLY],
                "predicted_visual_required": predicted[VISUAL_REQUIRED],
            }
        )
    return output


def compare_routers(
    llm_rows: Sequence[Mapping[str, Any]],
    adaptive_rows: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, int], list[dict[str, Any]]]:
    llm_by_id = {str(row["query_id"]): row for row in llm_rows}
    adaptive_by_id = {str(row["query_id"]): row for row in adaptive_rows}
    shared_ids = sorted(llm_by_id.keys() & adaptive_by_id.keys())
    counts = Counter()
    disagreements: list[dict[str, Any]] = []
    for query_id in shared_ids:
        llm = llm_by_id[query_id]
        adaptive = adaptive_by_id[query_id]
        same = llm.get("predicted_route") == adaptive.get("predicted_route")
        counts["same_prediction" if same else "different_prediction"] += 1
        llm_correct = bool(llm.get("route_correct"))
        adaptive_correct = bool(adaptive.get("route_correct"))
        if llm_correct and adaptive_correct:
            counts["both_correct"] += 1
        elif llm_correct:
            counts["llm_only_correct"] += 1
        elif adaptive_correct:
            counts["adaptive_only_correct"] += 1
        else:
            counts["both_wrong"] += 1
        if not same:
            disagreements.append(
                {
                    "query_id": query_id,
                    "question": adaptive.get("question") or llm.get("question", ""),
                    "answer_type": adaptive.get("answer_type") or llm.get("answer_type", ""),
                    "expected_route": adaptive.get("expected_route") or llm.get("expected_route", ""),
                    "llm_prediction": llm.get("predicted_route", ""),
                    "adaptive_prediction": adaptive.get("predicted_route", ""),
                    "adaptive_used_llm_router": adaptive.get("used_llm_router"),
                    "llm_routing_reason": llm.get("routing_reason", ""),
                    "adaptive_routing_reason": adaptive.get("routing_reason", ""),
                }
            )
    counts["joined_queries"] = len(shared_ids)
    counts["llm_only_queries"] = len(llm_by_id.keys() - adaptive_by_id.keys())
    counts["adaptive_only_queries"] = len(adaptive_by_id.keys() - llm_by_id.keys())
    return dict(counts), disagreements


def error_samples(
    rows: Sequence[Mapping[str, Any]], expected: str, predicted: str
) -> list[dict[str, Any]]:
    fields = (
        "query_id",
        "question",
        "answer_type",
        "question_type",
        "domain",
        "expected_route",
        "predicted_route",
        "used_llm_router",
        "routing_reason",
    )
    matches = [
        row
        for row in rows
        if row.get("expected_route") == expected and row.get("predicted_route") == predicted
    ]
    return [{field: row.get(field, "") for field in fields} for row in sorted(matches, key=lambda row: str(row["query_id"]))]


def write_csv(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = list(dict.fromkeys(key for row in rows for key in row))
    with path.open("w", encoding="utf-8", newline="") as output:
        if not fieldnames:
            return
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def _format(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6f}"
    return str(value)


def print_records(title: str, rows: Sequence[Mapping[str, Any]], limit: int | None = None) -> None:
    print(f"\n{title}")
    for row in rows[:limit]:
        print("  " + " | ".join(f"{key}={_format(value)}" for key, value in row.items()))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--llm-csv", type=Path, default=DEFAULT_LLM_CSV)
    parser.add_argument("--adaptive-csv", type=Path, default=DEFAULT_ADAPTIVE_CSV)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    datasets = {
        "llm": load_results(args.llm_csv),
        "adaptive": load_results(args.adaptive_csv),
    }
    grouped: dict[str, list[dict[str, Any]]] = {}
    for router, rows in datasets.items():
        print_records(f"{router.upper()} overall", [summarize_router(rows)])
        for field in ("answer_type", "question_type", "domain"):
            grouped.setdefault(field, []).extend(group_analysis(rows, field, router))

    for field, rows in grouped.items():
        print_records(f"By {field}", rows)
        write_csv(args.output_dir / f"unidoc_router_analysis_by_{field}.csv", rows)

    adaptive_paths = adaptive_path_analysis(datasets["adaptive"])
    print_records("Adaptive rule shortcut vs LLM fallback", adaptive_paths)

    comparison, disagreements = compare_routers(datasets["llm"], datasets["adaptive"])
    print_records("Pure LLM vs Adaptive comparison", [comparison])
    print_records("Disagreement samples (max 20)", disagreements, limit=20)
    write_csv(args.output_dir / "unidoc_router_disagreements.csv", disagreements)

    fn_rows = error_samples(datasets["adaptive"], VISUAL_REQUIRED, TEXT_ONLY)
    fp_rows = error_samples(datasets["adaptive"], TEXT_ONLY, VISUAL_REQUIRED)
    print_records("Adaptive FN samples (max 20)", fn_rows, limit=20)
    print_records("Adaptive FP samples (max 20)", fp_rows, limit=20)
    write_csv(args.output_dir / "unidoc_router_adaptive_fn.csv", fn_rows)
    write_csv(args.output_dir / "unidoc_router_adaptive_fp.csv", fp_rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
