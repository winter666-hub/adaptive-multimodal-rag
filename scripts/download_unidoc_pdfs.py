"""Download and safely restore the PDF corpus for the UniDoc benchmark.

The official archives are downloaded from ``Salesforce/UniDoc-Bench`` and
normalized to ``<output-root>/<domain>/<domain>/<document_id>.pdf``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import tarfile
import tempfile
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

DATASET_ID = "Salesforce/UniDoc-Bench"
DOMAINS = (
    "commerce_manufacturing",
    "construction",
    "crm",
    "education",
    "energy",
    "finance",
    "healthcare",
    "legal",
)
ARCHIVES = {domain: f"{domain}_pdfs.tar.gz" for domain in DOMAINS}
EXPECTED_BENCHMARK_ROWS = 1_600
EXPECTED_REFERENCED_PDFS = 1_028
DEFAULT_OUTPUT_ROOT = Path("datasets/unidoc")
DEFAULT_BENCHMARK = Path("benchmarks/unidoc.jsonl")
DRIVE_PREFIX = re.compile(r"^[A-Za-z]:")


class UnsafeArchiveError(ValueError):
    """Raised when an archive member cannot be extracted safely."""


class ArchiveLayoutError(ValueError):
    """Raised when archive PDFs cannot be mapped unambiguously."""


@dataclass(frozen=True)
class BenchmarkValidation:
    total_rows: int
    references: tuple[str, ...]
    found: tuple[str, ...]
    missing: tuple[str, ...]


@dataclass(frozen=True)
class ExtractionResult:
    pdfs: tuple[Path, ...]
    apple_double_pdfs: int


def _parse_source_pdf(value: Any, *, line_number: int) -> PurePosixPath:
    if not isinstance(value, str):
        raise TypeError(f"benchmark line {line_number}: source_pdf must be a string")
    path = PurePosixPath(value.replace("\\", "/"))
    if (
        path.is_absolute()
        or len(path.parts) != 3
        or path.parts[0] != path.parts[1]
        or path.parts[0] not in DOMAINS
        or path.suffix.casefold() != ".pdf"
        or path.name.startswith("._")
        or any(part in {"", ".", ".."} or DRIVE_PREFIX.match(part) for part in path.parts)
    ):
        raise ValueError(
            f"benchmark line {line_number}: unsafe or unexpected source_pdf {value!r}"
        )
    return path


def load_benchmark_references(benchmark: Path) -> tuple[int, tuple[str, ...]]:
    references: set[str] = set()
    total_rows = 0
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
            source_pdf = _parse_source_pdf(row.get("source_pdf"), line_number=line_number)
            references.add(source_pdf.as_posix())
            total_rows += 1
    return total_rows, tuple(sorted(references))


def validate_benchmark(benchmark: Path, output_root: Path) -> BenchmarkValidation:
    total_rows, references = load_benchmark_references(benchmark)
    found: list[str] = []
    missing: list[str] = []
    for reference in references:
        source = PurePosixPath(reference)
        local_path = output_root.joinpath(*source.parts)
        (found if local_path.is_file() else missing).append(reference)
    return BenchmarkValidation(total_rows, references, tuple(found), tuple(missing))


def references_by_domain(references: tuple[str, ...]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for reference in references:
        path = PurePosixPath(reference)
        result[path.parts[0]].add(path.name)
    return dict(result)


def _safe_member_parts(member_name: str) -> tuple[str, ...]:
    normalized = member_name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (
        not normalized
        or normalized.startswith(("/", "//"))
        or path.is_absolute()
        or not path.parts
        or any(
            part in {"", ".", ".."} or DRIVE_PREFIX.match(part) or ":" in part
            for part in path.parts
        )
    ):
        raise UnsafeArchiveError(f"unsafe archive member path: {member_name!r}")
    return path.parts


def extract_archive_pdfs(archive: Path, destination: Path) -> ExtractionResult:
    """Extract regular PDFs only, rejecting traversal, links, and special files."""
    destination.mkdir(parents=True, exist_ok=True)
    destination_resolved = destination.resolve()
    extracted: list[Path] = []
    seen_paths: set[Path] = set()
    apple_double_pdfs = 0

    with tarfile.open(archive, mode="r:gz") as tar:
        for member in tar:
            parts = _safe_member_parts(member.name)
            target = destination.joinpath(*parts)
            resolved = target.resolve()
            if resolved != destination_resolved and destination_resolved not in resolved.parents:
                raise UnsafeArchiveError(f"archive member escapes destination: {member.name!r}")
            if member.issym() or member.islnk():
                raise UnsafeArchiveError(f"archive links are not allowed: {member.name!r}")
            if member.isdir():
                continue
            if not member.isfile():
                raise UnsafeArchiveError(f"archive special file is not allowed: {member.name!r}")
            if target.suffix.casefold() != ".pdf":
                continue
            if target.name.startswith("._"):
                apple_double_pdfs += 1
                continue
            if resolved in seen_paths:
                raise UnsafeArchiveError(f"duplicate archive member path: {member.name!r}")
            seen_paths.add(resolved)
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise UnsafeArchiveError(f"could not read archive member: {member.name!r}")
            with source, target.open("xb") as output:
                shutil.copyfileobj(source, output)
            extracted.append(target)

    return ExtractionResult(tuple(extracted), apple_double_pdfs)


def inspect_archive_layout(
    pdfs: tuple[Path, ...], staging_root: Path, domain: str, required_names: set[str]
) -> tuple[dict[str, Path], dict[str, int]]:
    """Inspect archive parents and create an unambiguous basename-to-PDF mapping."""
    if not pdfs:
        raise ArchiveLayoutError(f"{domain}: archive contains no normal PDF files")

    mapping: dict[str, Path] = {}
    parent_counts: Counter[str] = Counter()
    for pdf in pdfs:
        if pdf.name in mapping:
            first = mapping[pdf.name].relative_to(staging_root).as_posix()
            second = pdf.relative_to(staging_root).as_posix()
            raise ArchiveLayoutError(
                f"{domain}: duplicate PDF basename {pdf.name!r}: {first!r} and {second!r}"
            )
        mapping[pdf.name] = pdf
        parent = pdf.parent.relative_to(staging_root).as_posix() or "."
        parent_counts[parent] += 1

    missing_from_archive = sorted(required_names - mapping.keys())
    if missing_from_archive:
        preview = ", ".join(missing_from_archive[:10])
        suffix = f" (+{len(missing_from_archive) - 10} more)" if len(missing_from_archive) > 10 else ""
        raise ArchiveLayoutError(
            f"{domain}: archive is missing {len(missing_from_archive)} benchmark PDF(s): "
            f"{preview}{suffix}"
        )
    return mapping, dict(sorted(parent_counts.items()))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def install_pdfs(mapping: dict[str, Path], target_dir: Path) -> tuple[int, int]:
    """Install PDFs atomically without silently replacing conflicting local files."""
    target_dir.mkdir(parents=True, exist_ok=True)
    installed = 0
    reused = 0
    for name, source in sorted(mapping.items()):
        target = target_dir / name
        if target.exists():
            if not target.is_file() or target.stat().st_size != source.stat().st_size:
                raise FileExistsError(f"refusing to replace conflicting local path: {target}")
            # Hash only same-sized existing files; this keeps reruns safe without trusting size alone.
            if _sha256(target) != _sha256(source):
                raise FileExistsError(f"refusing to replace conflicting local PDF: {target}")
            reused += 1
            continue
        temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
        try:
            shutil.copy2(source, temporary)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        installed += 1
    return installed, reused


def _domain_is_complete(output_root: Path, domain: str, required_names: set[str]) -> bool:
    target = output_root / domain / domain
    return bool(required_names) and all((target / name).is_file() for name in required_names)


def _print_validation(summary: BenchmarkValidation, output_root: Path, *, dry_run: bool) -> None:
    print("\nFinal benchmark validation:" if not dry_run else "\nCurrent benchmark validation (dry-run):")
    print(f"  PDF root: {output_root.resolve()}")
    print(f"  Total benchmark rows: {summary.total_rows:,}")
    print(f"  Unique referenced PDFs: {len(summary.references):,}")
    print(f"  Referenced PDFs found: {len(summary.found):,}")
    print(f"  Missing referenced PDFs: {len(summary.missing):,}")
    print(
        f"  Expected rows ({EXPECTED_BENCHMARK_ROWS:,}): "
        f"{'PASS' if summary.total_rows == EXPECTED_BENCHMARK_ROWS else 'FAIL'}"
    )
    print(
        f"  Expected unique PDFs ({EXPECTED_REFERENCED_PDFS:,}): "
        f"{'PASS' if len(summary.references) == EXPECTED_REFERENCED_PDFS else 'FAIL'}"
    )
    if summary.missing:
        for reference in summary.missing[:20]:
            print(f"  Missing: {reference}")
        if len(summary.missing) > 20:
            print(f"  ... and {len(summary.missing) - 20:,} more")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--benchmark", type=Path, default=DEFAULT_BENCHMARK)
    parser.add_argument(
        "--archive-root",
        type=Path,
        help="Archive directory (default: <output-root>/_archives)",
    )
    parser.add_argument(
        "--delete-archives",
        action="store_true",
        help="Delete each local archive after its domain is successfully restored",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Inspect the benchmark and print planned paths without downloading or writing",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_root = args.output_root
    archive_root = args.archive_root or output_root / "_archives"
    total_rows, references = load_benchmark_references(args.benchmark)
    required_by_domain = references_by_domain(references)

    if total_rows != EXPECTED_BENCHMARK_ROWS or len(references) != EXPECTED_REFERENCED_PDFS:
        raise SystemExit(
            "Benchmark shape mismatch: expected "
            f"{EXPECTED_BENCHMARK_ROWS:,} rows and {EXPECTED_REFERENCED_PDFS:,} unique PDFs, "
            f"found {total_rows:,} and {len(references):,}"
        )

    print(f"Dataset: {DATASET_ID} (repo_type=dataset)")
    print(f"Output root: {output_root.resolve()}")
    print(f"Archive root: {archive_root.resolve()}")

    if args.dry_run:
        print("\nDry-run plan (no downloads, extraction, deletion, or directory creation):")
        for domain in DOMAINS:
            archive = archive_root / ARCHIVES[domain]
            target = output_root / domain / domain
            status = "reuse complete domain" if _domain_is_complete(
                output_root, domain, required_by_domain[domain]
            ) else "download and extract"
            print(f"  {domain}:")
            print(f"    archive: {archive.resolve()}")
            print(f"    extract target: {target.resolve()}")
            print(f"    benchmark references: {len(required_by_domain[domain]):,}")
            print(f"    action: {status}")
        summary = validate_benchmark(args.benchmark, output_root)
        _print_validation(summary, output_root, dry_run=True)
        return

    try:
        from huggingface_hub import hf_hub_download
    except ImportError as exc:
        raise SystemExit(
            "Missing dependency 'huggingface_hub'. Install it with: "
            "python -m pip install huggingface-hub"
        ) from exc

    output_root.mkdir(parents=True, exist_ok=True)
    archive_root.mkdir(parents=True, exist_ok=True)
    for domain in DOMAINS:
        archive_name = ARCHIVES[domain]
        target_dir = output_root / domain / domain
        required_names = required_by_domain[domain]
        print(f"\n[{domain}]")
        print(f"  Archive destination: {(archive_root / archive_name).resolve()}")
        print(f"  Extract destination: {target_dir.resolve()}")

        if _domain_is_complete(output_root, domain, required_names):
            print(f"  Reused complete domain ({len(required_names):,} referenced PDFs found).")
            existing_archive = archive_root / archive_name
            if args.delete_archives and existing_archive.is_file():
                existing_archive.unlink()
                print(f"  Deleted archive: {existing_archive.resolve()}")
            continue

        downloaded = Path(
            hf_hub_download(
                repo_id=DATASET_ID,
                filename=archive_name,
                repo_type="dataset",
                local_dir=str(archive_root),
            )
        )
        print(f"  Downloaded archive: {downloaded.resolve()}")

        with tempfile.TemporaryDirectory(prefix=f".extract-{domain}-", dir=output_root) as temp:
            staging_root = Path(temp)
            extraction = extract_archive_pdfs(downloaded, staging_root)
            mapping, parent_counts = inspect_archive_layout(
                extraction.pdfs, staging_root, domain, required_names
            )
            print("  Inspected archive PDF parent(s):")
            for parent, count in parent_counts.items():
                print(f"    {parent}: {count:,} normal PDF(s)")
            print(f"  Excluded AppleDouble PDFs: {extraction.apple_double_pdfs:,}")
            installed, reused = install_pdfs(mapping, target_dir)
            print(f"  Installed PDFs: {installed:,}; reused identical PDFs: {reused:,}")

        if args.delete_archives:
            downloaded.unlink()
            print(f"  Deleted archive: {downloaded.resolve()}")

    summary = validate_benchmark(args.benchmark, output_root)
    _print_validation(summary, output_root, dry_run=False)
    if summary.missing:
        raise SystemExit(f"Validation failed: {len(summary.missing):,} referenced PDFs are missing")


if __name__ == "__main__":
    main()
