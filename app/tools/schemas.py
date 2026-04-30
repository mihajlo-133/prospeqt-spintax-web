"""OpenAI Chat Completions tool-call schemas for the 8 spintax agent tools.

Each entry is the canonical Chat-Completions shape:

    {
        "type": "function",
        "function": {
            "name": ...,
            "description": ...,
            "parameters": { JSON-Schema, strict-mode },
        },
    }

`spintax_runner.py` converts these to the Responses-API shape via
`_to_responses_tool()` and to the Anthropic shape via `_to_anthropic_tool()`.
Both converters expect the chat shape; do NOT pre-convert here.

## Strict-mode invariants

Responses-API tool calls run with `strict: True`. That requires:

  1. `additionalProperties: False` on every parameters block.
  2. EVERY property listed in `required`.
  3. Optional fields modeled as `{"type": ["string", "null"]}` (or
     `["integer", "null"]`, etc.) — the LLM must pass `null` to skip.

Per-loop dispatchers in `spintax_runner.py` coalesce `None -> "unknown"`
for `role`-style enum fields BEFORE calling the underlying tool function,
so the tool implementations stay clean of null-handling logic.

## Description style

Front-load the use case in the first sentence. The model dispatches
based on description scanning, so the discriminator must be unmissable
within the first ~80 chars. Examples:

  - "Score candidate synonyms YOU ALREADY HAVE..."  (input expected)
  - "Retrieve pre-cleared synonyms..."              (no input needed)
  - "Look up a word on WordHippo..."                (network call)
"""

from __future__ import annotations

from typing import Any, Dict, List


# Reusable enum: outbound-copy roles. ROLE_VALUES in app/tools/constants.py
# uses {"opener", "body", "proof", "cta", "ps", "unknown"}; the schema
# exposes the same set so callers can't pass values the classifier won't
# accept. "unknown" is the safe default the dispatcher coalesces None to.
_ROLE_ENUM: List[str] = ["opener", "body", "proof", "cta", "ps", "unknown"]


def _role_field(description: str) -> Dict[str, Any]:
    """Build a strict-mode role field. None coalesces to 'unknown' downstream."""
    return {
        "type": ["string", "null"],
        "enum": _ROLE_ENUM + [None],  # type: ignore[list-item]
        "description": description,
    }


def _optional_str(description: str) -> Dict[str, Any]:
    return {
        "type": ["string", "null"],
        "description": description,
    }


def _optional_int(description: str, default_hint: int | None = None) -> Dict[str, Any]:
    desc = description
    if default_hint is not None:
        desc = f"{description} If null, defaults to {default_hint}."
    return {
        "type": ["integer", "null"],
        "description": desc,
    }


# ---------------------------------------------------------------------------
# Synonym axis (4 tools)
# ---------------------------------------------------------------------------

WORDHIPPO_LOOKUP_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "wordhippo_lookup",
        "description": (
            "Look up a word on WordHippo. Two modes: (a) pass `context_id=null` "
            "to discover all definition buckets so you can pick a sense; (b) pass "
            'a specific `context_id` (e.g. "C0-7") to fetch synonyms for that '
            "bucket. Typical flow: call once with context_id=null, choose a bucket "
            "from the response, then call again with that context_id. Network call "
            "via Spider; prefer `get_pre_approved_synonyms` first for common words."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "word": {
                    "type": "string",
                    "description": "The single English word to look up. Lowercased internally.",
                },
                "context_id": _optional_str(
                    'WordHippo definition-bucket id (e.g. "C0-7"). '
                    "Pass null to retrieve all buckets in discovery mode."
                ),
            },
            "required": ["word", "context_id"],
            "additionalProperties": False,
        },
    },
}


