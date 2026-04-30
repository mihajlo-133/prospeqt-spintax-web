"""Heuristic candidate scoring for synonym selection."""

from __future__ import annotations

from typing import Dict, List

from app.tools.constants import APPROVED_LEXICON, ROLE_VALUES
from app.tools.utils import lower_text, score_to_status


FORMAL_REJECTIONS = {
    "ascertained",
    "observed",
    "identified",
    "examined",
    "facilitate",
    "enable",
    "optimize",
    "furnish",
    "transmit",
}

CASUAL_POSITIVES = {
    "noticed",
    "found",
    "spotted",
    "came across",
    "share",
    "show",
    "pass along",
    "support",
    "back",
}


def score_synonym_candidates(
    source_word: str,
    sentence: str,
    candidates: List[str],
    role: str = "unknown",
    sense_label: str = "unknown",
) -> Dict[str, object]:
    if role not in ROLE_VALUES:
        role = "unknown"

    source_word = source_word.strip().lower()
    sentence_low = lower_text(sentence)
    approved_bank = APPROVED_LEXICON.get(source_word, {})
    approved = {item.lower() for item in approved_bank.get("approved", [])}
    candidate_review = {item.lower() for item in approved_bank.get("candidate_review", [])}
    rejected_bank = {item.lower() for item in approved_bank.get("rejected", [])}

    results = []
    for candidate in candidates:
        cand = candidate.strip()
        cand_low = cand.lower()
        semantic_fit = _semantic_fit(source_word, cand_low, sense_label)
        tone_fit = _tone_fit(cand_low)
        corpus_familiarity = 0.85 if cand_low in approved or cand_low in candidate_review else 0.45
        anti_ai_fit = 0.2 if cand_low in FORMAL_REJECTIONS or cand_low in rejected_bank else 0.9
        diversification_value = 0.3 if cand_low == source_word else 0.7
        placement_fit = _placement_fit(cand_low, role, sentence_low)

        final_score = (
            semantic_fit * 0.35
            + tone_fit * 0.2
            + corpus_familiarity * 0.15
            + anti_ai_fit * 0.15
            + diversification_value * 0.05
            + placement_fit * 0.1
        )

        if cand_low in rejected_bank:
            final_score = min(final_score, 0.35)
        elif cand_low in approved:
            final_score = max(final_score, 0.8)
        elif cand_low in candidate_review:
            final_score = max(final_score, 0.55)

        status = score_to_status(final_score)
        reason = _reason_for_candidate(cand_low, status, sentence_low)
        results.append(
            {
                "candidate": cand,
                "scores": {
                    "semantic_fit": round(semantic_fit, 2),
                    "tone_fit": round(tone_fit, 2),
                    "corpus_familiarity": round(corpus_familiarity, 2),
                    "anti_ai_fit": round(anti_ai_fit, 2),
                    "diversification_value": round(diversification_value, 2),
                    "placement_fit": round(placement_fit, 2),
                },
                "final_score": round(final_score, 2),
                "status": status,
                "reason": reason,
            }
        )

    return {
        "source_word": source_word,
        "sentence": sentence,
        "role": role,
        "sense_label": sense_label,
        "results": results,
    }


def lookup_approved_lexicon(source_word: str, role: str = "unknown", sense_label: str = "unknown") -> Dict[str, object]:
    bank = APPROVED_LEXICON.get(source_word.strip().lower(), {"approved": [], "candidate_review": [], "rejected": []})
    return {
        "source_word": source_word.strip().lower(),
        "role": role,
        "sense_label": sense_label,
        "approved": bank.get("approved", []),
        "candidate_review": bank.get("candidate_review", []),
        "rejected": bank.get("rejected", []),
    }


def _semantic_fit(source_word: str, candidate: str, sense_label: str) -> float:
    if candidate == source_word:
        return 0.9
    if sense_label in {"visual_observation", "data_observation", "discovery_inference"}:
        return 0.9 if candidate in {"noticed", "found", "spotted", "came across", "looked at"} else 0.45
    if sense_label in {"send_share_cta", "phone_number_cta"}:
        return 0.9 if candidate in {"show", "share", "send", "pass along", "walk through", "go over"} else 0.4
    if sense_label in {"proof_growth", "mechanism_help"}:
        return 0.85 if candidate in {"support", "back", "help", "grow"} else 0.4
    return 0.55


def _tone_fit(candidate: str) -> float:
    if candidate in FORMAL_REJECTIONS:
        return 0.2
    if candidate in CASUAL_POSITIVES:
        return 0.9
    if len(candidate.split()) > 3:
        return 0.45
    return 0.65


def _placement_fit(candidate: str, role: str, sentence_low: str) -> float:
    if role == "cta":
        return 0.9 if candidate in {"show", "share", "send", "pass along", "walk through", "go over"} else 0.45
    if role == "opener":
        return 0.9 if candidate in {"noticed", "found", "spotted", "came across"} else 0.45
    if role == "proof":
        return 0.8 if candidate in {"support", "back", "help", "grow"} else 0.45
    return 0.6


def _reason_for_candidate(candidate: str, status: str, sentence_low: str) -> str:
    if candidate in FORMAL_REJECTIONS:
        return "Too formal or AI-ish for cold-email tone."
    if status == "approved":
        return "Good semantic and tone fit for this role."
    if status == "candidate_review":
        return "Potentially useful, but should be reviewed for tone and context."
    return "Weak semantic or tone fit for the current sentence/role."
