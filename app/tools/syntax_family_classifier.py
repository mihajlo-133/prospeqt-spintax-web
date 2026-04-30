"""Syntax family classifier for high-value outbound copy structures."""

from __future__ import annotations

from typing import Dict, List

from app.tools.constants import (
    CTA_CURIOSITY_MARKERS,
    CTA_INTEREST_MARKERS,
    CTA_PERMISSION_MARKERS,
    CTA_PHONE_MARKERS,
    OBSERVATION_MARKERS,
    PROOF_MARKERS,
    ROLE_VALUES,
)
from app.tools.utils import contains_any, lower_text


def classify_syntax_family(sentence: str, role: str = "unknown") -> Dict[str, object]:
    if role not in ROLE_VALUES:
        role = "unknown"

    low = lower_text(sentence)
    warnings: List[str] = []

    if role == "cta":
        if contains_any(low, CTA_PHONE_MARKERS):
            family, confidence, alternates = (
                "cta_phone_number",
                0.95,
                ["cta_permission", "cta_interest"],
            )
        elif contains_any(low, CTA_CURIOSITY_MARKERS):
            family, confidence, alternates = (
                "cta_curiosity",
                0.92,
                ["cta_permission", "cta_interest"],
            )
        elif contains_any(low, CTA_PERMISSION_MARKERS):
            family, confidence, alternates = (
                "cta_permission",
                0.92,
                ["cta_curiosity", "cta_interest"],
            )
        elif contains_any(low, CTA_INTEREST_MARKERS):
            family, confidence, alternates = (
                "cta_interest",
                0.88,
                ["cta_permission", "cta_curiosity"],
            )
        else:
            family, confidence, alternates = "cta_generic", 0.5, []
            warnings.append("CTA family match was weak; review classification.")
    elif role == "opener":
        has_greeting = low.startswith(("hey ", "hi ", "hello ")) or "{{firstname}}" in low
        has_observation = contains_any(low, OBSERVATION_MARKERS)
        has_review = "review" in low
        if has_greeting and has_observation:
            family, confidence, alternates = (
                "greeting_plus_observation",
                0.9,
                ["evidence_first_observation", "greeting_only"],
            )
        elif has_observation and has_review:
            family, confidence, alternates = (
                "evidence_first_observation",
                0.87,
                ["greeting_plus_observation"],
            )
        elif has_observation:
            family, confidence, alternates = (
                "evidence_first_observation",
                0.8,
                ["greeting_plus_observation"],
            )
        elif has_greeting:
            family, confidence, alternates = "greeting_only", 0.82, ["greeting_plus_observation"]
        else:
            family, confidence, alternates = "opener_generic", 0.45, []
            warnings.append("Opener family match was weak; review classification.")
    elif role == "proof":
        if low.startswith("we helped") or low.startswith("our product helped"):
            family, confidence, alternates = (
                "proof_helper_led",
                0.93,
                ["proof_result_led", "proof_system_led"],
            )
        elif contains_any(low, ["grew from", "went from", "jump from"]):
            family, confidence, alternates = (
                "proof_result_led",
                0.9,
                ["proof_helper_led", "proof_system_led"],
            )
        elif contains_any(low, ["using our system", "used our product"]):
            family, confidence, alternates = (
                "proof_system_led",
                0.86,
                ["proof_helper_led", "proof_result_led"],
            )
        elif contains_any(low, PROOF_MARKERS):
            family, confidence, alternates = (
                "proof_generic",
                0.6,
                ["proof_helper_led", "proof_result_led"],
            )
        else:
            family, confidence, alternates = "proof_generic", 0.45, []
            warnings.append("Proof family match was weak; review classification.")
    else:
        family, confidence, alternates = f"{role}_generic", 0.4, []
        warnings.append("Role-specific family classifier not implemented; using generic fallback.")

    return {
        "sentence": sentence,
        "role": role,
        "family": family,
        "confidence": round(confidence, 2),
        "alternate_families": alternates,
        "warnings": warnings,
    }
