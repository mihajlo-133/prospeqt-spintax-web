"""Shared helpers used across spintax agent tooling modules."""

from __future__ import annotations

import html
import json
import re
from typing import Iterable, List, Sequence, TypeVar

T = TypeVar("T")


def clean_text(value: str) -> str:
    value = html.unescape(value)
    value = re.sub(r"<.*?>", " ", value, flags=re.S)
    value = re.sub(r"\s+", " ", value)
    return value.strip()


def normalize_space(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()


def lower_text(value: str) -> str:
    return normalize_space(value).lower()


def unique_preserve_order(values: Iterable[T]) -> List[T]:
    seen = set()
    out: List[T] = []
    for value in values:
        key = json.dumps(value, sort_keys=True, ensure_ascii=False) if isinstance(value, (dict, list)) else str(value).lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def contains_any(text: str, patterns: Sequence[str]) -> bool:
    low = text.lower()
    return any(pattern.lower() in low for pattern in patterns)


def score_to_status(score: float) -> str:
    if score >= 0.75:
        return "approved"
    if score >= 0.5:
        return "candidate_review"
    return "rejected"
