"""Offline route utility from paired forced-run correctness, never answer-type GT."""

from __future__ import annotations

import csv
import math
import random
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from furiosa_rag.route_gt_audit import ALIGNMENT_OUTCOMES, ANSWER_TYPES, alignment_outcome

OPTIMAL_ROUTES = {
    "BOTH_CORRECT": "EITHER",
    "TEXT_ONLY_BETTER": "TEXT_ONLY",
    "VISION_NEEDED": "VISUAL_REQUIRED",
    "BOTH_WRONG": "NEITHER",
}
ROUTES = {"TEXT_ONLY", "VISUAL_REQUIRED"}
NOTES = (
    "Utility uses forced Text/Vision judge correctness; answer_type only groups samples.",
    "Regret is a retrospective forced-run oracle proxy, not a causal claim about RA answers.",
    (
        "BOTH_CORRECT unnecessary Vision means forced Text was also correct in hindsight; "
        "it does not imply runtime oracle knowledge."
    ),
    (
        "BOTH_WRONG means neither observed forced run succeeded; routing alone is not an "
        "established remedy. RA may still succeed; page hits do not establish failure causes."
    ),
    (
        "N/A means no eligible samples. Page-hit rates use available rows only. "
        "CSV rates are fractions; latencies are milliseconds."
    ),
    (
        "visual_page_hit_1 preserves existing page_hit_1 values, including stored False "
        "without a selected page; its denominator is not restricted to Vision calls."
    ),
)


def load_csv(path: str | Path) -> list[dict[str, str]]:
    with Path(path).open(encoding="utf-8-sig", newline="") as source:
        return list(csv.DictReader(source))


def _index(rows: Sequence[Mapping[str, Any]], source: str) -> dict[str, Mapping[str, Any]]:
    if not rows:
        raise ValueError(f"{source}: empty input (missing IDs)")
    indexed = {}
    for number, row in enumerate(rows, 2):
        ids = [str(row[key]).strip() for key in ("query_id", "id") if row.get(key) is not None]
        if not ids or any(not value for value in ids):
            raise ValueError(f"{source}: missing ID at CSV row {number}")
        if len(set(ids)) != 1:
            raise ValueError(f"{source}: conflicting id/query_id at CSV row {number}")
        query_id = ids[0]
        if query_id in indexed:
            raise ValueError(f"{source}: duplicate ID {query_id!r}")
        if str(row.get("error") or "").strip():
            raise ValueError(f"{source}: error for ID {query_id!r}: {row['error']}")
        indexed[query_id] = row
    return indexed


def _boolean(value: Any, context: str, *, optional: bool = False) -> bool | None:
    token = str(value).strip().lower()
    if token in {"true", "1"}:
        return True
    if token in {"false", "0"}:
        return False
    if optional and token in {"", "none", "null", "nan", "n/a"}:
        return None
    raise ValueError(f"{context}: expected boolean, got {value!r}")


