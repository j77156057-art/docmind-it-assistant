"""Heading-aware, overlap-preserving document chunking.

The default path produces one chunk per window (backward compatible). When
``child_max_chars`` is supplied, a *parent-child* layout is produced: large
parent windows (``max_chars``) give the LLM rich context, while smaller child
windows (``child_max_chars``) are what actually get embedded and retrieved. Each
child carries the enclosing parent window in ``parent_content`` so generation can
be grounded in a larger span than the one that matched the query.
"""
from __future__ import annotations

from dataclasses import dataclass

from .parsers import ParsedDocument


@dataclass(frozen=True)
class DocumentChunk:
    ordinal: int
    heading: str
    page_number: int | None
    content: str
    parent_content: str = ""


def _split_windows(text: str, *, size: int, overlap: int) -> list[tuple[int, int, str]]:
    """Slide a fixed-size, overlap-preserving window across ``text``.

    Returns ``(start, end, content)`` tuples. Boundaries prefer a line break or a
    Chinese full stop when one sits in the latter half of the window, so chunks do
    not cut mid-thought unless the window is too small to avoid it.
    """
    windows: list[tuple[int, int, str]] = []
    if size <= 0:
        raise ValueError("切片窗口大小必须为正数")
    start = 0
    while start < len(text):
        end = min(len(text), start + size)
        if end < len(text):
            boundary = max(text.rfind("\n", start, end), text.rfind("。", start, end))
            if boundary > start + size // 2:
                end = boundary + 1
        content = text[start:end].strip()
        if content:
            windows.append((start, end, content))
        if end >= len(text):
            break
        start = max(start + 1, end - overlap)
    return windows


def chunk_document(document: ParsedDocument, *, max_chars: int, overlap_chars: int,
                  child_max_chars: int | None = None,
                  child_overlap_chars: int | None = None) -> list[DocumentChunk]:
    """Chunk a parsed document.

    With ``child_max_chars=None`` this behaves exactly like before: one windowed
    chunk per section, ``parent_content`` left empty. With a child size, parent
    windows (``max_chars``) and child windows (``child_max_chars``) are produced
    per section and every child is linked to the parent window that contains it.
    """
    chunks: list[DocumentChunk] = []
    for section in document.sections:
        text = "\n".join(line.strip() for line in section.text.splitlines() if line.strip())
        if not text:
            continue
        parent_windows = _split_windows(text, size=max_chars, overlap=overlap_chars)
        if child_max_chars is None:
            for start, _end, content in parent_windows:
                chunks.append(DocumentChunk(
                    len(chunks), section.heading[:512], section.page_number, content,
                ))
            continue
        child_overlap = (
            child_overlap_chars
            if child_overlap_chars is not None
            else min(overlap_chars, max(0, child_max_chars // 4))
        )
        child_windows = _split_windows(text, size=child_max_chars, overlap=child_overlap)
        for child_start, _c_end, child_content in child_windows:
            # Link the child to the parent window that best contains its start offset.
            parent_text = ""
            for p_start, p_end, p_content in parent_windows:
                if p_start <= child_start < p_end:
                    parent_text = p_content
                    break
            if not parent_text and parent_windows:
                parent_text = max(
                    parent_windows,
                    key=lambda pw: min(pw[1], _c_end) - max(pw[0], child_start),
                )[2]
            if not parent_text:
                parent_text = child_content
            chunks.append(DocumentChunk(
                len(chunks), section.heading[:512], section.page_number,
                child_content, parent_text,
            ))
    if not chunks:
        raise ValueError("文档分块结果为空")
    return chunks
