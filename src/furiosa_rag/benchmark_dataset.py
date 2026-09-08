"""Shared JSONL schema loading for routing and E2E benchmarks."""

from __future__ import annotations

import json
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from furiosa_rag.router import QueryRoute

REQUIRED_FIELDS = ("id", "question")
OPTIONAL_FIELDS = (
    "category",
    "paper",
    "expected_route",
    "expected_page",
    "expected_pages",
    "expected_visual_evidence",
    "document_id",
    "source_pdf",
    "domain",
    "question_type",
    "answer_type",
    "gold_answer",
    "audit_sampling_seed",
    "audit_sampling_method",
)
LEGACY_CATEGORIES = {"text", "explicit_visual", "implicit_visual"}
STRING_METADATA_FIELDS = (
    "paper",
    "expected_visual_evidence",
    "document_id",
    "domain",
    "question_type",
    "answer_type",
    "gold_answer",
    "audit_sampling_method",
)


def _positive_int(value: Any, *, field: str, line_number: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"invalid {field} on line {line_number}: expected a positive integer")
    return value


def _expected_pages(row: dict[str, Any], line_number: int) -> list[int]:
    if "expected_pages" in row:
        pages = row["expected_pages"]
        if not isinstance(pages, list):
            raise ValueError(f"invalid expected_pages on line {line_number}: expected a list")
        return [
            _positive_int(page, field="expected_pages value", line_number=line_number)
            for page in pages
        ]
    if "expected_page" in row:
        return [_positive_int(row["expected_page"], field="expected_page", line_number=line_number)]
    return []


def _validate_source_pdf(value: Any, line_number: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"invalid source_pdf on line {line_number}: expected a non-empty string")
    if "\\" in value:
        raise ValueError(f"invalid source_pdf on line {line_number}: expected a POSIX path")
    path = PurePosixPath(value)
    if path.is_absolute() or PureWindowsPath(value).is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe source_pdf on line {line_number}: {value!r}")
    return value


def _normalize_row(row: Any, line_number: int) -> dict[str, Any]:
    if not isinstance(row, dict):
        raise TypeError(f"invalid benchmark row on line {line_number}: expected an object")
    missing = set(REQUIRED_FIELDS) - row.keys()
    if missing:
        raise ValueError(f"line {line_number} is missing fields: {', '.join(sorted(missing))}")
    for field in REQUIRED_FIELDS:
        if not isinstance(row[field], str) or not row[field].strip():
            raise ValueError(f"invalid {field} on line {line_number}")

    category = row.get("category")
    if category is not None and category not in LEGACY_CATEGORIES:
        raise ValueError(f"invalid category on line {line_number}")
    expected_route = row.get("expected_route")
    if expected_route is not None and expected_route not in {route.value for route in QueryRoute}:
        raise ValueError(f"invalid expected_route on line {line_number}")
    for field in STRING_METADATA_FIELDS:
        if field in row and not isinstance(row[field], str):
            raise ValueError(f"invalid {field} on line {line_number}: expected a string")

    normalized: dict[str, Any] = {field: row[field] for field in REQUIRED_FIELDS}
    normalized.update(
        (field, row[field])
        for field in OPTIONAL_FIELDS
        if field in row and field != "expected_pages"
    )
    normalized["expected_pages"] = _expected_pages(row, line_number)
    if "source_pdf" in normalized:
        normalized["source_pdf"] = _validate_source_pdf(normalized["source_pdf"], line_number)
    return normalized


def load_benchmark_jsonl(path: str | Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    with Path(path).open(encoding="utf-8") as source:
        for line_number, line in enumerate(source, start=1):
            if not line.strip():
                continue
            try:
                raw_row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON on line {line_number}: {exc.msg}") from exc
            row = _normalize_row(raw_row, line_number)
            if row["id"] in seen_ids:
                raise ValueError(f"duplicate id on line {line_number}: {row['id']}")
            seen_ids.add(row["id"])
            rows.append(row)
    if not rows:
        raise ValueError("benchmark dataset is empty")
    return rows
