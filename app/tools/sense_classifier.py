"""Heuristic word-sense classifier for outbound-copy use cases."""

from __future__ import annotations

from typing import Dict, List

from app.tools.constants import ROLE_VALUES, SENSE_KEYWORDS
from app.tools.utils import lower_text


def classify_word_sense_for_sentence(word: str, sentence: str, role: str = "unknown") -> Dict[str, object]:
    if role not in ROLE_VALUES:
        role = "unknown"

    low = lower_text(sentence)
    word_low = word.strip().lower()
    warnings: List[str] = []

    scores = {label: 0 for label in SENSE_KEYWORDS}
    for label, keywords in SENSE_KEYWORDS.items():
        scores[label] = sum(1 for keyword in keywords if keyword in low)

    if role == "cta":
        scores["send_share_cta"] += 2
        if "number" in low or "reach you" in low:
            scores["phone_number_cta"] += 3
    if role in {"opener", "body"} and any(k in low for k in ["sba", "records", "data"]):
        scores["data_observation"] += 3
    if role == "proof":
        scores["proof_growth"] += 2
    if word_low == "help":
        scores["mechanism_help"] += 4
    if word_low in {"send", "show", "share"}:
        scores["send_share_cta"] += 4
    if word_low in {"saw", "noticed", "spotted"}:
        scores["visual_observation"] += 2
        if any(k in low for k in ["data", "records", "sba"]):
            scores["data_observation"] += 2

    best_label = max(scores, key=scores.get)
    best_score = scores[best_label]
    if best_score == 0:
        best_label = "unknown"
        warnings.append("Could not confidently infer a practical sense label from sentence heuristics.")

    recommended_context_ids = _recommended_context_ids(word_low, best_label)
    confidence = 0.35 if best_label == "unknown" else min(0.95, 0.45 + best_score * 0.1)
    if len(recommended_context_ids) > 1:
        warnings.append("Multiple plausible WordHippo contexts; caller should prefer explicit review.")

    return {
        "word": word,
        "sentence": sentence,
        "role": role,
        "sense_label": best_label,
        "recommended_context_ids": recommended_context_ids,
        "confidence": round(confidence, 2),
        "warnings": warnings,
    }


def _recommended_context_ids(word: str, sense_label: str) -> List[str]:
    # These ids are anchored to observed WordHippo page structure for key high-value words.
    # Fallback remains empty if the mapping is not yet curated.
    if word == "saw":
        mapping = {
            "visual_observation": ["C0-7"],
            "data_observation": ["C0-9", "C0-10"],
            "discovery_inference": ["C0-10"],
        }
        return mapping.get(sense_label, ["C0-7"])
    if word == "send":
        mapping = {
            "send_share_cta": ["C0-1", "C0-5", "C0-12", "C0-19"],
            "phone_number_cta": ["C0-12"],
        }
        return mapping.get(sense_label, ["C0-1"])
    return []
