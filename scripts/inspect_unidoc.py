"""Inspect all UniDoc-Bench domain-split metadata without downloading PDFs."""

from __future__ import annotations

import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

DATASET_ID = "Salesforce/UniDoc-Bench"
FINANCE_PDF_DIRECTORY = Path(__file__).resolve().parents[1] / "datasets/unidoc/finance/finance"
ROUTE_BY_ANSWER_TYPE = {
    "text_only": "TEXT_ONLY",
    "image_only": "VISUAL_REQUIRED",
    "table_required": "VISUAL_REQUIRED",
    "image_plus_text_as_answer": "VISUAL_REQUIRED",
}
QUESTION_FIELDS = ("question", "rewritten_question_obscured")
ANSWER_FIELDS = ("answer", "complete_answer")
LONGDOC_PATH_FIELDS = (
    "longdoc_image_paths",
    "image_paths",
    "longdoc_paths",
    "document_image_paths",
)
GT_PATH_FIELDS = ("gt_image_paths", "ground_truth_image_paths", "gt_paths")
PAGE_PATTERN = re.compile(r"(?:^|[_-])page[_-]?(\d+)(?=\D*$)", re.IGNORECASE)


def first_present(row: Mapping[str, Any], names: Iterable[str]) -> tuple[str | None, Any]:
    """Return the first present field, retaining false-y values for validation."""
    for name in names:
        if name in row:
            return name, row[name]
    return None, None


def flatten_paths(value: Any) -> list[str]:
    """Extract path-like strings from common scalar/list/dict metadata shapes."""
    if value is None:
        return []
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text[0] in "[{":
            try:
                return flatten_paths(json.loads(text))
            except (json.JSONDecodeError, TypeError):
                pass
        return [text]
    if isinstance(value, Mapping):
        preferred = ("path", "file", "filename", "image_path", "source")
        paths = [path for key in preferred if key in value for path in flatten_paths(value[key])]
        if paths:
            return paths
        return [path for item in value.values() for path in flatten_paths(item)]
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        return [path for item in value for path in flatten_paths(item)]
    return []


def paths_from_fields(row: Mapping[str, Any], fields: Iterable[str]) -> list[str]:
    return [path for field in fields if field in row for path in flatten_paths(row[field])]


def parse_document_id(path: str) -> str | None:
    """Infer a document id from ``.../<id>/<id>_page_0001.png``-style paths."""
    normalized = path.replace("\\", "/").split("?", 1)[0]
    parsed = PurePosixPath(normalized)
    match = PAGE_PATTERN.search(parsed.stem)
    filename_id = parsed.stem[: match.start()].rstrip("_-") if match else ""
    parent_id = parsed.parent.name
    if parent_id and parent_id.lower() not in {"images", "pages", "pdfs"}:
        return parent_id
    return filename_id or None


def parse_page(path: str) -> int | None:
    normalized = path.replace("\\", "/").split("?", 1)[0]
    match = PAGE_PATTERN.search(PurePosixPath(normalized).stem)
    return int(match.group(1)) if match else None


def document_ids_from_row(row: Mapping[str, Any]) -> set[str]:
    longdoc_paths = paths_from_fields(row, LONGDOC_PATH_FIELDS)
    # GT paths are also valid path metadata if a schema version omits longdoc paths.
    candidate_paths = longdoc_paths or paths_from_fields(row, GT_PATH_FIELDS)
    return {doc_id for path in candidate_paths if (doc_id := parse_document_id(path))}


def print_counts(label: str, values: Counter[Any]) -> None:
    print(f"\n{label}:")
    for value, count in sorted(values.items(), key=lambda item: (-item[1], str(item[0]))):
        print(f"  {value!s}: {count}")


