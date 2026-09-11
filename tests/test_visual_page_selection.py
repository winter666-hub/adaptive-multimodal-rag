import csv
import json

import pytest

from furiosa_rag.visual_page_selection import (
    Evidence,
    build_diagnostic_rows,
    build_summary,
    candidate_pages,
    select_candidate_a,
    select_candidate_b,
    write_outputs,
)


def _evidence() -> list[Evidence]:
    return [
        Evidence(page=9, rerank_score=0.7, retrieval_score=0.1, rank=1),
        Evidence(page=7, rerank_score=0.6, retrieval_score=0.8, rank=2),
        Evidence(page=7, rerank_score=0.2, retrieval_score=0.7, rank=3),
    ]


def test_selectors_aggregate_duplicate_pages_deterministically() -> None:
    evidence = _evidence()

    assert candidate_pages(evidence) == [9, 7]
    assert select_candidate_a(evidence) == 7
    assert select_candidate_b(evidence) == 7


def test_candidate_a_tie_uses_max_score_then_rank() -> None:
    evidence = [
        Evidence(page=4, rerank_score=0.5, retrieval_score=0.2, rank=1),
        Evidence(page=5, rerank_score=0.25, retrieval_score=0.2, rank=2),
        Evidence(page=5, rerank_score=0.25, retrieval_score=0.2, rank=3),
    ]

    assert select_candidate_a(evidence) == 4


def test_candidate_b_can_use_stored_retrieval_score() -> None:
    evidence = [
        Evidence(page=4, rerank_score=0.8, retrieval_score=0.1, rank=1),
        Evidence(page=5, rerank_score=0.7, retrieval_score=0.9, rank=2),
    ]

    assert select_candidate_a(evidence) == 4
    assert select_candidate_b(evidence) == 5


def _run_row(query_id: str, *, page_hit: bool = False) -> dict[str, str]:
    return {
        "query_id": query_id,
        "answer_type": "image_only",
        "route": "VISUAL_REQUIRED",
        "selected_page": "9",
        "page_hit_1": str(page_hit),
        "retrieval_page_hit_3": "True",
        "expected_page": "",
        "expected_pages": "[7]",
        "sources": json.dumps(
            [
                {"page": 9, "rerank_score": 0.7, "retrieval_score": 0.1},
                {"page": 7, "rerank_score": 0.6, "retrieval_score": 0.8},
                {"page": 7, "rerank_score": 0.2, "retrieval_score": 0.7},
            ]
        ),
    }


def test_build_rows_reproduces_baseline_and_marks_nested_diagnostic() -> None:
    rows, checks = build_diagnostic_rows(
        [_run_row("q1")], [{"query_id": "q1", "outcome": "BOTH_WRONG"}]
    )

    assert rows == [
        {
            "query_id": "q1",
            "answer_type": "image_only",
            "current_page": 9,
            "baseline_page": 9,
            "candidate_pages": [9, 7],
            "gt_pages": [7],
            "baseline_hit": False,
            "candidate_page_recall_3": True,
            "candidate_a_page": 7,
            "candidate_a_hit": True,
            "candidate_b_page": 7,
            "candidate_b_hit": True,
            "eligible_visual_subset": True,
            "both_wrong_diagnostic": True,
        }
    ]
    assert checks["visual_top1_reproduction_checks"] == 1
    assert build_summary(rows, checks)["both_wrong_diagnostic"]["candidate_a"][
        "percentage_point_improvement"
    ] == 100.0


def test_build_rows_rejects_selected_page_mismatch() -> None:
    row = _run_row("q1")
    row["selected_page"] = "8"

    with pytest.raises(ValueError, match="rank-1 source page"):
        build_diagnostic_rows([row], [{"query_id": "q1", "outcome": "BOTH_WRONG"}])


def test_write_outputs_refuses_to_overwrite(tmp_path) -> None:
    csv_path = tmp_path / "diagnostic.csv"
    summary_path = tmp_path / "summary.json"
    row = {
        "query_id": "q1",
        "answer_type": "image_only",
        "current_page": 9,
        "baseline_page": 9,
        "candidate_pages": [9, 7],
        "gt_pages": [7],
        "baseline_hit": False,
        "candidate_page_recall_3": True,
        "candidate_a_page": 7,
        "candidate_a_hit": True,
        "candidate_b_page": 7,
        "candidate_b_hit": True,
        "eligible_visual_subset": True,
        "both_wrong_diagnostic": True,
    }
    write_outputs([row], {"n": 1}, csv_path=csv_path, summary_path=summary_path)

    with csv_path.open(encoding="utf-8", newline="") as input_file:
        written = list(csv.DictReader(input_file))
    assert written[0]["candidate_pages"] == "[9,7]"
    with pytest.raises(FileExistsError, match="refusing to overwrite"):
        write_outputs([row], {"n": 1}, csv_path=csv_path, summary_path=summary_path)
