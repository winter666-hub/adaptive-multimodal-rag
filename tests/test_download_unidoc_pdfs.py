import io
import json
import tarfile
from pathlib import Path

import pytest

from scripts.download_unidoc_pdfs import (
    ArchiveLayoutError,
    UnsafeArchiveError,
    extract_archive_pdfs,
    inspect_archive_layout,
    validate_benchmark,
)


def _write_tar(archive: Path, members: dict[str, bytes]) -> None:
    with tarfile.open(archive, "w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))


def test_extract_inspects_unexpected_prefix_and_excludes_appledouble(tmp_path: Path) -> None:
    archive = tmp_path / "finance_pdfs.tar.gz"
    _write_tar(
        archive,
        {
            "unexpected/prefix/finance/finance/0001.pdf": b"%PDF-one",
            "unexpected/prefix/finance/finance/extra.pdf": b"%PDF-extra",
            "unexpected/prefix/finance/finance/._0001.pdf": b"metadata",
            "unexpected/prefix/readme.txt": b"ignored",
        },
    )
    staging = tmp_path / "staging"

    result = extract_archive_pdfs(archive, staging)
    mapping, parents = inspect_archive_layout(result.pdfs, staging, "finance", {"0001.pdf"})

    assert sorted(mapping) == ["0001.pdf", "extra.pdf"]
    assert parents == {"unexpected/prefix/finance/finance": 2}
    assert result.apple_double_pdfs == 1


@pytest.mark.parametrize("member_name", ["../escape.pdf", "/absolute.pdf", "C:/escape.pdf"])
def test_extract_rejects_path_traversal(tmp_path: Path, member_name: str) -> None:
    archive = tmp_path / "unsafe.tar.gz"
    _write_tar(archive, {member_name: b"%PDF"})

    with pytest.raises(UnsafeArchiveError):
        extract_archive_pdfs(archive, tmp_path / "staging")


def test_layout_rejects_duplicate_basenames(tmp_path: Path) -> None:
    first = tmp_path / "a" / "same.pdf"
    second = tmp_path / "b" / "same.pdf"
    first.parent.mkdir()
    second.parent.mkdir()
    first.write_bytes(b"one")
    second.write_bytes(b"two")

    with pytest.raises(ArchiveLayoutError, match="duplicate PDF basename"):
        inspect_archive_layout((first, second), tmp_path, "finance", {"same.pdf"})


def test_validation_counts_rows_unique_found_and_missing(tmp_path: Path) -> None:
    benchmark = tmp_path / "benchmark.jsonl"
    rows = [
        {"source_pdf": "finance/finance/0001.pdf"},
        {"source_pdf": "finance/finance/0001.pdf"},
        {"source_pdf": "legal/legal/0002.pdf"},
    ]
    benchmark.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    output_root = tmp_path / "unidoc"
    found = output_root / "finance" / "finance" / "0001.pdf"
    found.parent.mkdir(parents=True)
    found.write_bytes(b"%PDF")

    summary = validate_benchmark(benchmark, output_root)

    assert summary.total_rows == 3
    assert len(summary.references) == 2
    assert summary.found == ("finance/finance/0001.pdf",)
    assert summary.missing == ("legal/legal/0002.pdf",)
