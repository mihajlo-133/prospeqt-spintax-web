"""Thin wrapper around the in-process APPROVED_LEXICON dict.

In v1 this is a static seed bank held in `app/tools/constants.py`. The
wrapper exists so future versions can swap the backing store (Postgres,
file, remote service) without changing call sites in the runner.

Public surface mirrors what `synonym_scorer.lookup_approved_lexicon`
needs internally and lets the agent tool layer call into a single
abstraction.
"""

from __future__ import annotations

from typing import Dict, List

from app.tools.constants import APPROVED_LEXICON


def get_approved_synonyms(source_word: str) -> Dict[str, List[str]]:
    """Return the three lexicon buckets for a source word.

    Always returns the full three-key shape — empty lists for unknown
    words — so callers don't need to special-case missing entries.
    """
    bank = APPROVED_LEXICON.get(source_word.strip().lower(), {})
    return {
        "approved": list(bank.get("approved", [])),
        "candidate_review": list(bank.get("candidate_review", [])),
        "rejected": list(bank.get("rejected", [])),
    }


def has_entry(source_word: str) -> bool:
    """True if the lexicon has any cleared entries for this source word."""
    bank = APPROVED_LEXICON.get(source_word.strip().lower(), {})
    return any(bank.get(key) for key in ("approved", "candidate_review", "rejected"))
