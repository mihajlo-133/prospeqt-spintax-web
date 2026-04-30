"""Public synonym-axis tool functions (async) matching the 8-tool schema."""

from __future__ import annotations

from typing import Dict

from app.tools.sense_classifier import classify_word_sense_for_sentence
from app.tools.synonym_scorer import lookup_approved_lexicon, score_synonym_candidates
from app.tools.wordhippo_client import get_fetcher_async
from app.tools.wordhippo_parser import parse_wordhippo_sections


async def wordhippo_lookup(word: str) -> Dict[str, object]:
    """Fetch all synonym sections for *word* via the configured fetch mode.

    fetch_mode is NOT a parameter - it is read from settings.wordhippo_mode
    (default: spider). This is the production-locked behaviour per critic mandate.
    """
    fetcher = await get_fetcher_async()
    raw_html = await fetcher.fetch(word)
    definitions = parse_wordhippo_sections(raw_html)
    warnings = []
    if not definitions:
        warnings.append("No definitions were parsed from the WordHippo page.")
    return {
        "word": word,
        "definition_count": len(definitions),
        "definitions": definitions,
        "warnings": warnings,
    }


async def wordhippo_lookup_synonyms(word: str, context_id: str) -> Dict[str, object]:
    """Fetch synonyms for a specific WordHippo context_id."""
    payload = await wordhippo_lookup(word=word)
    for definition in payload["definitions"]:
        if definition["context_id"] == context_id:
            return {
                "word": word,
                "context_id": context_id,
                "word_type": definition.get("word_type"),
                "definition": definition["definition"],
                "synonyms": definition["synonyms"],
                "synonym_count": definition["synonym_count"],
                "warnings": [],
            }
    return {
        "word": word,
        "context_id": context_id,
        "word_type": None,
        "definition": "",
        "synonyms": [],
        "synonym_count": 0,
        "warnings": [f"No matching context_id found for {context_id}."],
    }
