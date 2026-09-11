import csv
import hashlib
import json
from pathlib import Path

import pytest

from scripts.verify_unidoc_pdf_provenance import (
    ManifestEntry,
    build_manifest,
    compare_manifests,
    load_benchmark_references,
    read_manifest,
    serialize_manifest,
)


def _write_benchmark(path: Path, rows: list[dict[str, str]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")


def _write_manifest(path: Path, entries: list[ManifestEntry]) -> None:
    path.write_bytes(serialize_manifest(entries))


def test_manifest_is_deduplicated_sorted_and_deterministic(tmp_path: Path) -> None:
    benchmark = tmp_path / "benchmark.jsonl"
    _write_benchmark(
        benchmark,
        [
            {"id": "row-1", "source_pdf": "finance/finance/b.pdf"},
            {"id": "row-2", "source_pdf": "finance/finance/a.pdf"},
            {"id": "row-3", "source_pdf": "finance/finance/b.pdf"},
        ],
    )
    root = tmp_path / "unidoc"
    pdf_dir = root / "finance" / "finance"
    pdf_dir.mkdir(parents=True)
    (pdf_dir / "a.pdf").write_bytes(b"a")
    (pdf_dir / "b.pdf").write_bytes(b"bb")

    references = load_benchmark_references(benchmark)
    first, missing = build_manifest(references, root)
    second, second_missing = build_manifest(references, root)

    assert references.row_count == 3
    assert references.rows_by_path["finance/finance/b.pdf"] == ("row-1", "row-3")
    assert missing == second_missing == ()
    assert first == second
    assert [entry.relative_path for entry in first] == [
        "finance/finance/a.pdf",
        "finance/finance/b.pdf",
    ]
    assert first[0].sha256 == hashlib.sha256(b"a").hexdigest()
    assert first[0].size_bytes == 1
    assert serialize_manifest(first) == serialize_manifest(second)


def test_build_manifest_reports_missing_pdf(tmp_path: Path) -> None:
    benchmark = tmp_path / "benchmark.jsonl"
    _write_benchmark(
        benchmark, [{"id": "row-1", "source_pdf": "legal/legal/missing.pdf"}]
    )

    entries, missing = build_manifest(load_benchmark_references(benchmark), tmp_path)

    assert entries == ()
    assert missing == ("legal/legal/missing.pdf",)


@pytest.mark.parametrize(
    "source_pdf", ["../escape.pdf", "/absolute.pdf", "C:/absolute.pdf", "legal\\x.pdf"]
)
def test_rejects_unsafe_or_non_posix_source_pdf(tmp_path: Path, source_pdf: str) -> None:
    benchmark = tmp_path / "benchmark.jsonl"
    _write_benchmark(benchmark, [{"id": "row-1", "source_pdf": source_pdf}])

    with pytest.raises(ValueError):
        load_benchmark_references(benchmark)


def test_compare_detects_hash_size_missing_and_extra(tmp_path: Path) -> None:
    digest_a = "a" * 64
    digest_b = "b" * 64
    home = tmp_path / "home.csv"
    school = tmp_path / "school.csv"
    _write_manifest(
        home,
        [
            ManifestEntry("finance/finance/different.pdf", digest_a, 10),
            ManifestEntry("finance/finance/home-only.pdf", digest_a, 20),
            ManifestEntry("finance/finance/same.pdf", digest_a, 30),
        ],
    )
    _write_manifest(
        school,
        [
            ManifestEntry("finance/finance/different.pdf", digest_b, 11),
            ManifestEntry("finance/finance/same.pdf", digest_a, 30),
            ManifestEntry("finance/finance/school-only.pdf", digest_a, 20),
        ],
    )

    comparison = compare_manifests(home, school)

    assert comparison.missing_paths == ("finance/finance/home-only.pdf",)
    assert comparison.extra_paths == ("finance/finance/school-only.pdf",)
    assert comparison.hash_mismatches == ("finance/finance/different.pdf",)
    assert comparison.size_mismatches == ("finance/finance/different.pdf",)
    assert comparison.mismatched_paths == (
        "finance/finance/different.pdf",
        "finance/finance/home-only.pdf",
        "finance/finance/school-only.pdf",
    )
    assert not comparison.manifest_digests_match
    assert not comparison.is_match


def test_read_manifest_rejects_nonlexical_order(tmp_path: Path) -> None:
    manifest = tmp_path / "manifest.csv"
    with manifest.open("w", encoding="utf-8", newline="") as file:
        writer = csv.writer(file, lineterminator="\n")
        writer.writerow(("relative_path", "sha256", "size_bytes"))
        writer.writerow(("legal/legal/b.pdf", "a" * 64, "1"))
        writer.writerow(("legal/legal/a.pdf", "b" * 64, "2"))

    with pytest.raises(ValueError, match="lexical"):
        read_manifest(manifest)
