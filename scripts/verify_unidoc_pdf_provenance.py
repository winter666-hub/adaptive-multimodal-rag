"""Create and compare deterministic SHA-256 manifests for UniDoc benchmark PDFs.

This utility only reads the benchmark and PDF bytes. It does not import or run any
benchmark, router, model, or judge code.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

EXPECTED_BENCHMARK_ROWS = 1_600
EXPECTED_UNIQUE_PDFS = 1_028
MANIFEST_FIELDS = ("relative_path", "sha256", "size_bytes")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    sha256: str
    size_bytes: int


@dataclass(frozen=True)
class BenchmarkReferences:
    row_count: int
    rows_by_path: dict[str, tuple[str, ...]]

    @property
    def relative_paths(self) -> tuple[str, ...]:
        return tuple(sorted(self.rows_by_path))


@dataclass(frozen=True)
class ManifestComparison:
    missing_paths: tuple[str, ...]
    extra_paths: tuple[str, ...]
    hash_mismatches: tuple[str, ...]
    size_mismatches: tuple[str, ...]
    manifest_digests_match: bool

    @property
    def mismatched_paths(self) -> tuple[str, ...]:
        return tuple(
            sorted(
                set(self.missing_paths)
                | set(self.extra_paths)
                | set(self.hash_mismatches)
                | set(self.size_mismatches)
            )
        )

    @property
    def is_match(self) -> bool:
        return not self.mismatched_paths and self.manifest_digests_match


def _normalize_relative_pdf(value: Any, *, line_number: int) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(f"benchmark line {line_number}: source_pdf must be a non-empty string")
    if "\\" in value:
        raise ValueError(f"benchmark line {line_number}: source_pdf must use POSIX separators")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or path.suffix.casefold() != ".pdf"
        or any(part in {"", ".", ".."} or DRIVE_PREFIX_RE.match(part) for part in path.parts)
    ):
        raise ValueError(f"benchmark line {line_number}: unsafe source_pdf {value!r}")
    return path.as_posix()


def load_benchmark_references(benchmark: Path) -> BenchmarkReferences:
    rows_by_path: dict[str, list[str]] = defaultdict(list)
    row_count = 0
    with benchmark.open(encoding="utf-8") as file:
        for line_number, line in enumerate(file, start=1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"benchmark line {line_number}: invalid JSON: {exc}") from exc
            if not isinstance(row, dict):
                raise TypeError(f"benchmark line {line_number}: expected a JSON object")
            relative_path = _normalize_relative_pdf(
                row.get("source_pdf"), line_number=line_number
            )
            row_id = row.get("id")
            if not isinstance(row_id, str) or not row_id:
                row_id = f"line:{line_number}"
            rows_by_path[relative_path].append(row_id)
            row_count += 1
    return BenchmarkReferences(
        row_count=row_count,
        rows_by_path={path: tuple(row_ids) for path, row_ids in rows_by_path.items()},
    )


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def build_manifest(
    references: BenchmarkReferences, dataset_root: Path
) -> tuple[tuple[ManifestEntry, ...], tuple[str, ...]]:
    root = dataset_root.resolve()
    entries: list[ManifestEntry] = []
    missing: list[str] = []
    for relative_path in references.relative_paths:
        pure_path = PurePosixPath(relative_path)
        pdf = root.joinpath(*pure_path.parts)
        if not pdf.is_file():
            missing.append(relative_path)
            continue
        entries.append(
            ManifestEntry(
                relative_path=relative_path,
                sha256=_sha256_file(pdf),
                size_bytes=pdf.stat().st_size,
            )
        )
    return tuple(entries), tuple(missing)


def serialize_manifest(entries: Iterable[ManifestEntry]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=MANIFEST_FIELDS, lineterminator="\n")
    writer.writeheader()
    previous: str | None = None
    for entry in entries:
        if previous is not None and entry.relative_path <= previous:
            raise ValueError("manifest entries must be unique and in lexical relative_path order")
        writer.writerow(
            {
                "relative_path": entry.relative_path,
                "sha256": entry.sha256,
                "size_bytes": entry.size_bytes,
            }
        )
        previous = entry.relative_path
    return output.getvalue().encode("utf-8")


def manifest_digest(manifest_bytes: bytes) -> str:
    return hashlib.sha256(manifest_bytes).hexdigest()


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as file:
            file.write(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def write_manifest(entries: tuple[ManifestEntry, ...], output: Path, digest_output: Path) -> str:
    content = serialize_manifest(entries)
    digest = manifest_digest(content)
    _atomic_write(output, content)
    _atomic_write(digest_output, f"{digest}  {output.name}\n".encode("ascii"))
    return digest


def read_manifest(path: Path) -> tuple[ManifestEntry, ...]:
    entries: list[ManifestEntry] = []
    with path.open(encoding="utf-8", newline="") as file:
        reader = csv.DictReader(file)
        if tuple(reader.fieldnames or ()) != MANIFEST_FIELDS:
            raise ValueError(f"{path}: expected CSV fields {MANIFEST_FIELDS}")
        for row_number, row in enumerate(reader, start=2):
            relative_path = _normalize_relative_pdf(
                row["relative_path"], line_number=row_number
            )
            sha256 = row["sha256"]
            if not SHA256_RE.fullmatch(sha256):
                raise ValueError(f"{path}:{row_number}: invalid SHA-256")
            try:
                size_bytes = int(row["size_bytes"])
            except ValueError as exc:
                raise ValueError(f"{path}:{row_number}: invalid size_bytes") from exc
            if size_bytes < 0:
                raise ValueError(f"{path}:{row_number}: size_bytes must be non-negative")
            entries.append(ManifestEntry(relative_path, sha256, size_bytes))
    # This also rejects duplicates and non-deterministic ordering.
    serialize_manifest(entries)
    return tuple(entries)


def compare_manifests(home_path: Path, school_path: Path) -> ManifestComparison:
    home_entries = read_manifest(home_path)
    school_entries = read_manifest(school_path)
    home = {entry.relative_path: entry for entry in home_entries}
    school = {entry.relative_path: entry for entry in school_entries}
    shared_paths = sorted(home.keys() & school.keys())
    return ManifestComparison(
        missing_paths=tuple(sorted(home.keys() - school.keys())),
        extra_paths=tuple(sorted(school.keys() - home.keys())),
        hash_mismatches=tuple(
            path for path in shared_paths if home[path].sha256 != school[path].sha256
        ),
        size_mismatches=tuple(
            path for path in shared_paths if home[path].size_bytes != school[path].size_bytes
        ),
        manifest_digests_match=(
            _sha256_file(home_path) == _sha256_file(school_path)
        ),
    )


def _validate_expected_shape(references: BenchmarkReferences) -> None:
    unique_count = len(references.rows_by_path)
    if references.row_count != EXPECTED_BENCHMARK_ROWS or unique_count != EXPECTED_UNIQUE_PDFS:
        raise SystemExit(
            "Benchmark shape mismatch: expected "
            f"{EXPECTED_BENCHMARK_ROWS:,} rows and {EXPECTED_UNIQUE_PDFS:,} unique PDFs; "
            f"found {references.row_count:,} rows and {unique_count:,} unique PDFs"
        )


def create_command(args: argparse.Namespace) -> int:
    references = load_benchmark_references(args.benchmark)
    _validate_expected_shape(references)
    entries, missing = build_manifest(references, args.dataset_root)
    print(f"benchmark rows: {references.row_count}")
    print(f"unique source PDFs: {len(references.rows_by_path)}")
    print(f"duplicate references removed: {references.row_count - len(references.rows_by_path)}")
    print(f"missing PDFs: {len(missing)}")
    if missing:
        for relative_path in missing:
            print(f"missing: {relative_path}")
        return 1

    content = serialize_manifest(entries)
    digest = manifest_digest(content)
    if args.verify_deterministic:
        second_entries, second_missing = build_manifest(references, args.dataset_root)
        second_content = serialize_manifest(second_entries)
        if second_missing or second_content != content:
            raise RuntimeError("deterministic regeneration failed")
        print("deterministic regeneration: PASS")

    written_digest = write_manifest(entries, args.output, args.digest_output)
    if written_digest != digest or _sha256_file(args.output) != digest:
        raise RuntimeError("written manifest digest verification failed")
    print(f"manifest: {args.output}")
    print(f"manifest digest file: {args.digest_output}")
    print(f"manifest SHA-256: {digest}")
    return 0


def compare_command(args: argparse.Namespace) -> int:
    references = load_benchmark_references(args.benchmark)
    _validate_expected_shape(references)
    comparison = compare_manifests(args.home, args.school)
    home_entries = read_manifest(args.home)
    school_entries = read_manifest(args.school)
    print(f"home paths: {len(home_entries)}")
    print(f"school paths: {len(school_entries)}")
    print(f"relative path sets identical: {not comparison.missing_paths and not comparison.extra_paths}")
    print(f"SHA-256 mismatch count: {len(comparison.hash_mismatches)}")
    print(f"size mismatch count: {len(comparison.size_mismatches)}")
    print(f"missing count (school vs home): {len(comparison.missing_paths)}")
    print(f"extra count (school vs home): {len(comparison.extra_paths)}")
    print(f"manifest digests identical: {comparison.manifest_digests_match}")
    print(f"mismatch count: {len(comparison.mismatched_paths)}")
    if comparison.is_match:
        print("MATCH")
        print(f"all {len(home_entries)} benchmark PDFs are byte-identical")
        return 0


    print("MISMATCH")
    print(f"{len(comparison.mismatched_paths)} PDFs differ")
    for relative_path in comparison.mismatched_paths:
        affected_rows = references.rows_by_path.get(relative_path, ())
        print(f"{relative_path}\trows={','.join(affected_rows) if affected_rows else '<none>'}")
    return 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser("create", help="create a manifest from local PDF bytes")
    create.add_argument("--benchmark", type=Path, default=Path("benchmarks/unidoc.jsonl"))
    create.add_argument("--dataset-root", type=Path, default=Path("datasets/unidoc"))
    create.add_argument("--output", type=Path, required=True)
    create.add_argument("--digest-output", type=Path, required=True)
    create.add_argument("--verify-deterministic", action="store_true")
    create.set_defaults(handler=create_command)

    compare = subparsers.add_parser("compare", help="compare home and school manifests")
    compare.add_argument("--benchmark", type=Path, default=Path("benchmarks/unidoc.jsonl"))
    compare.add_argument("--home", type=Path, required=True)
    compare.add_argument("--school", type=Path, required=True)
    compare.set_defaults(handler=compare_command)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    raise SystemExit(args.handler(args))


if __name__ == "__main__":
    main()
