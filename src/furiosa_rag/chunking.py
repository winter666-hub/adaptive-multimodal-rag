"""Page-preserving configurable word chunking."""

from __future__ import annotations

from furiosa_rag.models import Chunk, PageText

MAX_EMBEDDING_INPUT_UTF8_BYTES = 7_600


def _split_long_word(word: str, max_utf8_bytes: int) -> list[str]:
    parts: list[str] = []
    current: list[str] = []
    current_bytes = 0
    for character in word:
        character_bytes = len(character.encode("utf-8"))
        if current and current_bytes + character_bytes > max_utf8_bytes:
            parts.append("".join(current))
            current = []
            current_bytes = 0
        current.append(character)
        current_bytes += character_bytes
    if current:
        parts.append("".join(current))
    return parts


def split_text_for_embedding(
    text: str,
    *,
    max_utf8_bytes: int = MAX_EMBEDDING_INPUT_UTF8_BYTES,
    overlap_words: int = 0,
) -> list[str]:
    """Split only oversized text using a conservative tokenizer-free byte bound."""
    if max_utf8_bytes <= 0:
        raise ValueError("max_utf8_bytes must be greater than zero")
    if overlap_words < 0:
        raise ValueError("overlap_words must not be negative")
    normalized = " ".join(text.split())
    if not normalized:
        return []
    if len(normalized.encode("utf-8")) <= max_utf8_bytes:
        return [normalized]

    words = normalized.split()
    segments: list[str] = []
    start = 0
    while start < len(words):
        if len(words[start].encode("utf-8")) > max_utf8_bytes:
            segments.extend(_split_long_word(words[start], max_utf8_bytes))
            start += 1
            continue

        end = start
        segment_bytes = 0
        while end < len(words):
            word_bytes = len(words[end].encode("utf-8"))
            added_bytes = word_bytes + (1 if end > start else 0)
            if segment_bytes + added_bytes > max_utf8_bytes:
                break
            segment_bytes += added_bytes
            end += 1
        segments.append(" ".join(words[start:end]))
        if end == len(words):
            break
        # A word larger than the whole budget cannot share a word-overlap window.
        # Advance directly to it instead of emitting progressively smaller duplicates.
        if len(words[end].encode("utf-8")) > max_utf8_bytes:
            start = end
        else:
            start = max(start + 1, end - overlap_words)
    return segments


class PageChunker:
    def __init__(
        self,
        chunk_size: int = 700,
        chunk_overlap: int = 100,
        max_embedding_input_utf8_bytes: int = MAX_EMBEDDING_INPUT_UTF8_BYTES,
    ) -> None:
        if chunk_size <= 0:
            raise ValueError("chunk_size must be greater than zero")
        if chunk_overlap < 0 or chunk_overlap >= chunk_size:
            raise ValueError("chunk_overlap must be between zero and chunk_size - 1")
        if max_embedding_input_utf8_bytes <= 0:
            raise ValueError("max_embedding_input_utf8_bytes must be greater than zero")
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.max_embedding_input_utf8_bytes = max_embedding_input_utf8_bytes

    def split(self, pages: list[PageText]) -> list[Chunk]:
        chunks: list[Chunk] = []
        step = self.chunk_size - self.chunk_overlap
        for page in pages:
            words = page.text.split()
            page_chunk_index = 1
            for start in range(0, len(words), step):
                candidate = " ".join(words[start : start + self.chunk_size]).strip()
                for text in split_text_for_embedding(
                    candidate,
                    max_utf8_bytes=self.max_embedding_input_utf8_bytes,
                    overlap_words=self.chunk_overlap,
                ):
                    chunks.append(
                        Chunk(
                            chunk_id=f"page-{page.page_number}-chunk-{page_chunk_index}",
                            page_number=page.page_number,
                            text=text,
                        )
                    )
                    page_chunk_index += 1
                if start + self.chunk_size >= len(words):
                    break
        return chunks
