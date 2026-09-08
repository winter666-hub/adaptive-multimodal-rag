"""Convert all UniDoc-Bench splits to one deterministic JSONL benchmark."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path, PurePosixPath
from typing import Any

DATASET_ID = "Salesforce/UniDoc-Bench"
DEFAULT_OUTPUT = Path("benchmarks/unidoc.jsonl")
ROUTE_BY_ANSWER_TYPE = {
    "text_only": "TEXT_ONLY",
    "image_only": "VISUAL_REQUIRED",
    "table_required": "VISUAL_REQUIRED",
    "image_plus_text_as_answer": "VISUAL_REQUIRED",
}
LONGDOC_PATH_FIELDS = ("longdoc_image_paths", "image_paths", "longdoc_paths")
GT_PATH_FIELDS = ("gt_image_paths", "ground_truth_image_paths", "gt_paths")
PAGE_PATTERN = re.compile(r"(?:^|[_-])page[_-]?(\d+)(?=\D*$)", re.IGNORECASE)


def flatten_paths(value: Any) -> list[str]:
    """Return path strings from scalar, sequence, or common mapping representations."""
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


def extract_document_id(path: str) -> str | None:
    """Extract the ID from ``.../<id>/<id>_page_0004.png`` metadata paths."""
    normalized = path.replace("\\", "/").split("?", 1)[0]
    parsed = PurePosixPath(normalized)
    match = PAGE_PATTERN.search(parsed.stem)
    filename_id = parsed.stem[: match.start()].rstrip("_-") if match else ""
    parent_id = parsed.parent.name
    if parent_id and parent_id.casefold() not in {"images", "pages", "pdfs"}:
        return parent_id
    return filename_id or None


def extract_document_ids(row: Mapping[str, Any]) -> list[str]:
    paths = paths_from_fields(row, GT_PATH_FIELDS)
    document_ids = list(
        dict.fromkeys(doc_id for path in paths if (doc_id := extract_document_id(path)))
    )
    if document_ids:
        return document_ids
    paths = paths_from_fields(row, LONGDOC_PATH_FIELDS)
    return list(dict.fromkeys(doc_id for path in paths if (doc_id := extract_document_id(path))))


def parse_page(path: str) -> int | None:
    normalized = path.replace("\\", "/").split("?", 1)[0]
    match = PAGE_PATTERN.search(PurePosixPath(normalized).stem)
    return int(match.group(1)) if match else None


def extract_expected_pages(row: Mapping[str, Any]) -> list[int]:
    """Parse every GT page while preserving its source order and duplicates."""
    paths = paths_from_fields(row, GT_PATH_FIELDS)
    pages = [parse_page(path) for path in paths]
    if not paths or any(page is None for page in pages):
        raise ValueError("gt_image_paths is empty or contains an unparseable page path")
    return [page for page in pages if page is not None]


def route_for_answer_type(answer_type: str) -> str:
    try:
        return ROUTE_BY_ANSWER_TYPE[answer_type]
    except KeyError as exc:
        raise ValueError(f"unknown answer_type: {answer_type!r}") from exc


def stable_id(domain: str, index: int) -> str:
    if index < 1:
        raise ValueError("stable index must be at least 1")
    return f"unidoc_{domain}_{index:04d}"


def make_source_pdf(domain: str, document_id: str) -> str:
    if not domain or not document_id:
        raise ValueError("domain and document_id must be non-empty")
    if any(
        part in {"", ".", ".."} or "/" in part or "\\" in part for part in (domain, document_id)
    ):
        raise ValueError("domain and document_id must be single safe path components")
    filename = f"{document_id}.pdf"
    if filename.startswith("._"):
        raise ValueError("AppleDouble metadata files are not valid source PDFs")
    return PurePosixPath(domain, domain, filename).as_posix()


def convert_row(row: Mapping[str, Any], split_name: str, index: int) -> dict[str, Any]:
    question = row.get("question")
    answer = row.get("answer")
    domain = row.get("domain")
    answer_type = row.get("answer_type")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("question must be a non-empty string")
    if not isinstance(answer, str) or not answer.strip():
        raise ValueError("answer must be a non-empty string")
    if not isinstance(domain, str) or not domain.strip():
        raise ValueError("domain must be a non-empty string")
    if domain != split_name:
        raise ValueError(f"row domain {domain!r} does not match split {split_name!r}")
    document_ids = extract_document_ids(row)
    if len(document_ids) != 1:
        raise ValueError(f"expected exactly one document_id, found {document_ids!r}")
    document_id = document_ids[0]
    expected_pages = extract_expected_pages(row)

    return {
        "id": stable_id(domain, index),
        "question": question,
        "gold_answer": answer,
        "domain": domain,
        "question_type": row.get("question_type"),
        "answer_type": answer_type,
        "expected_route": route_for_answer_type(answer_type),
        "document_id": document_id,
        "source_pdf": make_source_pdf(domain, document_id),
        "expected_pages": expected_pages,
    }


def convert_splits(dataset_dict: Mapping[str, Sequence[Mapping[str, Any]]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    for split_name in sorted(dataset_dict):
        for index, row in enumerate(dataset_dict[split_name], start=1):
            try:
                rows.append(convert_row(row, split_name, index))
            except ValueError as exc:
                errors.append(f"{split_name}[{index - 1}]: {exc}")
    if errors:
        preview = "\n".join(f"  - {error}" for error in errors[:20])
        remainder = f"\n  ... and {len(errors) - 20} more" if len(errors) > 20 else ""
        raise ValueError(
            f"UniDoc conversion failed for {len(errors)} row(s):\n{preview}{remainder}"
        )
    return rows


def is_valid_source_pdf(source_pdf: Any) -> bool:
    if not isinstance(source_pdf, str):
        return False
    path = PurePosixPath(source_pdf)
    return (
        not path.is_absolute()
        and len(path.parts) == 3
        and path.parts[0] == path.parts[1]
        and path.suffix == ".pdf"
        and not path.name.startswith("._")
        and ".." not in path.parts
    )


def validate_local_pdfs(rows: Sequence[Mapping[str, Any]], pdf_root: Path) -> None:
    source_pdfs = sorted({str(row["source_pdf"]) for row in rows})
    missing: list[tuple[str, Path]] = []
    mapped = 0
    for source_pdf in source_pdfs:
        expected_path = pdf_root / Path(*PurePosixPath(source_pdf).parts)
        expected_name = PurePosixPath(source_pdf).name
        valid_names = {
            path.name
            for path in expected_path.parent.glob("*.pdf")
            if path.is_file() and not path.name.startswith("._")
        }
        if expected_name in valid_names:
            mapped += 1
        else:
            missing.append((str(PurePosixPath(source_pdf).stem), expected_path))

    print("\nLocal PDF validation:")
    print(f"  PDF root: {pdf_root}")
    print(f"  Mapped PDF count: {mapped:,}")
    print(f"  Missing PDF count: {len(missing):,}")
    for document_id, path in missing:
        print(f"  Missing {document_id}: {path}")


def print_validation(rows: Sequence[Mapping[str, Any]]) -> None:
    ids = [str(row["id"]) for row in rows]
    duplicate_ids = sum(count - 1 for count in Counter(ids).values() if count > 1)
    print("\nConversion validation:")
    print(f"  Total rows: {len(rows):,}")
    print(f"  Unique IDs: {len(set(ids)):,}")
    print(f"  Duplicate IDs: {duplicate_ids:,}")
    for label, field in (
        ("Domain counts", "domain"),
        ("answer_type counts", "answer_type"),
        ("expected_route counts", "expected_route"),
    ):
        print(f"  {label}:")
        for value, count in sorted(Counter(row[field] for row in rows).items()):
            print(f"    {value}: {count:,}")
    documents = {(row["domain"], row["document_id"]) for row in rows}
    print(f"  Unique documents: {len(documents):,}")
    print(f"  Empty expected_pages: {sum(not row['expected_pages'] for row in rows):,}")
    print(
        f"  Invalid source_pdf: {sum(not is_valid_source_pdf(row['source_pdf']) for row in rows):,}"
    )
    unknown = sum(row["answer_type"] not in ROUTE_BY_ANSWER_TYPE for row in rows)
    print(f"  Unknown answer_type: {unknown:,}")


def write_jsonl(rows: Sequence[Mapping[str, Any]], output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="\n") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument(
        "--pdf-root",
        type=Path,
        help="Optional root containing <domain>/<domain>/<document_id>.pdf",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    try:
        from datasets import load_dataset
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'datasets'. Install it with: python -m pip install datasets"
        ) from exc

    dataset_dict = load_dataset(DATASET_ID)
    if not hasattr(dataset_dict, "items"):
        raise TypeError(f"Expected DatasetDict, got {type(dataset_dict).__name__}")
    rows = convert_splits(dataset_dict)
    print_validation(rows)
    if args.pdf_root is not None:
        validate_local_pdfs(rows, args.pdf_root)
    write_jsonl(rows, args.output)
    print(f"\nWrote {len(rows):,} rows to {args.output}")


if __name__ == "__main__":
    main()
