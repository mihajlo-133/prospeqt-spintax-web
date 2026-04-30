"""Async tool implementations + per-loop dispatchers for the spintax runner.

Each public `*_impl` function here corresponds to one of the 8 tools in
`app.tools.schemas.ALL_SPINTAX_TOOLS`. The functions do two things:

  1. Coalesce strict-mode null values (e.g. `role=None -> "unknown"`,
     `max_variants=None -> 3`). This keeps the underlying tool modules
     (sense_classifier, syntax_reshuffler, etc.) clean of null-handling.
  2. Bridge sync/async — the synonym-axis WordHippo lookup is the only
     network call, so only that one is genuinely async; the rest just
     delegate to the sync helpers.

Three dispatch helpers, one per runner loop, sit at the bottom:

  - `dispatch_chat(name, args_json: str)` — Chat Completions API
  - `dispatch_responses(name, args_json: str)` — /v1/responses
  - `dispatch_anthropic(name, args: dict)` — Anthropic Messages API

Per critic's blocking note: do NOT unify these. Chat + Responses both pass
JSON-string arguments, but the function-call shapes differ enough that
collapsing them obscures the per-loop edge cases.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List

from app.tools.fingerprint_lint import lint_structure_repetition
from app.tools.lexicon_store import get_approved_synonyms
from app.tools.sense_classifier import classify_word_sense_for_sentence
from app.tools.synonym_scorer import score_synonym_candidates
from app.tools.syntax_block_classifier import classify_sentence_blocks
from app.tools.syntax_family_classifier import classify_syntax_family
from app.tools.syntax_reshuffler import reshape_blocks
from app.tools.wordhippo_client import get_fetcher_async
from app.tools.wordhippo_parser import parse_wordhippo_sections


# ---------------------------------------------------------------------------
# Coalescers — strict-mode nullable args become safe defaults
# ---------------------------------------------------------------------------


def _role_or_unknown(role: str | None) -> str:
    """Map None / missing role to 'unknown' (matches ROLE_VALUES)."""
    return role if role else "unknown"


def _str_or_unknown(value: str | None) -> str:
    return value if value else "unknown"


# ---------------------------------------------------------------------------
# Synonym axis (4 tools)
# ---------------------------------------------------------------------------


async def wordhippo_lookup_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    """Look up a word on WordHippo. context_id=None -> all buckets."""
    word = args["word"]
    context_id = args.get("context_id")  # may be None per strict-mode
    fetcher = await get_fetcher_async()
    raw_html = await fetcher.fetch(word)
    sections = parse_wordhippo_sections(raw_html)
    warnings: List[str] = []
    if not sections:
        warnings.append("No definitions parsed from WordHippo page (empty response or layout change).")

    if context_id is None:
        return {
            "word": word,
            "definition_count": len(sections),
            "definitions": sections,
            "warnings": warnings,
        }

    for section in sections:
        if section["context_id"] == context_id:
            return {
                "word": word,
                "context_id": context_id,
                "word_type": section.get("word_type"),
                "definition": section["definition"],
                "synonyms": section["synonyms"],
                "synonym_count": section["synonym_count"],
                "warnings": warnings,
            }
    return {
        "word": word,
        "context_id": context_id,
        "word_type": None,
        "definition": "",
        "synonyms": [],
        "synonym_count": 0,
        "warnings": warnings + [f"No matching context_id {context_id!r} found in WordHippo response."],
    }


def classify_word_sense_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    return classify_word_sense_for_sentence(
        word=args["word"],
        sentence=args["sentence"],
        role=_role_or_unknown(args.get("role")),
    )


def score_synonym_candidates_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    return score_synonym_candidates(
        source_word=args["source_word"],
        sentence=args["sentence"],
        candidates=args["candidates"],
        role=_role_or_unknown(args.get("role")),
        sense_label=_str_or_unknown(args.get("sense_label")),
    )


def get_pre_approved_synonyms_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    """Return curated lexicon buckets for the source word.

    Returns shape compatible with `lookup_approved_lexicon` plus the
    role/sense_label echoed back per schema contract.
    """
    source_word = args["source_word"]
    role = _role_or_unknown(args.get("role"))
    sense_label = _str_or_unknown(args.get("sense_label"))
    bank = get_approved_synonyms(source_word)
    return {
        "source_word": source_word.strip().lower(),
        "role": role,
        "sense_label": sense_label,
        **bank,
    }


# ---------------------------------------------------------------------------
# Syntax axis (3 tools)
# ---------------------------------------------------------------------------


def classify_sentence_blocks_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    return classify_sentence_blocks(
        sentence=args["sentence"],
        role=_role_or_unknown(args.get("role")),
    )


def identify_syntax_family_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    """Renamed from classify_syntax_family per critic. Same underlying logic."""
    return classify_syntax_family(
        sentence=args["sentence"],
        role=_role_or_unknown(args.get("role")),
    )


def reshape_blocks_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    max_variants = args.get("max_variants")
    if max_variants is None:
        max_variants = 3
    return reshape_blocks(
        sentence=args["sentence"],
        role=_role_or_unknown(args.get("role")),
        source_family=args.get("source_family"),
        target_family=args.get("target_family"),
        max_variants=int(max_variants),
    )


# ---------------------------------------------------------------------------
# Corpus axis (1 tool)
# ---------------------------------------------------------------------------


def lint_structure_repetition_impl(args: Dict[str, Any]) -> Dict[str, Any]:
    return lint_structure_repetition(
        lines=args["lines"],
        role=_role_or_unknown(args.get("role")),
    )


# ---------------------------------------------------------------------------
# Per-loop dispatchers
#
# Each loop has a different way of giving us the tool's arguments:
#   - Chat: tc.function.arguments is a JSON string
#   - Responses: tc.arguments is a JSON string
#   - Anthropic: b.input is already a dict
#
# Per critic: do NOT unify. The shapes are close enough to look identical
# but the edge cases (which fields exist on tc/b, how to extract `name`)
# differ in ways that bite if collapsed.
# ---------------------------------------------------------------------------


# Sync tool implementations indexed by name. Async tools dispatched separately.
_SYNC_IMPLS = {
    "classify_word_sense_for_sentence": classify_word_sense_impl,
    "score_synonym_candidates": score_synonym_candidates_impl,
    "get_pre_approved_synonyms": get_pre_approved_synonyms_impl,
    "classify_sentence_blocks": classify_sentence_blocks_impl,
    "identify_syntax_family": identify_syntax_family_impl,
    "reshape_blocks": reshape_blocks_impl,
    "lint_structure_repetition": lint_structure_repetition_impl,
}


_ASYNC_IMPLS = {
    "wordhippo_lookup": wordhippo_lookup_impl,
}


SPINTAX_TOOL_NAMES = frozenset(_SYNC_IMPLS) | frozenset(_ASYNC_IMPLS)


async def _dispatch_by_name(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Shared inner dispatch — only the arg-extraction path differs per loop."""
    if name in _ASYNC_IMPLS:
        return await _ASYNC_IMPLS[name](args)
    if name in _SYNC_IMPLS:
        return _SYNC_IMPLS[name](args)
    return {
        "error": f"Unknown spintax tool: {name!r}",
        "known_tools": sorted(SPINTAX_TOOL_NAMES),
    }


async def dispatch_chat(name: str, args_json: str) -> Dict[str, Any]:
    """Chat Completions: tool_call.function.arguments is a JSON string."""
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as exc:
        return {"error": f"Could not parse tool args as JSON: {exc}"}
    return await _dispatch_by_name(name, args)


async def dispatch_responses(name: str, args_json: str) -> Dict[str, Any]:
    """Responses API: function_call.arguments is a JSON string."""
    try:
        args = json.loads(args_json) if args_json else {}
    except json.JSONDecodeError as exc:
        return {"error": f"Could not parse tool args as JSON: {exc}"}
    return await _dispatch_by_name(name, args)


async def dispatch_anthropic(name: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """Anthropic Messages: tool_use.input is already a parsed dict."""
    if not isinstance(args, dict):
        return {"error": f"Anthropic tool_use.input was not a dict: {type(args).__name__}"}
    return await _dispatch_by_name(name, args)
