from pathlib import PurePosixPath

import pytest

from scripts.prepare_unidoc import (
    ROUTE_BY_ANSWER_TYPE,
    convert_row,
    extract_document_id,
    extract_expected_pages,
    make_source_pdf,
    parse_page,
    route_for_answer_type,
    stable_id,
)


def sample_row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "question": "What is shown?",
        "answer": "A chart.",
        "gt_image_paths": [
            "/images/finance/0002128/0002128_page_0004.png",
            "/images/finance/0002128/0002128_page_0010.png",
        ],
        "longdoc_image_paths": ["/images/finance/0002128/0002128_page_0001.png"],
        "question_type": "factual_retrieval",
        "answer_type": "image_only",
        "domain": "finance",
    }
    row.update(overrides)
    return row


@pytest.mark.parametrize(
    ("answer_type", "expected"),
    ROUTE_BY_ANSWER_TYPE.items(),
)
def test_answer_type_route_mapping(answer_type: str, expected: str) -> None:
    assert route_for_answer_type(answer_type) == expected


def test_document_id_extraction() -> None:
    path = "/images/finance/0002128/0002128_page_0004.png"
    assert extract_document_id(path) == "0002128"


def test_page_parsing() -> None:
    assert parse_page("/images/0002128_page_0004.png") == 4


def test_multiple_pages_preserve_order() -> None:
    assert extract_expected_pages(sample_row()) == [4, 10]


def test_stable_id() -> None:
    assert stable_id("finance", 7) == "unidoc_finance_0007"
    assert stable_id("finance", 7) == stable_id("finance", 7)


def test_source_pdf_generation() -> None:
    source = make_source_pdf("finance", "0002128")
    assert source == "finance/finance/0002128.pdf"
    assert PurePosixPath(source).parts == ("finance", "finance", "0002128.pdf")


def test_convert_row() -> None:
    converted = convert_row(sample_row(), "finance", 1)
    assert converted["id"] == "unidoc_finance_0001"
    assert converted["expected_pages"] == [4, 10]
    assert converted["source_pdf"] == "finance/finance/0002128.pdf"


def test_unknown_answer_type_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown answer_type"):
        route_for_answer_type("future_unknown_type")
