"""Structure repetition lint for sets of campaign lines.

Renamed from `structure_lint.py` in pi-agents to avoid name overlap with
`app/lint.py` (the deterministic spintax linter). This module is the
fingerprint-style corpus-level repetition checker.
"""

from __future__ import annotations

from collections import Counter
from typing import Dict, List

from app.tools.syntax_family_classifier import classify_syntax_family


def lint_structure_repetition(lines: List[str], role: str = "unknown") -> Dict[str, object]:
    family_counts = Counter()
    warnings: List[str] = []
    first_words = Counter()

    for line in lines:
        payload = classify_syntax_family(line, role=role)
        family_counts[payload["family"]] += 1
        first_token = (line.strip().split() or [""])[0].lower()
        if first_token:
            first_words[first_token] += 1

    line_count = len(lines)
    if line_count:
        dominant_family, dominant_count = family_counts.most_common(1)[0]
        ratio = dominant_count / line_count
        if ratio >= 0.7:
            warnings.append(
                f"High family concentration: {dominant_family} appears in {dominant_count}/{line_count} lines."
            )
        elif ratio >= 0.5:
            warnings.append(
                f"Moderate family concentration: {dominant_family} appears in {dominant_count}/{line_count} lines."
            )
        repeated_starters = [
            token for token, count in first_words.items() if count >= max(2, line_count // 2)
        ]
        if repeated_starters:
            warnings.append(
                "Repeated line starters detected: " + ", ".join(sorted(repeated_starters))
            )
    risk_level = "low"
    if any("High" in warning for warning in warnings):
        risk_level = "high"
    elif warnings:
        risk_level = "medium"

    return {
        "line_count": line_count,
        "family_counts": dict(family_counts),
        "repetition_warnings": warnings,
        "risk_level": risk_level,
    }
