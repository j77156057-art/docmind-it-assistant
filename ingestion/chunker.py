"""Heading-aware, overlap-preserving document chunking."""
from __future__ import annotations

from dataclasses import dataclass

from .parsers import ParsedDocument


@dataclass(frozen=True)
class DocumentChunk:
    ordinal: int
    heading: str
    page_number: int | None
    content: str


def chunk_document(document: ParsedDocument, *, max_chars: int, overlap_chars: int) -> list[DocumentChunk]:
    chunks: list[DocumentChunk] = []
    for section in document.sections:
        text = "\n".join(line.strip() for line in section.text.splitlines() if line.strip())
        start = 0
        while start < len(text):
            end = min(len(text), start + max_chars)
            if end < len(text):
                boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end))
                if boundary > start + max_chars // 2:
                    end = boundary + 1
            content = text[start:end].strip()
            if content:
                chunks.append(DocumentChunk(
                    len(chunks), section.heading[:512], section.page_number, content,
                ))
            if end >= len(text):
                break
            start = max(start + 1, end - overlap_chars)
    if not chunks:
        raise ValueError("文档分块结果为空")
    return chunks
