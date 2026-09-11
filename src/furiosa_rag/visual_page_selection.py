"""Offline diagnostic for page selection from stored reranked evidence."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from furiosa_rag.route_gt_audit import ANSWER_TYPES

TOP_N = 3
REQUIRED_RUN_FIELDS = {
    "query_id",
    "answer_type",
    "route",
    "selected_page",
    "page_hit_1",
    "retrieval_page_hit_3",
    "expected_page",
    "expected_pages",
    "sources",
}
OUTPUT_FIELDS = (
    "query_id",
    "answer_type",
    "current_page",
    "baseline_page",
    "candidate_pages",
    "gt_pages",
    "baseline_hit",
    "candidate_page_recall_3",
    "candidate_a_page",
    "candidate_a_hit",
    "candidate_b_page",
    "candidate_b_hit",
    "eligible_visual_subset",
    "both_wrong_diagnostic",
)


@dataclass(frozen=True, slots=True)
class Evidence:
    page: int
    rerank_score: float
    retrieval_score: float
    rank: int


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as input_file:
        reader = csv.DictReader(input_file)
        if reader.fieldnames is None:
            raise ValueError(f"{path}: missing CSV header")
        return list(reader)


def _index(rows: Iterable[Mapping[str, str]], *, source: str) -> dict[str, Mapping[str, str]]:
    indexed: dict[str, Mapping[str, str]] = {}
    for row in rows:
        query_id = row.get("query_id", "")
        if not query_id:
            raise ValueError(f"{source}: missing query_id")
        if query_id in indexed:
            raise ValueError(f"{source}: duplicate query_id {query_id!r}")
        indexed[query_id] = row
    return indexed


def _parse_bool(value: str, *, field: str) -> bool:
    if value == "True":
        return True
    if value == "False":
        return False
    raise ValueError(f"{field}: expected True or False, got {value!r}")


def _parse_optional_bool(value: str, *, field: str) -> bool | None:
    return None if value == "" else _parse_bool(value, field=field)


def _parse_optional_page(value: str, *, field: str) -> int | None:
    if value == "":
        return None
    try:
        page = int(value)
    except ValueError as exc:
        raise ValueError(f"{field}: invalid page {value!r}") from exc
    if page <= 0:
        raise ValueError(f"{field}: page must be positive")
    return page


def _parse_gt_pages(row: Mapping[str, str]) -> list[int]:
    query_id = row["query_id"]
    raw_pages = row.get("expected_pages", "")
    if raw_pages:
        try:
            pages = json.loads(raw_pages)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{query_id}/expected_pages: invalid JSON") from exc
        if not isinstance(pages, list):
            raise ValueError(f"{query_id}/expected_pages: expected a list")
    else:
        page = _parse_optional_page(
            row.get("expected_page", ""), field=f"{query_id}/expected_page"
        )
        pages = [] if page is None else [page]
    if any(isinstance(page, bool) or not isinstance(page, int) or page <= 0 for page in pages):
        raise ValueError(f"{query_id}/expected_pages: pages must be positive integers")
    return list(dict.fromkeys(pages))


def _finite_score(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{field}: expected a numeric score")
    score = float(value)
    if not math.isfinite(score):
        raise ValueError(f"{field}: score must be finite")
    return score


def _parse_evidence(row: Mapping[str, str]) -> list[Evidence]:
    query_id = row["query_id"]
    try:
        sources = json.loads(row["sources"])
    except json.JSONDecodeError as exc:
        raise ValueError(f"{query_id}/sources: invalid JSON") from exc
    if not isinstance(sources, list) or not sources:
        raise ValueError(f"{query_id}/sources: expected a non-empty list")
    evidence = []
    for rank, source in enumerate(sources[:TOP_N], start=1):
        if not isinstance(source, dict):
            raise TypeError(f"{query_id}/sources/{rank}: expected an object")
        page = source.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page <= 0:
            raise ValueError(f"{query_id}/sources/{rank}/page: expected a positive integer")
        evidence.append(
            Evidence(
                page=page,
                rerank_score=_finite_score(
                    source.get("rerank_score"),
                    field=f"{query_id}/sources/{rank}/rerank_score",
                ),
                retrieval_score=_finite_score(
                    source.get("retrieval_score"),
                    field=f"{query_id}/sources/{rank}/retrieval_score",
                ),
                rank=rank,
            )
        )
    return evidence


def candidate_pages(evidence: Sequence[Evidence]) -> list[int]:
    """Return unique page candidates in stored rerank order."""
    return list(dict.fromkeys(item.page for item in evidence[:TOP_N]))


def _select_aggregated(
    evidence: Sequence[Evidence], *, include_retrieval: bool
) -> int:
    grouped: dict[int, list[Evidence]] = defaultdict(list)
    for item in evidence[:TOP_N]:
        grouped[item.page].append(item)
    if not grouped:
        raise ValueError("cannot select a page without evidence")

    def selection_key(group: tuple[int, list[Evidence]]) -> tuple[float, float, int]:
        _, items = group
        total = sum(
            item.rerank_score + (item.retrieval_score if include_retrieval else 0.0)
            for item in items
        )
        best_rerank = max(item.rerank_score for item in items)
        best_rank = min(item.rank for item in items)
        return total, best_rerank, -best_rank

    return max(grouped.items(), key=selection_key)[0]


def select_candidate_a(evidence: Sequence[Evidence]) -> int:
    """Select by summed rerank score per page."""
    return _select_aggregated(evidence, include_retrieval=False)


def select_candidate_b(evidence: Sequence[Evidence]) -> int:
    """Select by the parameter-free sum of rerank and retrieval scores per page."""
    return _select_aggregated(evidence, include_retrieval=True)


def build_diagnostic_rows(
    run_rows: Sequence[Mapping[str, str]], alignment_rows: Sequence[Mapping[str, str]]
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Build the primary eligible set plus literal additional diagnostic rows."""
    if not run_rows:
        raise ValueError("retrieval-aware results are empty")
    missing_fields = REQUIRED_RUN_FIELDS - set(run_rows[0])
    if missing_fields:
        raise ValueError(f"retrieval-aware results missing fields: {sorted(missing_fields)}")
    alignment = _index(alignment_rows, source="alignment")
    run_ids = {row["query_id"] for row in run_rows}
    if run_ids != set(alignment):
        raise ValueError("retrieval-aware results and alignment query IDs must match exactly")

    output: list[dict[str, Any]] = []
    visual_reproduction_checks = 0
    stored_page_hit_checks = 0
    all_routes_diagnostic_count = 0
    all_routes_diagnostic_text_only_count = 0
    for row in run_rows:
        query_id = row["query_id"]
        gt_pages = _parse_gt_pages(row)
        evidence = _parse_evidence(row)
        top1_page = evidence[0].page
        current_page = _parse_optional_page(
            row.get("selected_page", ""), field=f"{query_id}/selected_page"
        )
        stored_page_hit = _parse_optional_bool(
            row["page_hit_1"], field=f"{query_id}/page_hit_1"
        )
        stored_recall = _parse_bool(
            row["retrieval_page_hit_3"], field=f"{query_id}/retrieval_page_hit_3"
        )
        pages = candidate_pages(evidence)
        candidate_recall = bool(set(pages).intersection(gt_pages))
        if candidate_recall != stored_recall:
            raise ValueError(f"{query_id}: candidate page recall disagrees with stored Hit@3")

        is_all_routes_diagnostic = (
            alignment[query_id].get("outcome") == "BOTH_WRONG"
            and stored_recall
            and bool(gt_pages)
            and stored_page_hit is False
        )
        if is_all_routes_diagnostic:
            all_routes_diagnostic_count += 1
            if row["route"] != "VISUAL_REQUIRED":
                all_routes_diagnostic_text_only_count += 1

        is_eligible = row["route"] == "VISUAL_REQUIRED" and bool(gt_pages)
        if is_eligible:
            visual_reproduction_checks += 1
            if current_page != top1_page:
                raise ValueError(
                    f"{query_id}: selected_page {current_page!r} "
                    f"!= rank-1 source page {top1_page}"
                )
        if not is_eligible and not is_all_routes_diagnostic:
            continue
        gt_page_set = set(gt_pages)
        baseline_hit = top1_page in gt_page_set
        if is_eligible and stored_page_hit is not None:
            stored_page_hit_checks += 1
        if is_eligible and stored_page_hit is not None and baseline_hit != stored_page_hit:
            raise ValueError(f"{query_id}: reproduced baseline disagrees with stored page_hit_1")
        candidate_a_page = select_candidate_a(evidence)
        candidate_b_page = select_candidate_b(evidence)
        output.append(
            {
                "query_id": query_id,
                "answer_type": row["answer_type"],
                "current_page": current_page,
                "baseline_page": top1_page,
                "candidate_pages": pages,
                "gt_pages": gt_pages,
                "baseline_hit": baseline_hit,
                "candidate_page_recall_3": candidate_recall,
                "candidate_a_page": candidate_a_page,
                "candidate_a_hit": candidate_a_page in gt_page_set,
                "candidate_b_page": candidate_b_page,
                "candidate_b_hit": candidate_b_page in gt_page_set,
                "eligible_visual_subset": is_eligible,
                "both_wrong_diagnostic": is_all_routes_diagnostic,
            }
        )

    return output, {
        "visual_top1_reproduction_checks": visual_reproduction_checks,
        "visual_top1_reproduction_mismatches": 0,
        "eligible_stored_page_hit_checks": stored_page_hit_checks,
        "eligible_stored_page_hit_mismatches": 0,
        "all_routes_diagnostic_predicate_count": all_routes_diagnostic_count,
        "all_routes_diagnostic_text_only_count": all_routes_diagnostic_text_only_count,
    }


