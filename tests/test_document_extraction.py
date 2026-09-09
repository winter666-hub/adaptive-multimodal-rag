from pathlib import Path
from types import SimpleNamespace
from typing import Self

import pytest

from furiosa_rag.document import PdfTextExtractionError, PdfTextExtractor


class _Page:
    def __init__(self, text: str) -> None:
        self.text = text

    def extract_text(self) -> str:
        return self.text

    def get_text(self, mode: str) -> str:
        assert mode == "text"
        return self.text


class _Document:
    def __init__(self, texts: list[str]) -> None:
        self.pages = [_Page(text) for text in texts]

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def __iter__(self):
        return iter(self.pages)


def _pdf(tmp_path: Path) -> Path:
    path = tmp_path / "document.pdf"
    path.write_bytes(b"%PDF-synthetic")
    return path


def test_primary_success_does_not_invoke_fallback(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "furiosa_rag.document.PdfReader", lambda path: SimpleNamespace(pages=[_Page("primary")])
    )
    fallback = pytest.fail
    monkeypatch.setattr("furiosa_rag.document.pymupdf.open", fallback)

    result = PdfTextExtractor().extract_with_metadata(_pdf(tmp_path))

    assert result.parser == "pypdf"
    assert result.primary_error is None
    assert [(page.page_number, page.text) for page in result.pages] == [(1, "primary")]


def test_primary_exception_invokes_fallback_and_preserves_pages(
    tmp_path: Path, monkeypatch
) -> None:
    def fail_primary(path: Path) -> None:
        raise RuntimeError("malformed stream")

    monkeypatch.setattr("furiosa_rag.document.PdfReader", fail_primary)
    monkeypatch.setattr(
        "furiosa_rag.document.pymupdf.open", lambda path: _Document(["first", "", "third"])
    )

    result = PdfTextExtractor().extract_with_metadata(_pdf(tmp_path))

    assert result.parser == "pymupdf"
    assert result.primary_error == "RuntimeError: malformed stream"
    assert [(page.page_number, page.text) for page in result.pages] == [
        (1, "first"),
        (2, ""),
        (3, "third"),
    ]


def test_empty_fallback_is_not_treated_as_success(tmp_path: Path, monkeypatch) -> None:
    def fail_primary(path: Path) -> None:
        raise RuntimeError("malformed stream")

    monkeypatch.setattr("furiosa_rag.document.PdfReader", fail_primary)
    monkeypatch.setattr("furiosa_rag.document.pymupdf.open", lambda path: _Document(["", " "]))

    with pytest.raises(PdfTextExtractionError, match="no extractable text"):
        PdfTextExtractor().extract_with_metadata(_pdf(tmp_path))
