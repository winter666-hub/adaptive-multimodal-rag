"""Shared binary metrics for query-routing benchmarks."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from furiosa_rag.router import QueryRoute


def _safe_divide(numerator: int, denominator: int) -> float:
    return numerator / denominator if denominator else 0.0


def compute_routing_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    expected_field: str = "expected_route",
    predicted_field: str = "predicted_route",
) -> dict[str, int | float]:
    """Calculate metrics with VISUAL_REQUIRED as the positive class."""
    text = QueryRoute.TEXT_ONLY.value
    visual = QueryRoute.VISUAL_REQUIRED.value
    valid_routes = {text, visual}
    tp = tn = fp = fn = 0
    error_count = 0
    unevaluated_count = 0

    for row in rows:
        if row.get("error"):
            error_count += 1
        expected = row.get(expected_field)
        predicted = row.get(predicted_field)
        if expected not in valid_routes or predicted not in valid_routes:
            unevaluated_count += 1
            continue
        if expected == visual and predicted == visual:
            tp += 1
        elif expected == text and predicted == text:
            tn += 1
        elif expected == text and predicted == visual:
            fp += 1
        else:
            fn += 1

    evaluated_count = tp + tn + fp + fn
    accuracy = _safe_divide(tp + tn, evaluated_count)
    visual_precision = _safe_divide(tp, tp + fp)
    visual_recall = _safe_divide(tp, tp + fn)
    visual_f1 = _safe_divide(
        2 * visual_precision * visual_recall,
        visual_precision + visual_recall,
    )
    text_recall = _safe_divide(tn, tn + fp)
    return {
        "tp": tp,
        "tn": tn,
        "fp": fp,
        "fn": fn,
        "accuracy": accuracy,
        "visual_precision": visual_precision,
        "visual_recall": visual_recall,
        "visual_f1": visual_f1,
        "text_recall": text_recall,
        "specificity": text_recall,
        "balanced_accuracy": (visual_recall + text_recall) / 2,
        "evaluated_count": evaluated_count,
        "error_count": error_count,
        "unevaluated_count": unevaluated_count,
    }
