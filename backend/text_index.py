"""Language-neutral lexical normalization for retrieval and indexing."""
from __future__ import annotations

import re


def lexical_terms(text: str) -> list[str]:
    value = (text or "").lower()
    latin = re.findall(r"[a-z0-9_-]{2,}", value)
    cjk = [
        value[index:index + 2]
        for index in range(max(0, len(value) - 1))
        if "\u4e00" <= value[index] <= "\u9fff"
        and "\u4e00" <= value[index + 1] <= "\u9fff"
    ]
    return latin + cjk


def lexical_text(text: str) -> str:
    return " ".join(lexical_terms(text))