CLASSIFY_WORD_SENSE_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "classify_word_sense_for_sentence",
        "description": (
            "Classify which practical sense a target word carries inside a "
            "sentence (e.g. visual_observation vs data_observation vs "
            "send_share_cta). Returns a sense_label and recommended WordHippo "
            "context_ids. Use BEFORE `wordhippo_lookup` so you can target the "
            "right bucket on the first network call."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "word": {
                    "type": "string",
                    "description": "The word whose sense is being classified.",
                },
                "sentence": {
                    "type": "string",
                    "description": "The full sentence the word appears in. Drives the heuristics.",
                },
                "role": _role_field(
                    "Outbound-copy role of the sentence. Influences sense scoring. "
                    "Pass null if unknown — dispatcher coalesces to 'unknown'."
                ),
            },
            "required": ["word", "sentence", "role"],
            "additionalProperties": False,
        },
    },
}


SCORE_SYNONYM_CANDIDATES_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "score_synonym_candidates",
        "description": (
            "Score candidate synonyms YOU ALREADY HAVE against a sentence. "
            "Returns per-candidate final_score (0-1), six scoring dimensions, "
            "and a status (approved | candidate_review | rejected). Use AFTER "
            "you have a candidate list (e.g. from wordhippo_lookup or your own "
            "thinking) — do NOT call this to discover candidates."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_word": {
                    "type": "string",
                    "description": "The original word the candidates would replace.",
                },
                "sentence": {
                    "type": "string",
                    "description": "The full sentence the word appears in.",
                },
                "candidates": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Synonyms to score. Must be non-empty.",
                },
                "role": _role_field(
                    "Outbound-copy role. Influences placement_fit. Null -> 'unknown'."
                ),
                "sense_label": _optional_str(
                    'Sense label (e.g. "data_observation") from '
                    "classify_word_sense_for_sentence. Null -> 'unknown'."
                ),
            },
            "required": ["source_word", "sentence", "candidates", "role", "sense_label"],
            "additionalProperties": False,
        },
    },
}


GET_PRE_APPROVED_SYNONYMS_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_pre_approved_synonyms",
        "description": (
            "Retrieve pre-cleared synonyms from the static lexicon. Free, "
            "instant, no network call. Call this BEFORE `wordhippo_lookup` "
            "when the source word is common (saw, send, show, help) — saves a "
            "Spider API hit. Returns three buckets: approved, candidate_review, "
            "rejected. Empty buckets mean no curated entries for that word yet."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "source_word": {
                    "type": "string",
                    "description": "The word to look up in the curated lexicon.",
                },
                "role": _role_field(
                    "Outbound-copy role context. Returned in payload for caller "
                    "convenience. Null -> 'unknown'."
                ),
                "sense_label": _optional_str(
                    "Optional sense label for downstream cross-reference. Null "
                    "-> 'unknown'. Does not currently filter results — lexicon "
                    "is shared across senses for a given source_word."
                ),
            },
            "required": ["source_word", "role", "sense_label"],
            "additionalProperties": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Syntax axis (3 tools)
# ---------------------------------------------------------------------------

CLASSIFY_SENTENCE_BLOCKS_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "classify_sentence_blocks",
        "description": (
            "Extract structural blocks from a sentence (greeting, "
            "observation_verb, evidence_object, source_phrase, time_phrase, "
            "etc.). Returns a block-level decomposition with movability and "
            "required flags for fine-grained editing. Use this when you need "
            "to surgically swap or move parts of a sentence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sentence": {
                    "type": "string",
                    "description": "The sentence to decompose into blocks.",
                },
                "role": _role_field(
                    "Outbound-copy role. Drives which block extractor runs "
                    "(opener vs cta vs proof). Null -> 'unknown' (generic fallback)."
                ),
            },
            "required": ["sentence", "role"],
            "additionalProperties": False,
        },
    },
}