def _safe_relative_improvement(candidate_rate: float, baseline_rate: float) -> float | None:
    return (candidate_rate - baseline_rate) / baseline_rate if baseline_rate else None


def summarize_rows(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    baseline_hits = sum(bool(row["baseline_hit"]) for row in rows)
    recall_hits = sum(bool(row["candidate_page_recall_3"]) for row in rows)
    baseline_rate = baseline_hits / n if n else 0.0
    summary: dict[str, Any] = {
        "n": n,
        "candidate_page_recall_3": {
            "hits": recall_hits,
            "rate": recall_hits / n if n else 0.0,
        },
        "baseline": {"hits": baseline_hits, "rate": baseline_rate},
    }
    for name, field in (
        ("candidate_a", "candidate_a_hit"),
        ("candidate_b", "candidate_b_hit"),
    ):
        hits = sum(bool(row[field]) for row in rows)
        rate = hits / n if n else 0.0
        summary[name] = {
            "hits": hits,
            "rate": rate,
            "absolute_improvement_hits": hits - baseline_hits,
            "percentage_point_improvement": (rate - baseline_rate) * 100,
            "relative_improvement": _safe_relative_improvement(rate, baseline_rate),
        }
    return summary


def build_summary(rows: Sequence[Mapping[str, Any]], checks: Mapping[str, int]) -> dict[str, Any]:
    eligible = [row for row in rows if row["eligible_visual_subset"]]
    answer_type = {
        name: summarize_rows([row for row in eligible if row["answer_type"] == name])
        for name in ANSWER_TYPES
    }
    diagnostic = [row for row in rows if row["both_wrong_diagnostic"]]
    visual_diagnostic = [row for row in diagnostic if row["eligible_visual_subset"]]
    return {
        "experiment": "offline_visual_page_selection_diagnostic",
        "top_n": TOP_N,
        "selector_inputs": "stored top-3 reranked textual evidence only",
        "baseline_rule": "page of rank-1 stored reranked evidence",
        "candidate_a_rule": "sum rerank_score per page; tie: max rerank_score, then best rank",
        "candidate_b_rule": (
            "sum (rerank_score + retrieval_score) per page; "
            "tie: max rerank_score, then best rank"
        ),
        "eligible_scope": "route=VISUAL_REQUIRED and at least one GT visual page",
        "diagnostic_scope": (
            "BOTH_WRONG and stored retrieval_page_hit_3=True and a GT visual page "
            "and stored page_hit_1=False; includes routes that did not invoke Vision"
        ),
        "diagnostic_baseline_note": (
            "For non-visual-routed diagnostic rows, baseline_page is the hypothetical stored "
            "rank-1 page and current_page is null. The visual-routed subset isolates page selection."
        ),
        "overall_eligible_visual": summarize_rows(eligible),
        "by_answer_type": answer_type,
        "both_wrong_diagnostic": summarize_rows(diagnostic),
        "both_wrong_diagnostic_visual_routed": summarize_rows(visual_diagnostic),
        "validation_checks": dict(checks),
        "external_api_calls": 0,
        "new_inference_calls": 0,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as input_file:
        for block in iter(lambda: input_file.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_outputs(
    rows: Sequence[Mapping[str, Any]],
    summary: Mapping[str, Any],
    *,
    csv_path: Path,
    summary_path: Path,
) -> None:
    for path in (csv_path, summary_path):
        if path.exists():
            raise FileExistsError(f"refusing to overwrite existing output: {path}")
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    with csv_path.open("x", encoding="utf-8", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=OUTPUT_FIELDS)
        writer.writeheader()
        for source_row in rows:
            row = dict(source_row)
            row["candidate_pages"] = json.dumps(row["candidate_pages"], separators=(",", ":"))
            row["gt_pages"] = json.dumps(row["gt_pages"], separators=(",", ":"))
            writer.writerow(row)
    with summary_path.open("x", encoding="utf-8", newline="\n") as output_file:
        json.dump(summary, output_file, ensure_ascii=False, indent=2)
        output_file.write("\n")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--retrieval-aware",
        type=Path,
        default=Path("results/unidoc_full_retrieval_aware.csv"),
    )
    parser.add_argument(
        "--alignment",
        type=Path,
        default=Path("results/unidoc_full_alignment.csv"),
    )
    parser.add_argument(
        "--output-csv",
        type=Path,
        default=Path("results/visual_page_selection_diagnostic.csv"),
    )
    parser.add_argument(
        "--output-summary",
        type=Path,
        default=Path("results/visual_page_selection_summary.json"),
    )
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    run_rows = _read_csv(args.retrieval_aware)
    alignment_rows = _read_csv(args.alignment)
    rows, checks = build_diagnostic_rows(run_rows, alignment_rows)
    summary = build_summary(rows, checks)
    summary["inputs"] = {
        "retrieval_aware_csv": str(args.retrieval_aware),
        "retrieval_aware_sha256": _sha256(args.retrieval_aware),
        "alignment_csv": str(args.alignment),
        "alignment_sha256": _sha256(args.alignment),
    }
    write_outputs(rows, summary, csv_path=args.output_csv, summary_path=args.output_summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
