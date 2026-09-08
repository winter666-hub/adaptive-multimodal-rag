import json
from pathlib import Path

import pytest

from furiosa_rag.benchmark_dataset import load_benchmark_jsonl


def write_rows(path: Path, rows: list[dict[str, object]]) -> Path:
    path.write_text(
        "".join(json.dumps(row) + "\n" for row in rows),
        encoding="utf-8",
    )
    return path


def test_loads_legacy_ack_row_and_normalizes_empty_pages(tmp_path: Path) -> None:
    row = {"id": "A1", "question": "Why?", "category": "text"}
    assert load_benchmark_jsonl(write_rows(tmp_path / "ack.jsonl", [row])) == [
        {**row, "expected_pages": []}
    ]


def test_loads_unidoc_row_without_category_and_preserves_metadata(tmp_path: Path) -> None:
    row = {
        "id": "unidoc_finance_0001",
        "question": "What is shown?",
        "gold_answer": "A chart.",
        "domain": "finance",
        "question_type": "factual_retrieval",
        "answer_type": "image_only",
        "expected_route": "VISUAL_REQUIRED",
        "document_id": "0002128",
        "source_pdf": "finance/finance/0002128.pdf",
        "expected_pages": [4, 7],
        "ignored_extra": {"not": "serialized"},
    }
    loaded = load_benchmark_jsonl(write_rows(tmp_path / "unidoc.jsonl", [row]))[0]
    assert loaded == {key: value for key, value in row.items() if key != "ignored_extra"}


def test_normalizes_expected_page_but_preserves_legacy_field(tmp_path: Path) -> None:
    row = {"id": "A1", "question": "Why?", "expected_page": 3}
    loaded = load_benchmark_jsonl(write_rows(tmp_path / "one.jsonl", [row]))[0]
    assert loaded["expected_page"] == 3
    assert loaded["expected_pages"] == [3]


def test_preserves_multiple_expected_pages(tmp_path: Path) -> None:
    row = {"id": "A1", "question": "Why?", "expected_pages": [5, 2, 5]}
    loaded = load_benchmark_jsonl(write_rows(tmp_path / "many.jsonl", [row]))[0]
    assert loaded["expected_pages"] == [5, 2, 5]


def test_rejects_duplicate_ids(tmp_path: Path) -> None:
    rows = [{"id": "A1", "question": "One"}, {"id": "A1", "question": "Two"}]
    with pytest.raises(ValueError, match="duplicate id"):
        load_benchmark_jsonl(write_rows(tmp_path / "duplicate.jsonl", rows))


def test_rejects_invalid_expected_route(tmp_path: Path) -> None:
    row = {"id": "A1", "question": "Why?", "expected_route": "UNKNOWN"}
    with pytest.raises(ValueError, match="invalid expected_route"):
        load_benchmark_jsonl(write_rows(tmp_path / "route.jsonl", [row]))


@pytest.mark.parametrize("pages", ["4", [0], [True], ["4"]])
def test_rejects_invalid_expected_pages(tmp_path: Path, pages: object) -> None:
    row = {"id": "A1", "question": "Why?", "expected_pages": pages}
    with pytest.raises(ValueError, match="invalid expected_pages"):
        load_benchmark_jsonl(write_rows(tmp_path / "pages.jsonl", [row]))


@pytest.mark.parametrize(
    "source_pdf",
    ("/finance/file.pdf", "C:/finance/file.pdf", "finance/../legal/file.pdf"),
)
def test_rejects_absolute_or_traversing_source_pdf(tmp_path: Path, source_pdf: str) -> None:
    row = {"id": "A1", "question": "Why?", "source_pdf": source_pdf}
    with pytest.raises(ValueError, match="unsafe source_pdf"):
        load_benchmark_jsonl(write_rows(tmp_path / "source.jsonl", [row]))
