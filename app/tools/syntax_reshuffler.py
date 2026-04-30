"""Safe-ish syntax reshuffler for supported high-value sentence families."""

from __future__ import annotations

import re
from typing import Dict, List

from app.tools.syntax_block_classifier import classify_sentence_blocks
from app.tools.syntax_family_classifier import classify_syntax_family
from app.tools.utils import unique_preserve_order


def reshape_blocks(
    sentence: str,
    role: str = "unknown",
    source_family: str | None = None,
    target_family: str | None = None,
    max_variants: int = 3,
) -> Dict[str, object]:
    block_payload = classify_sentence_blocks(sentence, role=role)
    family_payload = classify_syntax_family(sentence, role=role)
    source_family = source_family or family_payload["family"]
    target_family = target_family or source_family

    variants = []
    if role == "opener":
        variants.extend(_reshape_opener(sentence, block_payload, target_family))
    elif role == "cta":
        variants.extend(_reshape_cta(sentence, target_family))
    elif role == "proof":
        variants.extend(_reshape_proof(sentence, target_family))

    unique_variants = []
    for text, family, transform, conf, warnings in unique_preserve_order(variants):
        if text.strip() and text.strip() != sentence.strip():
            unique_variants.append(
                {
                    "text": text.strip(),
                    "family": family,
                    "transformation_type": transform,
                    "meaning_preservation_confidence": conf,
                    "warnings": warnings,
                }
            )
    return {
        "sentence": sentence,
        "source_family": source_family,
        "target_family": target_family,
        "variants": unique_variants[:max_variants],
    }


def _reshape_opener(sentence: str, payload: Dict[str, object], target_family: str) -> List[tuple]:
    blocks = {block["label"]: block["text"] for block in payload["blocks"]}
    greeting = blocks.get("greeting", "")
    obs = blocks.get("observation_verb", "")
    evidence = blocks.get("evidence_object", "")
    source = blocks.get("source_phrase", "")
    time_phrase = blocks.get("time_phrase", "")
    data_phrase = blocks.get("data_source_phrase", "")
    variants = []

    if obs and data_phrase:
        body = sentence.strip()
        body = _strip_leading(body, greeting)
        body = re.sub(re.escape(data_phrase), "", body, count=1, flags=re.I)
        body = re.sub(r"\s+", " ", body).strip(" ,.-—")
        variants.append(
            (
                f"{data_phrase[0].upper() + data_phrase[1:]}, {body}.".replace("..", "."),
                "evidence_first_observation",
                "adjunct_fronting",
                0.83,
                [],
            )
        )
    if greeting and obs and evidence:
        tail = " ".join(part for part in [evidence, source, time_phrase] if part).strip()
        variants.append(
            (
                f"{greeting}, {obs} {tail}.".replace("..", "."),
                "greeting_plus_observation",
                "separator_cleanup",
                0.88,
                [],
            )
        )
    if obs and evidence and source:
        lead = f"{obs.capitalize()} {evidence} {source}"
        if time_phrase:
            lead += f" {time_phrase}"
        variants.append(
            (
                lead.rstrip(".") + ".",
                "evidence_first_observation",
                "greeting_drop",
                0.79,
                ["Greeting removed."],
            )
        )
    return variants


def _reshape_cta(sentence: str, target_family: str) -> List[tuple]:
    stripped = sentence.strip().rstrip("?")
    variants = []
    if "would it hurt to see" in sentence.lower():
        variants.append(
            (
                re.sub(r"(?i)would it hurt to see if", "Worth seeing if", stripped) + "?",
                "cta_curiosity",
                "curiosity_reframe",
                0.84,
                [],
            )
        )
        variants.append(
            (
                re.sub(r"(?i)would it hurt to see if", "Open to seeing if", stripped) + "?",
                "cta_curiosity",
                "curiosity_reframe",
                0.8,
                [],
            )
        )
    if "want to know more" in sentence.lower():
        variants.append(
            (
                re.sub(r"(?i)would you want to know more", "Want me to send more", stripped) + "?",
                "cta_permission",
                "family_shift",
                0.68,
                ["Meaning may shift from interest ask to permission ask."],
            )
        )
    if "good number to reach you" in sentence.lower():
        variants.append(
            (
                re.sub(
                    r"(?i)would you tell me a good number to reach you",
                    "Is there a good number to reach you on",
                    stripped,
                )
                + "?",
                "cta_phone_number",
                "contact_rephrase",
                0.9,
                [],
            )
        )
    return variants


def _reshape_proof(sentence: str, target_family: str) -> List[tuple]:
    variants = []
    match = re.search(
        r"We helped\s+(.+?)\s+(grow|go|jump|went)\s+from\s+(.+?)\s+to\s+(.+?)\s+(in\s+.+?)\s+by\s+(.+)",
        sentence,
        re.I,
    )
    if match:
        actor, verb, start, end, time_phrase, mechanism = match.groups()
        variants.append(
            (
                f"{actor} grew from {start} to {end} {time_phrase} with our help by {mechanism}",
                "proof_result_led",
                "helper_to_result_led",
                0.86,
                [],
            )
        )
        variants.append(
            (
                f"Using our system, {actor} grew from {start} to {end} {time_phrase} by {mechanism}",
                "proof_system_led",
                "helper_to_system_led",
                0.82,
                [],
            )
        )
    elif sentence.lower().startswith("our product helped"):
        variants.append(
            (
                sentence.replace("Our product helped", "Using our product,"),
                "proof_system_led",
                "helper_to_system_led",
                0.74,
                [],
            )
        )
    return variants


def _strip_leading(sentence: str, prefix: str) -> str:
    if not prefix:
        return sentence.strip()
    trimmed = sentence.strip()
    if trimmed.lower().startswith(prefix.lower()):
        return trimmed[len(prefix) :].lstrip(" ,—-")
    return trimmed