def main() -> None:
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'datasets'. Install it with: python -m pip install datasets"
        ) from exc

    # UniDoc-Bench stores the QA table as Parquet metadata. The large PDF archives are
    # separate repository files and are not fetched by this call.
    dataset_dict = load_dataset(DATASET_ID)
    if not hasattr(dataset_dict, "items"):
        raise TypeError(
            f"Expected a DatasetDict from {DATASET_ID}, got {type(dataset_dict).__name__}"
        )

    splits = list(dataset_dict.items())
    if not splits:
        raise ValueError(f"No splits were provided by {DATASET_ID}")

    print(f"Dataset: {DATASET_ID}")
    print("Splits:")
    for split_name, split_dataset in splits:
        print(f"  {split_name}: {len(split_dataset):,} rows")

    total_rows = sum(len(split_dataset) for _, split_dataset in splits)
    all_columns = list(
        dict.fromkeys(
            column for _, split_dataset in splits for column in split_dataset.column_names
        )
    )
    print(f"Total rows: {total_rows:,}")
    print(f"Column names: {all_columns}")

    reference_name, reference_dataset = splits[0]
    schema_mismatches: list[str] = []
    for split_name, split_dataset in splits[1:]:
        if split_dataset.features != reference_dataset.features:
            schema_mismatches.append(split_name)
    if schema_mismatches:
        print(
            f"WARNING: Split schemas differ from {reference_name!r}: {', '.join(schema_mismatches)}"
        )
        for split_name, split_dataset in splits:
            print(f"  {split_name} schema: {split_dataset.features}")
    else:
        print(f"Schema consistency: OK (all splits match {reference_name!r})")

    answer_type_counts: Counter[Any] = Counter()
    question_type_counts: Counter[Any] = Counter()
    domain_counts: Counter[Any] = Counter()
    route_counts: Counter[str] = Counter()
    unique_documents: set[tuple[str, str]] = set()
    failures = Counter()
    samples: list[dict[str, Any]] = []
    finance_qa_rows = 0
    finance_document_ids: set[str] = set()

    for split_name, split_dataset in splits:
        is_finance_split = split_name.casefold() == "finance"
        if is_finance_split:
            finance_qa_rows += len(split_dataset)
        for row in split_dataset:
            inspect_row(
                row,
                answer_type_counts,
                question_type_counts,
                domain_counts,
                route_counts,
                unique_documents,
                failures,
                samples,
            )
            if is_finance_split:
                finance_document_ids.update(document_ids_from_row(row))

    print_counts("answer_type counts", answer_type_counts)
    print_counts("question_type counts", question_type_counts)
    print_counts("domain counts", domain_counts)
    print_counts("Expected route distribution", route_counts)
    print(f"\nUnique documents (domain, document_id): {len(unique_documents):,}")

    print(f"\nValidation (all {total_rows:,} rows):")
    print(f"  Route mapping failures: {failures['route_mapping']}")
    print(f"  document_id extraction failures: {failures['document_id']}")
    print(f"  GT page parsing failures: {failures['gt_page']}")
    print(f"  Empty questions: {failures['empty_question']}")
    print(f"  Empty answers: {failures['empty_answer']}")

    validate_finance_pdfs(finance_qa_rows, finance_document_ids)

    print("\nSample rows:")
    for index, sample in enumerate(samples, start=1):
        print(f"\n--- Sample {index} ---")
        for key, value in sample.items():
            print(f"{key}: {value}")


def validate_finance_pdfs(finance_qa_rows: int, document_ids: set[str]) -> None:
    print("\nFinance PDF mapping validation:")
    print(f"  PDF directory: {FINANCE_PDF_DIRECTORY}")
    print(f"  Finance QA rows: {finance_qa_rows:,}")
    print(f"  Finance unique document IDs: {len(document_ids):,}")

    actual_pdf_ids: set[str] = set()
    if FINANCE_PDF_DIRECTORY.is_dir():
        actual_pdf_ids = {
            path.stem
            for path in FINANCE_PDF_DIRECTORY.iterdir()
            if path.is_file()
            and path.suffix.casefold() == ".pdf"
            and not path.name.startswith("._")
        }
    else:
        print(f"  WARNING: Finance PDF directory does not exist: {FINANCE_PDF_DIRECTORY}")

    # Set membership enforces an exact metadata-ID-to-filename-stem match, even on
    # case-insensitive filesystems. AppleDouble files were excluded above.
    mapped_ids = document_ids & actual_pdf_ids
    missing_ids = document_ids - actual_pdf_ids
    print(f"  Mapped PDF count: {len(mapped_ids):,}")
    print(f"  Missing PDF count: {len(missing_ids):,}")
    if missing_ids:
        print("  Missing PDFs:")
        for document_id in sorted(missing_ids):
            print(f"    {document_id}: {FINANCE_PDF_DIRECTORY / f'{document_id}.pdf'}")


def inspect_row(
    row: Mapping[str, Any],
    answer_type_counts: Counter[Any],
    question_type_counts: Counter[Any],
    domain_counts: Counter[Any],
    route_counts: Counter[str],
    unique_documents: set[tuple[str, str]],
    failures: Counter[str],
    samples: list[dict[str, Any]],
) -> None:
    answer_type = row.get("answer_type")
    question_type = row.get("question_type")
    domain = row.get("domain")
    answer_type_counts[answer_type] += 1
    question_type_counts[question_type] += 1
    domain_counts[domain] += 1

    normalized_answer_type = answer_type.strip().lower() if isinstance(answer_type, str) else None
    route = ROUTE_BY_ANSWER_TYPE.get(normalized_answer_type)
    if route is None:
        failures["route_mapping"] += 1
    else:
        route_counts[route] += 1

    gt_paths = paths_from_fields(row, GT_PATH_FIELDS)
    document_ids = document_ids_from_row(row)
    if not document_ids:
        failures["document_id"] += 1
    elif isinstance(domain, str) and domain.strip():
        unique_documents.update((domain.strip(), doc_id) for doc_id in document_ids)
    else:
        # A document cannot form the requested (domain, document_id) identity.
        failures["document_id"] += 1

    parsed_pages = [parse_page(path) for path in gt_paths]
    if not gt_paths or any(page is None for page in parsed_pages):
        failures["gt_page"] += 1
    expected_pages = [page for page in parsed_pages if page is not None]

    _, question = first_present(row, QUESTION_FIELDS)
    _, answer = first_present(row, ANSWER_FIELDS)
    if not isinstance(question, str) or not question.strip():
        failures["empty_question"] += 1
    if not isinstance(answer, str) or not answer.strip():
        failures["empty_answer"] += 1

    if len(samples) < 3:
        samples.append(
            {
                "question": question,
                "answer_type": answer_type,
                "expected_route": route,
                "domain": domain,
                "document_id": min(document_ids) if document_ids else None,
                "expected_pages": expected_pages,
            }
        )


if __name__ == "__main__":
    main()