def _latency(value: Any, context: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{context}: expected finite nonnegative latency, got {value!r}") from exc
    if not math.isfinite(number) or number < 0:
        raise ValueError(f"{context}: expected finite nonnegative latency, got {value!r}")
    return number


def _route(row: Mapping[str, Any], context: str) -> str:
    labels = [str(row[key]).strip() for key in ("predicted_route", "route") if row.get(key)]
    if not labels or len(set(labels)) != 1 or labels[0] not in ROUTES:
        raise ValueError(f"{context}: missing, invalid, or conflicting route: {labels}")
    return labels[0]


def build_utility_cases(
    alignment_rows: Sequence[Mapping[str, Any]],
    retrieval_aware_rows: Sequence[Mapping[str, Any]],
    judged_rows: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Join exact ID sets. EITHER is acceptable; NEITHER utility is indeterminate (null)."""
    alignment = _index(alignment_rows, "alignment")
    retrieval = _index(retrieval_aware_rows, "retrieval-aware")
    judged = _index(judged_rows, "retrieval-aware-judged")
    for name, indexed in (("retrieval-aware", retrieval), ("retrieval-aware-judged", judged)):
        missing = sorted(alignment.keys() - indexed.keys())
        extra = sorted(indexed.keys() - alignment.keys())
        if missing or extra:
            raise ValueError(f"{name}: ID mismatch; missing IDs={missing}; extra IDs={extra}")

    cases = []
    for query_id in sorted(alignment):
        source, run, judgment = alignment[query_id], retrieval[query_id], judged[query_id]
        text_correct = _boolean(source.get("text_correct"), f"{query_id}/text_correct")
        vision_correct = _boolean(source.get("vision_correct"), f"{query_id}/vision_correct")
        category = alignment_outcome(text_correct, vision_correct)
        if source.get("outcome") != category:
            raise ValueError(f"{query_id}: alignment outcome disagrees with forced correctness")
        answer_type = source.get("answer_type")
        if answer_type not in ANSWER_TYPES:
            raise ValueError(f"{query_id}: invalid answer_type {answer_type!r}")
        predicted = _route(run, f"{query_id}/retrieval-aware")
        for name, row in (("retrieval-aware", run), ("retrieval-aware-judged", judgment)):
            if row.get("strategy") not in (None, "", "retrieval_aware_adaptive"):
                raise ValueError(f"{query_id}/{name}: unexpected strategy {row['strategy']!r}")
            if row.get("answer_type") not in (None, "", answer_type):
                raise ValueError(f"{query_id}/{name}: answer_type mismatch")
        if (judgment.get("predicted_route") or judgment.get("route")) and (
            _route(judgment, f"{query_id}/judged") != predicted
        ):
            raise ValueError(f"{query_id}: judged route mismatch")
        if "answer" in run and "answer" in judgment and run["answer"] != judgment["answer"]:
            raise ValueError(f"{query_id}: judged answer mismatch")
        optimal = OPTIMAL_ROUTES[category]
        decisive = optimal in ROUTES
        cases.append(
            {
                "id": query_id,
                "answer_type": answer_type,
                "alignment_category": category,
                "optimal_route": optimal,
                "predicted_route": predicted,
                "route_utility_correct": None
                if optimal == "NEITHER"
                else (optimal == "EITHER" or predicted == optimal),
                "decisive": decisive,
                "avoidable_regret": decisive and predicted != optimal,
                "forced_text_correct": text_correct,
                "forced_vision_correct": vision_correct,
                "forced_text_latency_ms": _latency(
                    source.get("text_e2e_latency_ms"), f"{query_id}/forced_text_latency"
                ),
                "forced_vision_latency_ms": _latency(
                    source.get("vision_e2e_latency_ms"), f"{query_id}/forced_vision_latency"
                ),
                "retrieval_aware_correct": _boolean(
                    judgment.get("judge_correct"), f"{query_id}/judge_correct"
                ),
                "total_latency_ms": _latency(run.get("total_latency_ms"), f"{query_id}/total"),
                "routing_latency_ms": _latency(
                    run.get("routing_latency_ms"), f"{query_id}/routing"
                ),
                "retrieval_page_hit_3": _boolean(
                    run.get("retrieval_page_hit_3"),
                    f"{query_id}/retrieval_page_hit_3",
                    optional=True,
                ),
                "visual_page_hit_1": _boolean(
                    run.get("visual_page_hit_1", run.get("page_hit_1")),
                    f"{query_id}/visual_page_hit_1",
                    optional=True,
                ),
            }
        )
    return cases


def _ratio(numerator: float, denominator: int) -> float | None:
    return numerator / denominator if denominator else None


def _summarize(group: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    count = len(group)
    text_count = sum(row["predicted_route"] == "TEXT_ONLY" for row in group)
    vision_needed = [row for row in group if row["alignment_category"] == "VISION_NEEDED"]
    text_better = [row for row in group if row["alignment_category"] == "TEXT_ONLY_BETTER"]
    tp = sum(row["predicted_route"] == "VISUAL_REQUIRED" for row in vision_needed)
    fn = len(vision_needed) - tp
    tn = sum(row["predicted_route"] == "TEXT_ONLY" for row in text_better)
    fp = len(text_better) - tn
    decisive = len(vision_needed) + len(text_better)
    vision_recall, text_recall = _ratio(tp, len(vision_needed)), _ratio(tn, len(text_better))
    result = {
        "count": count,
        "ra_text_only_count": text_count,
        "ra_text_only_rate": _ratio(text_count, count),
        "ra_visual_required_count": count - text_count,
        "ra_visual_required_rate": _ratio(count - text_count, count),
        "ra_correctness": _ratio(sum(row["retrieval_aware_correct"] for row in group), count),
        "forced_text_correctness": _ratio(sum(row["forced_text_correct"] for row in group), count),
        "forced_vision_correctness": _ratio(
            sum(row["forced_vision_correct"] for row in group), count
        ),
        "ra_avg_e2e_latency_ms": _ratio(sum(row["total_latency_ms"] for row in group), count),
        "ra_avg_routing_latency_ms": _ratio(sum(row["routing_latency_ms"] for row in group), count),
        "decisive_count": decisive,
        "decisive_correct_count": tp + tn,
        "decisive_route_accuracy": _ratio(tp + tn, decisive),
        "vision_needed_count": len(vision_needed),
        "vision_selection_recall": vision_recall,
        "text_only_better_count": len(text_better),
        "text_selection_recall": text_recall,
        "optimal_visual_predicted_visual_count": tp,
        "optimal_visual_predicted_text_count": fn,
        "optimal_text_predicted_text_count": tn,
        "optimal_text_predicted_visual_count": fp,
        "decisive_vision_precision": _ratio(tp, tp + fp),
        "decisive_vision_recall": vision_recall,
        "decisive_vision_f1": _ratio(2 * tp, 2 * tp + fp + fn),
        "decisive_balanced_accuracy": (
            (vision_recall + text_recall) / 2
            if vision_recall is not None and text_recall is not None
            else None
        ),
        "avoidable_regret_count": fn + fp,
        "avoidable_regret_rate_overall": _ratio(fn + fp, count),
        "avoidable_regret_rate_decisive": _ratio(fn + fp, decisive),
        "missed_needed_vision_count": fn,
        "wrongly_used_vision_count": fp,
    }
    for field in ("retrieval_page_hit_3", "visual_page_hit_1"):
        available = [row[field] for row in group if row[field] is not None]
        result[f"{field}_count"] = sum(available)
        result[f"{field}_available_count"] = len(available)
        result[f"{field}_rate"] = _ratio(sum(available), len(available))
    return result


def summarize_utility(cases: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Return overall, all four alignment categories, and all four answer types."""
    groups = [("overall", "overall", list(cases))]
    groups.extend(
        ("alignment_category", category, [r for r in cases if r["alignment_category"] == category])
        for category in ALIGNMENT_OUTCOMES
    )
    groups.extend(
        ("answer_type", answer_type, [r for r in cases if r["answer_type"] == answer_type])
        for answer_type in ANSWER_TYPES
    )
    summaries = []
    for group_type, name, group in groups:
        summary = {"group_type": group_type, "group": name, **_summarize(group)}
        # Retrospective correctness-based efficiency preference, not runtime oracle knowledge.
        summary["unnecessary_vision_count"] = (
            summary["ra_visual_required_count"] if name == "BOTH_CORRECT" else None
        )
        summary["unnecessary_vision_rate"] = (
            summary["ra_visual_required_rate"] if name == "BOTH_CORRECT" else None
        )
        summaries.append(summary)
    return summaries


def paired_correctness_rows(
    cases: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Return the question-level Forced Vision/RA paired correctness table."""
    labels = {
        (True, True): "BOTH_CORRECT",
        (True, False): "FV_ONLY_CORRECT",
        (False, True): "RA_ONLY_CORRECT",
        (False, False): "BOTH_INCORRECT",
    }
    return [
        {
            "id": row["id"],
            "answer_type": row["answer_type"],
            "forced_vision_correct": row["forced_vision_correct"],
            "retrieval_aware_correct": row["retrieval_aware_correct"],
            "paired_outcome": labels[
                (row["forced_vision_correct"], row["retrieval_aware_correct"])
            ],
        }
        for row in cases
    ]


def _percentile(values: Sequence[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1 - fraction) + ordered[upper] * fraction


def paired_correctness_statistics(
    cases: Sequence[Mapping[str, Any]],
    *,
    bootstrap_seed: int = 42,
    bootstrap_samples: int = 10_000,
) -> dict[str, Any]:
    """Compute deterministic paired correctness tests without external services."""
    if not cases:
        raise ValueError("paired correctness requires at least one case")
    if bootstrap_samples < 1:
        raise ValueError("bootstrap_samples must be positive")
    paired = paired_correctness_rows(cases)
    counts = {
        label: sum(row["paired_outcome"] == label for row in paired)
        for label in (
            "BOTH_CORRECT",
            "FV_ONLY_CORRECT",
            "RA_ONLY_CORRECT",
            "BOTH_INCORRECT",
        )
    }
    count = len(paired)
    fv_only = counts["FV_ONLY_CORRECT"]
    ra_only = counts["RA_ONLY_CORRECT"]
    discordant = fv_only + ra_only
    tail = min(fv_only, ra_only)
    exact_p = min(
        1.0,
        2 * sum(math.comb(discordant, value) for value in range(tail + 1)) / 2**discordant,
    )
    corrected_chi_square = (
        (max(0, abs(fv_only - ra_only) - 1) ** 2) / discordant
        if discordant
        else 0.0
    )
    corrected_p = math.erfc(math.sqrt(corrected_chi_square / 2))
    differences = [
        int(row["retrieval_aware_correct"]) - int(row["forced_vision_correct"])
        for row in paired
    ]
    rng = random.Random(bootstrap_seed)
    bootstrap = [
        sum(rng.choice(differences) for _ in range(count)) / count
        for _ in range(bootstrap_samples)
    ]
    point_difference = sum(differences) / count
    return {
        "sample_count": count,
        "both_correct": counts["BOTH_CORRECT"],
        "fv_only_correct": fv_only,
        "ra_only_correct": ra_only,
        "both_incorrect": counts["BOTH_INCORRECT"],
        "ra_minus_fv_correct_count": ra_only - fv_only,
        "ra_minus_fv_difference": point_difference,
        "ra_minus_fv_percentage_points": point_difference * 100,
        "mcnemar_discordant_count": discordant,
        "mcnemar_exact_binomial_p": exact_p,
        "mcnemar_continuity_corrected_chi_square": corrected_chi_square,
        "mcnemar_continuity_corrected_p": corrected_p,
        "bootstrap_unit": "question",
        "bootstrap_seed": bootstrap_seed,
        "bootstrap_samples": bootstrap_samples,
        "bootstrap_confidence_level": 0.95,
        "bootstrap_percentile_ci": [
            _percentile(bootstrap, 0.025),
            _percentile(bootstrap, 0.975),
        ],
        "bootstrap_percentile_ci_percentage_points": [
            _percentile(bootstrap, 0.025) * 100,
            _percentile(bootstrap, 0.975) * 100,
        ],
    }


def efficiency_statistics(cases: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Compare empirical RA efficiency with Forced Vision on identical questions."""
    if not cases:
        raise ValueError("efficiency statistics require at least one case")
    count = len(cases)
    fv_correct = sum(row["forced_vision_correct"] for row in cases)
    ra_correct = sum(row["retrieval_aware_correct"] for row in cases)
    ra_vision_calls = sum(row["predicted_route"] == "VISUAL_REQUIRED" for row in cases)
    vision_calls_saved = count - ra_vision_calls
    fv_latency = sum(row["forced_vision_latency_ms"] for row in cases) / count
    ra_latency = sum(row["total_latency_ms"] for row in cases) / count
    correctness_loss = fv_correct - ra_correct
    return {
        "sample_count": count,
        "forced_vision_correct_count": fv_correct,
        "retrieval_aware_correct_count": ra_correct,
        "correct_answer_difference": ra_correct - fv_correct,
        "correctness_percentage_point_difference": (ra_correct - fv_correct) / count * 100,
        "forced_vision_calls": count,
        "retrieval_aware_vision_calls": ra_vision_calls,
        "vision_calls_saved": vision_calls_saved,
        "vision_call_reduction_rate": vision_calls_saved / count,
        "forced_vision_avg_e2e_latency_ms": fv_latency,
        "retrieval_aware_avg_e2e_latency_ms": ra_latency,
        "latency_reduction_ms": fv_latency - ra_latency,
        "latency_reduction_rate": (fv_latency - ra_latency) / fv_latency,
        "vision_calls_saved_per_fewer_correct_answer": (
            vision_calls_saved / correctness_loss if correctness_loss > 0 else None
        ),
    }


def format_summary(summaries: Sequence[Mapping[str, Any]]) -> str:
    def percent(value: Any) -> str:
        return "N/A" if value is None else f"{value:.3%}"

    def latency(value: Any) -> str:
        return "N/A" if value is None else f"{value:.2f}"

    overall = summaries[0]
    lines = ["Retrospective route-selection utility (offline)", f"Samples: {overall['count']}"]
    for label, field in (
        ("Forced Text correctness", "forced_text_correctness"),
        ("Forced Vision correctness", "forced_vision_correctness"),
    ):
        lines.append(f"{label}: {percent(overall[field])}")
    lines.extend(
        [
            "",
            "Alignment routes and RA outcomes:",
            (
                "group | n | TEXT_ONLY n (%) | VISUAL_REQUIRED n (%) | "
                "RA correct | E2E ms | routing ms"
            ),
        ]
    )
    for row in summaries:
        if row["group_type"] == "answer_type":
            continue
        lines.append(
            f"{row['group']} | {row['count']} | "
            f"{row['ra_text_only_count']} ({percent(row['ra_text_only_rate'])}) | "
            f"{row['ra_visual_required_count']} ({percent(row['ra_visual_required_rate'])}) | "
            f"{percent(row['ra_correctness'])} | {latency(row['ra_avg_e2e_latency_ms'])} | "
            f"{latency(row['ra_avg_routing_latency_ms'])}"
        )
    lines.extend(
        [
            "",
            f"VISION_NEEDED vision-selection recall: {percent(overall['vision_selection_recall'])}",
            "TEXT_ONLY_BETTER text-selection recall: " + percent(overall["text_selection_recall"]),
            (
                f"decisive_route_accuracy: {percent(overall['decisive_route_accuracy'])} "
                f"({overall['decisive_correct_count']}/{overall['decisive_count']})"
            ),
            "Decisive confusion (optimal -> predicted):",
        ]
    )
    for label, field in (
        ("VISUAL_REQUIRED -> VISUAL_REQUIRED", "optimal_visual_predicted_visual_count"),
        ("VISUAL_REQUIRED -> TEXT_ONLY", "optimal_visual_predicted_text_count"),
        ("TEXT_ONLY -> TEXT_ONLY", "optimal_text_predicted_text_count"),
        ("TEXT_ONLY -> VISUAL_REQUIRED", "optimal_text_predicted_visual_count"),
    ):
        lines.append(f"  {label}: {overall[field]}")
    for field in (
        "decisive_vision_precision",
        "decisive_vision_recall",
        "decisive_vision_f1",
        "decisive_balanced_accuracy",
        "avoidable_regret_count",
        "avoidable_regret_rate_overall",
        "avoidable_regret_rate_decisive",
        "missed_needed_vision_count",
        "wrongly_used_vision_count",
    ):
        value = overall[field]
        lines.append(f"{field}: {value if field.endswith('_count') else percent(value)}")
    both_correct = next(row for row in summaries if row["group"] == "BOTH_CORRECT")
    lines.extend(
        [
            "",
            f"BOTH_CORRECT TEXT_ONLY selection rate: {percent(both_correct['ra_text_only_rate'])}",
            (
                "BOTH_CORRECT unnecessary Vision (retrospective efficiency preference): "
                f"{both_correct['unnecessary_vision_count']}/{both_correct['count']} "
                f"({percent(both_correct['unnecessary_vision_rate'])})"
            ),
        ]
    )
    both_wrong = next(row for row in summaries if row["group"] == "BOTH_WRONG")
    for field in ("retrieval_page_hit_3", "visual_page_hit_1"):
        lines.append(
            f"BOTH_WRONG {field}: {percent(both_wrong[f'{field}_rate'])} "
            f"({both_wrong[f'{field}_count']}/{both_wrong[f'{field}_available_count']} "
            "available rows)"
        )
    lines.extend(
        [
            "",
            "Answer-type decisive utility (answer_type is not route GT):",
            (
                "answer_type | decisive n | accuracy | VISION_NEEDED n | vision recall | "
                "TEXT_ONLY_BETTER n | text recall"
            ),
        ]
    )
    for row in summaries:
        if row["group_type"] == "answer_type":
            lines.append(
                f"{row['group']} | {row['decisive_count']} | "
                f"{percent(row['decisive_route_accuracy'])} | {row['vision_needed_count']} | "
                f"{percent(row['vision_selection_recall'])} | {row['text_only_better_count']} | "
                f"{percent(row['text_selection_recall'])}"
            )
    lines.extend(["", *NOTES])
    return "\n".join(lines)
