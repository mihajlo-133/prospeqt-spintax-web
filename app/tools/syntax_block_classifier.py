"""Heuristic sentence block classifier for outbound-copy structures."""

from __future__ import annotations

import re
from typing import Dict, List

from app.tools.constants import ROLE_VALUES
from app.tools.utils import lower_text


GREETING_RE = re.compile(r"^(hey|hi|hello)\s+[^,\-—]+", re.I)


def classify_sentence_blocks(sentence: str, role: str = "unknown") -> Dict[str, object]:
    if role not in ROLE_VALUES:
        role = "unknown"

    low = lower_text(sentence)
    blocks: List[Dict[str, object]] = []
    warnings: List[str] = []

    if role == "opener":
        _classify_opener(sentence, low, blocks)
        sentence_type = "opener_observation" if any(b["label"] == "observation_verb" for b in blocks) else "opener_generic"
    elif role == "cta":
        _classify_cta(sentence, low, blocks)
        sentence_type = "cta_question" if "?" in sentence else "cta_statement"
    elif role == "proof":
        _classify_proof(sentence, low, blocks)
        sentence_type = "proof_line"
    else:
        _classify_generic(sentence, low, blocks)
        sentence_type = f"{role}_generic"

    if not blocks:
        warnings.append("No meaningful block structure was inferred; falling back to generic sentence block.")
        blocks.append({"label": "sentence", "text": sentence, "required": True, "movable": False, "notes": "fallback"})

    return {
        "sentence": sentence,
        "role": role,
        "sentence_type": sentence_type,
        "blocks": blocks,
        "warnings": warnings,
    }


def _classify_opener(sentence: str, low: str, blocks: List[Dict[str, object]]) -> None:
    greeting_match = GREETING_RE.match(sentence.strip())
    if greeting_match:
        blocks.append(_block("greeting", greeting_match.group(0), True, True, "optional opener greeting"))
    if any(token in low for token in [" - ", " — "]):
        sep = "-" if " - " in sentence else "—"
        blocks.append(_block("separator", sep, False, True, "surface separator"))
    observation = _first_match(low, ["came across", "records show", "noticed", "spotted", "found", "saw"])
    if observation:
        blocks.append(_block("observation_verb", observation, True, False, "core observation verb"))
    evidence = _extract_evidence_object(sentence)
    if evidence:
        blocks.append(_block("evidence_object", evidence, True, False, "main observed object"))
    source_phrase, time_phrase = _extract_review_source_and_time(sentence)
    if source_phrase:
        blocks.append(_block("source_phrase", source_phrase, False, True, "source phrase"))
    date_phrase = time_phrase or _extract_date_phrase(sentence)
    if date_phrase:
        blocks.append(_block("time_phrase", date_phrase, False, True, "time/date phrase"))
    data_phrase = _extract_data_phrase(sentence)
    if data_phrase:
        blocks.append(_block("data_source_phrase", data_phrase, False, True, "records/data framing"))


def _classify_cta(sentence: str, low: str, blocks: List[Dict[str, object]]) -> None:
    if sentence.strip().endswith("?"):
        blocks.append(_block("ask_clause", sentence.strip(), True, False, "question ask"))
    conditional = _extract_if_clause(sentence)
    if conditional:
        blocks.append(_block("condition_clause", conditional, False, True, "if-based condition"))
    if "good number" in low or "reach you" in low:
        blocks.append(_block("channel_contact_clause", sentence.strip(), True, False, "phone/contact ask"))
    offer = _first_match(low, ["want me to", "can i", "should i", "would you want", "would it hurt"])
    if offer:
        blocks.append(_block("offer_clause", offer, False, True, "cta framing clause"))


