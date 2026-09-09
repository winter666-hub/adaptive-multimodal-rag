"""Local PDF text extraction for the Text RAG MVP."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path

import pymupdf
from pypdf import PdfReader

from furiosa_rag.models import PageText

logger = logging.getLogger(__name__)
_PYMUPDF_DIAGNOSTIC_LOCK = threading.Lock()


@dataclass(frozen=True, slots=True)
class PdfExtraction:
    pages: list[PageText]
    parser: str
    primary_error: str | None = None


class PdfTextExtractionError(ValueError):
    """Raised when neither supported parser can extract usable PDF text."""


class PdfTextExtractor:
    def extract(self, pdf_path: str | Path) -> list[PageText]:
        return self.extract_with_metadata(pdf_path).pages

    def extract_with_metadata(self, pdf_path: str | Path) -> PdfExtraction:
        path = Path(pdf_path)
        if not path.is_file():
            raise FileNotFoundError(f"PDF not found: {path}")
        if path.suffix.lower() != ".pdf":
            raise ValueError(f"Expected a PDF file: {path}")

        try:
            pages = self._extract_with_pypdf(path)
        except Exception as primary_error:
            primary_detail = f"{type(primary_error).__name__}: {primary_error}"
            logger.warning(
                "pypdf extraction failed for %s; retrying with PyMuPDF: %s",
                path,
                primary_detail,
            )
            try:
                pages = self._extract_with_pymupdf(path)
            except Exception as fallback_error:
                fallback_detail = f"{type(fallback_error).__name__}: {fallback_error}"
                raise PdfTextExtractionError(
                    f"PDF text extraction failed with pypdf ({primary_detail}) and "
                    f"PyMuPDF ({fallback_detail})"
                ) from fallback_error
            if not any(page.text for page in pages):
                raise PdfTextExtractionError(
                    "PyMuPDF fallback produced no extractable text; OCR is not implemented yet"
                ) from primary_error
            logger.warning("PDF text extraction succeeded with PyMuPDF fallback: %s", path)
            return PdfExtraction(pages, "pymupdf", primary_detail)

        if not any(page.text for page in pages):
            raise ValueError("PDF contains no extractable text; OCR is not implemented yet")
        return PdfExtraction(pages, "pypdf")

    @staticmethod
    def _extract_with_pypdf(path: Path) -> list[PageText]:
        reader = PdfReader(path)
        return [
            PageText(page_number=index, text=(page.extract_text() or "").strip())
            for index, page in enumerate(reader.pages, start=1)
        ]

    @staticmethod
    def _extract_with_pymupdf(path: Path) -> list[PageText]:
        # MuPDF can print thousands of low-level repair messages for one malformed file.
        # Serialize the temporary process-global setting and retain our concise fallback log.
        with _PYMUPDF_DIAGNOSTIC_LOCK:
            display_errors = pymupdf.TOOLS.mupdf_display_errors()
            display_warnings = pymupdf.TOOLS.mupdf_display_warnings()
            pymupdf.TOOLS.mupdf_display_errors(False)
            pymupdf.TOOLS.mupdf_display_warnings(False)
            try:
                with pymupdf.open(path) as document:
                    return [
                        PageText(page_number=index, text=(page.get_text("text") or "").strip())
                        for index, page in enumerate(document, start=1)
                    ]
            finally:
                pymupdf.TOOLS.mupdf_display_errors(display_errors)
                pymupdf.TOOLS.mupdf_display_warnings(display_warnings)