IDENTIFY_SYNTAX_FAMILY_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "identify_syntax_family",
        "description": (
            "Tag a sentence with its reusable structural family (e.g. "
            "cta_curiosity, proof_helper_led, evidence_first_observation). "
            "Returns one family label, alternate families, and confidence. "
            "Different from `classify_sentence_blocks`: this is a single "
            "high-level tag, not a block decomposition."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sentence": {
                    "type": "string",
                    "description": "The sentence to tag with a family label.",
                },
                "role": _role_field(
                    "Outbound-copy role. Required to disambiguate family namespaces "
                    "(cta_* vs opener_* vs proof_*). Null -> 'unknown'."
                ),
            },
            "required": ["sentence", "role"],
            "additionalProperties": False,
        },
    },
}


RESHAPE_BLOCKS_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "reshape_blocks",
        "description": (
            "Generate safe alternate renderings of a sentence. Pass `target_family` "
            'to steer toward a specific structure (e.g. "evidence_first_observation"); '
            "pass null to get default safe reshufflings of the current family. "
            "Variants preserve proposition meaning, role appropriateness, and "
            "{{variable}} integrity. Use after `identify_syntax_family` so you know "
            "what target_family options are reachable."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "sentence": {
                    "type": "string",
                    "description": "The sentence to reshape.",
                },
                "role": _role_field(
                    "Outbound-copy role. Drives which reshape branch fires. "
                    "Null -> 'unknown' (no-op)."
                ),
                "source_family": _optional_str(
                    "Current family of the sentence. If null, the tool will "
                    "auto-detect via identify_syntax_family internally."
                ),
                "target_family": _optional_str(
                    "Desired family for the variant. If null, defaults to source_family "
                    "(safe within-family reshufflings only)."
                ),
                "max_variants": _optional_int(
                    "Cap on returned variant count. Range 1-10 recommended.",
                    default_hint=3,
                ),
            },
            "required": [
                "sentence",
                "role",
                "source_family",
                "target_family",
                "max_variants",
            ],
            "additionalProperties": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Corpus axis (1 tool)
# ---------------------------------------------------------------------------

LINT_STRUCTURE_REPETITION_TOOL: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "lint_structure_repetition",
        "description": (
            "Lint a list of sentence variants for corpus-level repetition. "
            "Returns family concentration ratios, repeated-line-starter "
            "warnings, and an overall risk_level (low | medium | high). Run "
            "this after generating multiple variants to catch the case where "
            "all your 'different' versions actually share the same family."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "The sentences/variants to compare.",
                },
                "role": _role_field(
                    "Outbound-copy role applied to every line during family "
                    "tagging. Null -> 'unknown'."
                ),
            },
            "required": ["lines", "role"],
            "additionalProperties": False,
        },
    },
}


# ---------------------------------------------------------------------------
# Aggregate
# ---------------------------------------------------------------------------

# Order matters for cost-gradient hinting: cheapest/lookup first, classifiers
# next, network call middle, generators last. The model reads tool lists
# top-to-bottom when scanning; cheap-then-expensive nudges right ordering.
ALL_SPINTAX_TOOLS: List[Dict[str, Any]] = [
    GET_PRE_APPROVED_SYNONYMS_TOOL,  # free, no network
    CLASSIFY_WORD_SENSE_TOOL,  # free, no network
    CLASSIFY_SENTENCE_BLOCKS_TOOL,  # free, no network
    IDENTIFY_SYNTAX_FAMILY_TOOL,  # free, no network
    WORDHIPPO_LOOKUP_TOOL,  # network: Spider call
    SCORE_SYNONYM_CANDIDATES_TOOL,  # free, post-WordHippo validation
    RESHAPE_BLOCKS_TOOL,  # free, generator
    LINT_STRUCTURE_REPETITION_TOOL,  # free, corpus-level QA
]


# Lookup table for per-loop dispatchers (chat / responses / anthropic).
# Maps tool name -> (callable, expected-args-keys) for runtime dispatch.
# The runner imports this and selects on `tool_name`.
TOOL_NAMES: List[str] = [t["function"]["name"] for t in ALL_SPINTAX_TOOLS]