def _classify_proof(sentence: str, low: str, blocks: List[Dict[str, object]]) -> None:
    actor = _extract_actor(sentence)
    if actor:
        blocks.append(_block("actor_np", actor, True, False, "actor/company"))
    result = _first_match(low, ["we helped", "our product helped", "grew from", "went from", "jump from", "used our product"])
    if result:
        blocks.append(_block("result_vp", result, True, False, "main proof/result frame"))
    metric = _extract_metric_phrase(sentence)
    if metric:
        blocks.append(_block("metric_phrase", metric, True, False, "quantitative proof phrase"))
    time_phrase = _extract_time_window(sentence)
    if time_phrase:
        blocks.append(_block("time_phrase", time_phrase, False, True, "time window"))
    mechanism = _extract_by_phrase(sentence)
    if mechanism:
        blocks.append(_block("mechanism_phrase", mechanism, False, True, "how it happened"))


def _classify_generic(sentence: str, low: str, blocks: List[Dict[str, object]]) -> None:
    blocks.append(_block("sentence", sentence.strip(), True, False, "generic fallback"))


def _block(label: str, text: str, required: bool, movable: bool, notes: str) -> Dict[str, object]:
    return {
        "label": label,
        "text": text.strip(),
        "required": required,
        "movable": movable,
        "notes": notes,
    }


def _first_match(low: str, options: List[str]) -> str:
    for option in sorted(options, key=len, reverse=True):
        if option in low:
            return option
    return ""


def _extract_phrase(sentence: str, preposition: str) -> str:
    match = re.search(rf'\b{re.escape(preposition)}\b\s+[^,.!?]+', sentence, re.I)
    return match.group(0).strip() if match else ""


def _extract_review_source_and_time(sentence: str) -> tuple[str, str]:
    matches = re.findall(r'\bfrom\b\s+\{\{[^}]+\}\}', sentence, re.I)
    if not matches:
        return "", ""
    source = ""
    time_phrase = ""
    for match in matches:
        low = match.lower()
        if "date" in low or "year" in low:
            time_phrase = match.strip()
        elif not source:
            source = match.strip()
    return source, time_phrase


def _extract_date_phrase(sentence: str) -> str:
    match = re.search(r'\b(?:from|on|in)\b\s+\{\{[^}]*?(?:date|year)[^}]*\}\}|\b(?:in\s+\d{4}|from\s+\d{4})\b', sentence, re.I)
    return match.group(0).strip() if match else ""


def _extract_data_phrase(sentence: str) -> str:
    match = re.search(r'(?:in|from|based on)\s+(?:the\s+)?(?:sba\s+data|sba\s+records|sba\s+files|records|data)', sentence, re.I)
    return match.group(0).strip() if match else ""


def _extract_if_clause(sentence: str) -> str:
    match = re.search(r'\bif\b[^?.,!]+', sentence, re.I)
    return match.group(0).strip() if match else ""


def _extract_actor(sentence: str) -> str:
    if sentence.lower().startswith("we helped"):
        return "we"
    match = re.match(r'([A-Z][A-Za-z0-9&\-\s]+?)\s+(?:grew|went|jumped|used)', sentence)
    return match.group(1).strip() if match else ""


def _extract_metric_phrase(sentence: str) -> str:
    match = re.search(r'\bfrom\s+\d+[\d,]*\s+to\s+\d+[\d,]*\b[^,.!?]*|\badd(?:ed)?\s+\{\{[^}]+\}\}|\badd(?:ed)?\s+\d+[\d,]*\b[^,.!?]*', sentence, re.I)
    return match.group(0).strip() if match else ""


def _extract_time_window(sentence: str) -> str:
    match = re.search(r'\bin\s+(?:\d+\s+)?(?:day|days|week|weeks|month|months|year|years)\b[^,.!?]*|\bunder\s+a\s+month\b', sentence, re.I)
    return match.group(0).strip() if match else ""


def _extract_by_phrase(sentence: str) -> str:
    match = re.search(r'\bby\b\s+[^,.!?]+', sentence, re.I)
    return match.group(0).strip() if match else ""


def _extract_evidence_object(sentence: str) -> str:
    match = re.search(r'(?:the\s+)?(?:1-star|5-star|google)?\s*review[^,.!?]*|\b{{review_name}}[^,.!?]*review[^,.!?]*|\b{{loan_amount}}[^,.!?]*', sentence, re.I)
    return match.group(0).strip() if match else ""
