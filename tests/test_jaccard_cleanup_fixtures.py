"""Test fixtures and smoke verification for V3 Jaccard cleanup phase.

Phase 3 prep per V3_DRIFT_JACCARD_AND_V2_RETRY_SPEC.md. This file:
  1. Defines constants for the Fox & Farmer block-6 failure scenario
     (drift_retry produced V4/V5 with Jaccard distance 0.0 vs V1 - pure
     word reorder).
  2. Defines mock sub-call response fixtures for the upcoming cleanup
     loop tests (good cleanup, partial cleanup, length-broken cleanup).
  3. Runs SMOKE tests that verify the broken fixture actually triggers
     the diversity gate (so we know our fixture is realistic before any
     production code lands).
  4. Verifies the "good cleanup" fixture would in fact resolve the
     violations.

When Workstream 1 (per-block Jaccard cleanup phase) lands, additional
tests for the cleanup helpers go below the smoke tests.

Spec: /Users/mihajlo/Desktop/prospeqt-spintax-web/V3_DRIFT_JACCARD_AND_V2_RETRY_SPEC.md
Source case: gpt-5.5 job 06baee12 block 6 - V4 and V5 share 100% of
content words with V1, just reordered.
"""

import pytest

# =============================================================================
# FIXTURE A: The broken Fox & Farmer block (block 6 from gpt-5.5 job 06baee12)
# =============================================================================
# Block 6 is the p.s. line. After drift_retry exited, the variants were:
#   V1: p.s. we helped Fox & Farmer block 3 critiques from going public last month
#   V2: ...keep 3...                                  (one word swap)
#   V3: ...stop 3...                                  (one word swap)
#   V4: p.s. last month, we helped Fox & Farmer block 3 critiques from going public
#                                                     (pure word reorder)
#   V5: p.s. we helped block 3 Fox & Farmer critiques from going public last month
#                                                     (pure word reorder)
# Expected per-pair Jaccard distances vs V1:
#   V2: ~0.13 (one content word swap: "block" -> "keep")
#   V3: ~0.13 (one content word swap: "block" -> "stop")
#   V4: 0.00  (same word set, reordered)
#   V5: 0.00  (same word set, reordered)
# Block-avg ~ 0.065 -> well below BLOCK_AVG_FLOOR (0.30); fails diversity gate.

_BROKEN_BLOCK_VARS = [
    "p.s. we helped Fox & Farmer block 3 critiques from going public last month",
    "p.s. we helped Fox & Farmer keep 3 critiques from going public last month",
    "p.s. we helped Fox & Farmer stop 3 critiques from going public last month",
    "p.s. last month, we helped Fox & Farmer block 3 critiques from going public",
    "p.s. we helped block 3 Fox & Farmer critiques from going public last month",
]

# A clean block to pair with the broken one in a 2-block body. This block
# has real word substitution (synonyms, structural rewrites) so QA passes.
_CLEAN_BLOCK_VARS = [
    "We help {{practice_area}} firms with strong ratings stay on top "
    "by texting clients for reviews after their case is closed.",
    "Our team supports {{practice_area}} practices already rated highly "
    "by reaching out to clients via SMS once their matter wraps.",
    "Top-rated {{practice_area}} businesses keep their lead through our "
    "post-case text outreach asking happy customers for feedback.",
    "Once a case wraps, we ping clients on their phone for reviews - that "
    "is how leading {{practice_area}} groups protect their score.",
    "For {{practice_area}} practices already winning on reputation, we "
    "automate the post-case ask via texted review prompts.",
]


def _build_instantly_body(blocks: list[list[str]]) -> str:
    """Assemble a list of [v1..v5] block tuples into Instantly-format spintax."""
    parts = []
    for vars_ in blocks:
        assert len(vars_) == 5, "each block must have exactly 5 variations"
        parts.append("{{RANDOM | " + " | ".join(vars_) + "}}")
    return "\n\n".join(parts)


# Two-block body: clean block + broken block. Used by smoke tests below.
BROKEN_BODY_TWO_BLOCKS = _build_instantly_body([_CLEAN_BLOCK_VARS, _BROKEN_BLOCK_VARS])

# Original plain body (what was input to spintax) - needed for qa() input.
PLAIN_BODY_TWO_BLOCKS = (
    "We help {{practice_area}} firms with strong ratings stay on top "
    "by texting clients for reviews after their case is closed.\n\n"
    "p.s. we helped Fox & Farmer block 3 critiques from going public last month"
)

# =============================================================================
# FIXTURE B: Mock sub-call response - GOOD cleanup of block 6
# =============================================================================
# Real synonym substitution + structural rewrites. Preserves "Fox & Farmer"
# (proper noun) and "3" (factual count). Uses different content words.

GOOD_CLEANUP_RESPONSE = {
    "v2": "p.s. last month Fox & Farmer used us to bury 3 negative reviews silently",
    "v3": "p.s. Fox & Farmer pulled 3 unflattering reviews offline with our help recently",
    "v4": "p.s. our system kept 3 unhappy notes off the public profile of Fox & Farmer in the past 30 days",
    "v5": "p.s. recently, Fox & Farmer prevented 3 unwanted comments from reaching their listing",
    "strategies": ["lexical", "combined", "structural", "lexical"],
}

# =============================================================================
# FIXTURE C: Mock sub-call response - PARTIAL cleanup (still has 0.0 pair)
# =============================================================================
# V2/V3 fixed with synonyms; V4 still uses the same word set as V1 (failed).

PARTIAL_CLEANUP_RESPONSE = {
    "v2": "p.s. last month Fox & Farmer used us to bury 3 negative reviews silently",
    "v3": "p.s. Fox & Farmer pulled 3 unflattering reviews offline with our help recently",
    "v4": "p.s. last month, we helped Fox & Farmer block 3 critiques from going public",
    "v5": "p.s. recently, Fox & Farmer prevented 3 unwanted comments from reaching their listing",
    "strategies": ["lexical", "combined", "structural", "lexical"],
}

# =============================================================================
# FIXTURE D: Mock sub-call response - LENGTH-BROKEN cleanup
# =============================================================================
# Diverse content words but V2 is way too long (>12% over V1).
# V1 is 73 chars. Outer band cap is 73 + 12% = ~82 chars. V2 below is 130 chars.

LENGTH_BROKEN_CLEANUP_RESPONSE = {
    "v2": (
        "p.s. just so you know, in the last 30 days we successfully kept 3 less-than-flattering "
        "reviews of Fox & Farmer from ever reaching their public Google listing for everyone to see"
    ),
    "v3": "p.s. Fox & Farmer pulled 3 unflattering reviews offline with our help recently",
    "v4": "p.s. our system kept 3 unhappy notes off the public profile of Fox & Farmer in the past 30 days",
    "v5": "p.s. recently, Fox & Farmer prevented 3 unwanted comments from reaching their listing",
    "strategies": ["lexical", "combined", "structural", "lexical"],
}

# =============================================================================
# SMOKE TESTS - verify the fixtures are realistically broken / fixed
# =============================================================================


def test_broken_fixture_block6_v4_has_zero_jaccard():
    """V4 reorders V1's words. Tokenized (lowercase, stopwords removed,
    placeholders stripped, len>=3), V1 and V4 must have identical token sets,
    yielding Jaccard distance 0.0. This is the core failure mode V3
    Workstream 1 is meant to fix.
    """
    from app.qa import _diversity_tokens, _jaccard_distance

    v1_tokens = _diversity_tokens(_BROKEN_BLOCK_VARS[0])
    v4_tokens = _diversity_tokens(_BROKEN_BLOCK_VARS[3])
    assert v1_tokens == v4_tokens, (
        f"Expected V1 and V4 to have identical token sets (pure reorder); "
        f"got V1={v1_tokens}, V4={v4_tokens}"
    )
    distance = _jaccard_distance(v1_tokens, v4_tokens)
    assert distance == 0.0, f"Expected 0.0 Jaccard distance, got {distance}"


def test_broken_fixture_block6_v5_has_zero_jaccard():
    """V5 also reorders V1's words. Same expectation as V4."""
    from app.qa import _diversity_tokens, _jaccard_distance

    v1_tokens = _diversity_tokens(_BROKEN_BLOCK_VARS[0])
    v5_tokens = _diversity_tokens(_BROKEN_BLOCK_VARS[4])
    assert v1_tokens == v5_tokens, (
        f"Expected V1 and V5 to have identical token sets (pure reorder); "
        f"got V1={v1_tokens}, V5={v5_tokens}"
    )
    distance = _jaccard_distance(v1_tokens, v5_tokens)
    assert distance == 0.0


def test_broken_fixture_block6_block_avg_below_floor():
    """Block-avg V1<->Vn distance for block 6 must be below BLOCK_AVG_FLOOR
    (0.30). With V4 and V5 at 0.0 and V2/V3 at small values from one word
    swap, the average should be well under 0.30.
    """
    from app.qa import _diversity_tokens, _jaccard_distance, BLOCK_AVG_FLOOR

    v1_tokens = _diversity_tokens(_BROKEN_BLOCK_VARS[0])
    distances = []
    for v in _BROKEN_BLOCK_VARS[1:]:
        d = _jaccard_distance(v1_tokens, _diversity_tokens(v))
        assert d is not None
        distances.append(d)
    block_avg = sum(distances) / len(distances)
    assert block_avg < BLOCK_AVG_FLOOR, (
        f"Block-avg {block_avg:.3f} should be below floor {BLOCK_AVG_FLOOR}; "
        f"distances={[f'{d:.3f}' for d in distances]}"
    )


def test_broken_fixture_clean_block_passes_floors():
    """The clean block we paired with the broken one must pass both
    floors so QA isolates the failure to block 6 only.
    """
    from app.qa import (
        _diversity_tokens, _jaccard_distance,
        BLOCK_AVG_FLOOR, BLOCK_PAIR_FLOOR,
    )

    v1_tokens = _diversity_tokens(_CLEAN_BLOCK_VARS[0])
    distances = []
    for v in _CLEAN_BLOCK_VARS[1:]:
        d = _jaccard_distance(v1_tokens, _diversity_tokens(v))
        assert d is not None
        distances.append(d)
    block_avg = sum(distances) / len(distances)
    assert block_avg >= BLOCK_AVG_FLOOR, (
        f"Clean-block avg {block_avg:.3f} should be at or above "
        f"{BLOCK_AVG_FLOOR}; distances={[f'{d:.3f}' for d in distances]}"
    )
    for i, d in enumerate(distances, start=2):
        assert d >= BLOCK_PAIR_FLOOR, (
            f"Clean-block V{i} distance {d:.3f} below pair-floor "
            f"{BLOCK_PAIR_FLOOR}"
        )


def test_broken_body_qa_flags_only_block6():
    """End-to-end: run qa() against the two-block body. Expect:
      - At least one diversity error mentioning 'block 2' (the broken
        block at body position 2; numbering is 1-indexed in QA messages).
      - No diversity errors mentioning 'block 1' (the clean block).
    """
    from unittest.mock import patch
    from app.qa import qa

    # DIVERSITY_GATE_LEVEL is read at module import; patch the constant directly
    # (matches existing pattern in test_diversity_gate_error_mode_passed_false).
    with patch("app.qa.DIVERSITY_GATE_LEVEL", "error"):
        result = qa(BROKEN_BODY_TWO_BLOCKS, PLAIN_BODY_TWO_BLOCKS, "instantly")

    block1_errors = [
        e for e in result.get("errors", []) if "block 1" in e and "diversity" in e
    ]
    block2_errors = [
        e for e in result.get("errors", []) if "block 2" in e and "diversity" in e
    ]
    assert not block1_errors, (
        f"Clean block (#1) should not have diversity errors; got: {block1_errors}"
    )
    assert block2_errors, (
        f"Broken block (#2) should have diversity errors; full errors: "
        f"{result.get('errors', [])}"
    )

    # Block-avg score for block 2 should be well below 0.30
    scores = result.get("diversity_block_scores", [])
    assert len(scores) == 2, f"expected 2 block scores, got {scores}"
    assert scores[1] is not None and scores[1] < 0.30, (
        f"block 2 score should be below 0.30; got {scores[1]}"
    )


def test_good_cleanup_response_resolves_zero_pairs():
    """The 'good' mock sub-call response, if spliced into block 6, must
    eliminate the 0.0-pair Jaccard issue. Verify each V1<->Vn pair is
    above BLOCK_PAIR_FLOOR.
    """
    from app.qa import _diversity_tokens, _jaccard_distance, BLOCK_PAIR_FLOOR

    v1 = _BROKEN_BLOCK_VARS[0]
    new_variants = [
        GOOD_CLEANUP_RESPONSE["v2"],
        GOOD_CLEANUP_RESPONSE["v3"],
        GOOD_CLEANUP_RESPONSE["v4"],
        GOOD_CLEANUP_RESPONSE["v5"],
    ]
    v1_tokens = _diversity_tokens(v1)
    for i, v in enumerate(new_variants, start=2):
        d = _jaccard_distance(v1_tokens, _diversity_tokens(v))
        assert d is not None and d >= BLOCK_PAIR_FLOOR, (
            f"GOOD_CLEANUP V{i} distance {d} below pair-floor "
            f"{BLOCK_PAIR_FLOOR}"
        )


def test_partial_cleanup_response_still_has_zero_pair():
    """The 'partial' mock response keeps V4 as a word reorder of V1, so
    one pair must still be 0.0 distance. This fixture exists so the
    cleanup loop's 'no improvement' / retry path can be tested.
    """
    from app.qa import _diversity_tokens, _jaccard_distance

    v1_tokens = _diversity_tokens(_BROKEN_BLOCK_VARS[0])
    v4_tokens = _diversity_tokens(PARTIAL_CLEANUP_RESPONSE["v4"])
    assert v4_tokens == v1_tokens, (
        f"PARTIAL_CLEANUP V4 should still be a word reorder of V1 "
        f"(failed cleanup attempt); got V1={v1_tokens}, V4={v4_tokens}"
    )
    d = _jaccard_distance(v1_tokens, v4_tokens)
    assert d == 0.0


def test_length_broken_cleanup_response_v2_outside_outer_band():
    """The 'length-broken' mock response has V2 well outside the 12%
    outer length band. Used to test Hybrid B+D selection rule's hard
    length cap (Workstream 2).
    """
    v1_len = len(_BROKEN_BLOCK_VARS[0])
    v2_len = len(LENGTH_BROKEN_CLEANUP_RESPONSE["v2"])
    # Outer band: V1 +/- 12%, with floor 6 chars
    outer_diff = max(int(round(v1_len * 0.12)), 6)
    band_hi = v1_len + outer_diff
    assert v2_len > band_hi, (
        f"LENGTH_BROKEN V2 length {v2_len} should exceed outer band "
        f"{band_hi} (V1={v1_len}, +12%={outer_diff})"
    )


# =============================================================================
# UNIT TESTS - V3 Workstream 1 helpers
# =============================================================================


def test_extract_preserve_tokens_picks_up_double_brace_placeholders():
    """{{instantly_var}} placeholders must be preserved verbatim."""
    from app.spintax_runner import _extract_preserve_tokens

    v1 = "We help {{practice_area}} firms grow with {{tactic_name}}."
    preserves = _extract_preserve_tokens(v1)
    assert "{{practice_area}}" in preserves
    assert "{{tactic_name}}" in preserves


def test_extract_preserve_tokens_picks_up_emailbison_placeholders():
    """{EMAILBISON_VAR} placeholders must be preserved verbatim."""
    from app.spintax_runner import _extract_preserve_tokens

    v1 = "Hi {FIRSTNAME}, your {COMPANY_NAME} review just landed."
    preserves = _extract_preserve_tokens(v1)
    assert "{FIRSTNAME}" in preserves
    assert "{COMPANY_NAME}" in preserves


def test_extract_preserve_tokens_picks_up_multi_word_proper_nouns():
    """'Fox & Farmer' is a multi-word capitalized phrase; should be preserved
    as a single token.
    """
    from app.spintax_runner import _extract_preserve_tokens

    v1 = "p.s. we helped Fox & Farmer block 3 reviews last month"
    preserves = _extract_preserve_tokens(v1)
    assert any("Fox" in p and "Farmer" in p for p in preserves), (
        f"Expected a 'Fox & Farmer' phrase in preserves; got {preserves}"
    )


def test_extract_preserve_tokens_skips_sentence_initial_caps():
    """The first word of a sentence is often capitalized just for
    orthography (e.g. 'We', 'The'), not because it's a proper noun. The
    heuristic should not flag it as a preserve.
    """
    from app.spintax_runner import _extract_preserve_tokens

    v1 = "We help firms grow."
    preserves = _extract_preserve_tokens(v1)
    assert "We" not in preserves, (
        f"Sentence-initial 'We' should not be preserved; got {preserves}"
    )


def test_compute_jaccard_failing_blocks_empty_qa_result():
    """Empty/missing fields return empty list (no crash)."""
    from app.spintax_runner import compute_jaccard_failing_blocks

    assert compute_jaccard_failing_blocks({}) == []
    assert compute_jaccard_failing_blocks({"diversity_block_scores": []}) == []


def test_compute_jaccard_failing_blocks_flags_low_block_avg():
    """A block with avg below BLOCK_AVG_FLOOR (0.30) is flagged."""
    from app.spintax_runner import compute_jaccard_failing_blocks

    qa_result = {
        "diversity_block_scores": [0.5, 0.10, 0.4],
        "diversity_pair_distances": [
            [0.5, 0.5, 0.5, 0.5],
            [0.10, 0.10, 0.10, 0.10],
            [0.4, 0.4, 0.4, 0.4],
        ],
    }
    assert compute_jaccard_failing_blocks(qa_result) == [1]


def test_compute_jaccard_failing_blocks_flags_zero_pair_when_avg_passes():
    """Block 2 has block-avg=0.55 (above floor) but one pair at 0.0
    (below pair-floor 0.20). Must be flagged - this is the Fox & Farmer
    block 6 failure mode.
    """
    from app.spintax_runner import compute_jaccard_failing_blocks

    qa_result = {
        "diversity_block_scores": [0.5, 0.55, 0.4],
        "diversity_pair_distances": [
            [0.5, 0.5, 0.5, 0.5],
            [0.7, 0.7, 0.0, 0.8],  # one zero pair, but avg passes
            [0.4, 0.4, 0.4, 0.4],
        ],
    }
    assert compute_jaccard_failing_blocks(qa_result) == [1]


def test_compute_jaccard_failing_blocks_skips_none_scores():
    """Greeting/short blocks have score=None and must be skipped."""
    from app.spintax_runner import compute_jaccard_failing_blocks

    qa_result = {
        "diversity_block_scores": [None, 0.5, None, 0.10],
        "diversity_pair_distances": [
            [],
            [0.5, 0.5, 0.5, 0.5],
            [],
            [0.10, 0.10, 0.10, 0.10],
        ],
    }
    assert compute_jaccard_failing_blocks(qa_result) == [3]


def test_compute_jaccard_failing_blocks_passes_clean_block():
    """Block with avg above floor AND all pairs above pair-floor: not flagged."""
    from app.spintax_runner import compute_jaccard_failing_blocks

    qa_result = {
        "diversity_block_scores": [0.55, 0.65],
        "diversity_pair_distances": [
            [0.4, 0.5, 0.6, 0.7],
            [0.5, 0.7, 0.7, 0.8],
        ],
    }
    assert compute_jaccard_failing_blocks(qa_result) == []


def test_compute_jaccard_failing_blocks_on_broken_fixture():
    """End-to-end: feed BROKEN_BODY_TWO_BLOCKS through qa() and
    compute_jaccard_failing_blocks. Should flag exactly block index 1
    (the broken Fox & Farmer block).
    """
    from unittest.mock import patch
    from app.qa import qa
    from app.spintax_runner import compute_jaccard_failing_blocks

    # Run at warning level too: the helper must work regardless of gate level.
    with patch("app.qa.DIVERSITY_GATE_LEVEL", "warning"):
        result = qa(BROKEN_BODY_TWO_BLOCKS, PLAIN_BODY_TWO_BLOCKS, "instantly")
    failing = compute_jaccard_failing_blocks(result)
    assert failing == [1], (
        f"Expected only the broken Fox & Farmer block (idx 1); got {failing}"
    )


def test_build_jaccard_cleanup_prompt_includes_v1_verbatim():
    """The prompt must show V1 verbatim so the model can preserve it."""
    from app.spintax_runner import _build_jaccard_cleanup_prompt

    v1 = _BROKEN_BLOCK_VARS[0]
    variants = _BROKEN_BLOCK_VARS[1:]
    prompt = _build_jaccard_cleanup_prompt(
        block_v1=v1,
        block_variants=variants,
        pair_distances=[0.13, 0.13, 0.0, 0.0],
        block_position=2,
        platform="instantly",
    )
    assert v1 in prompt
    assert "preserved word-for-word" in prompt


def test_build_jaccard_cleanup_prompt_includes_overlap_words():
    """The prompt must list the specific overlap words to swap."""
    from app.spintax_runner import _build_jaccard_cleanup_prompt

    v1 = _BROKEN_BLOCK_VARS[0]
    variants = _BROKEN_BLOCK_VARS[1:]
    prompt = _build_jaccard_cleanup_prompt(
        block_v1=v1,
        block_variants=variants,
        pair_distances=[0.13, 0.13, 0.0, 0.0],
        block_position=2,
        platform="instantly",
    )
    # 'critiques' is a content word shared by V1 and all failing variants
    assert '"critiques"' in prompt or "critiques" in prompt


def test_build_jaccard_cleanup_prompt_includes_preserve_phrase():
    """'Fox & Farmer' must appear in the preserve list section."""
    from app.spintax_runner import _build_jaccard_cleanup_prompt

    v1 = _BROKEN_BLOCK_VARS[0]
    variants = _BROKEN_BLOCK_VARS[1:]
    prompt = _build_jaccard_cleanup_prompt(
        block_v1=v1,
        block_variants=variants,
        pair_distances=[0.13, 0.13, 0.0, 0.0],
        block_position=2,
        platform="instantly",
    )
    # The preserve list section explicitly names the proper-noun phrase
    assert "Fox & Farmer" in prompt
    # And the section header is present
    assert "PRESERVE-LIST" in prompt


def test_build_jaccard_cleanup_prompt_includes_length_band():
    """The prompt must specify a concrete character band."""
    from app.spintax_runner import _build_jaccard_cleanup_prompt

    v1 = _BROKEN_BLOCK_VARS[0]
    v1_len = len(v1)
    prompt = _build_jaccard_cleanup_prompt(
        block_v1=v1,
        block_variants=_BROKEN_BLOCK_VARS[1:],
        pair_distances=[0.13, 0.13, 0.0, 0.0],
        block_position=2,
        platform="instantly",
        tolerance=0.05,
        tolerance_floor=3,
    )
    # Match the cleanup helper: integer floor (truncation), not round.
    # See spintax_runner.py:_build_jaccard_cleanup_prompt.
    allowed = max(int(v1_len * 0.05), 3)
    band_lo = v1_len - allowed
    band_hi = v1_len + allowed
    assert f"between {band_lo} and {band_hi} characters" in prompt


def test_build_jaccard_cleanup_prompt_flags_failing_pairs():
    """V4/V5 with distance < pair-floor must be marked '<-- REWRITE'."""
    from app.spintax_runner import _build_jaccard_cleanup_prompt

    prompt = _build_jaccard_cleanup_prompt(
        block_v1=_BROKEN_BLOCK_VARS[0],
        block_variants=_BROKEN_BLOCK_VARS[1:],
        pair_distances=[0.13, 0.13, 0.0, 0.0],
        block_position=2,
        platform="instantly",
    )
    # V4 and V5 are below pair-floor; V2 and V3 are not
    rewrite_lines = [
        line for line in prompt.split("\n") if "<-- REWRITE" in line
    ]
    assert any("V4" in line for line in rewrite_lines)
    assert any("V5" in line for line in rewrite_lines)
    # V2 / V3 should not be flagged for rewrite (distance 0.13 still above 0.0
    # but below pair-floor 0.20 — these too should be flagged)
    # Actually 0.13 < 0.20 so they SHOULD be flagged. Verify all 4 are.
    assert any("V2" in line for line in rewrite_lines)
    assert any("V3" in line for line in rewrite_lines)


def test_build_jaccard_cleanup_prompt_includes_register_guidance():
    """Prompt must instruct the model to match V1's register and avoid
    whimsical/casual synonyms in professional copy. See Phase 1 register
    fix (Fox & Farmer feedback: 'cheerful'/'upbeat' clients felt off)."""
    from app.spintax_runner import _build_jaccard_cleanup_prompt

    prompt = _build_jaccard_cleanup_prompt(
        block_v1=_BROKEN_BLOCK_VARS[0],
        block_variants=_BROKEN_BLOCK_VARS[1:],
        pair_distances=[0.13, 0.13, 0.0, 0.0],
        block_position=2,
        platform="instantly",
    )
    assert "register" in prompt.lower()
    # Names the specific bad synonyms we saw in production.
    assert "cheerful" in prompt.lower()
    assert "upbeat" in prompt.lower()


def test_build_jaccard_cleanup_prompt_includes_domain_noun_lock():
    """Prompt must instruct the model to keep V1's domain nouns (clients,
    patients, etc.) intact across V2-V5. See Phase 1 noun-lock fix."""
    from app.spintax_runner import _build_jaccard_cleanup_prompt

    prompt = _build_jaccard_cleanup_prompt(
        block_v1=_BROKEN_BLOCK_VARS[0],
        block_variants=_BROKEN_BLOCK_VARS[1:],
        pair_distances=[0.13, 0.13, 0.0, 0.0],
        block_position=2,
        platform="instantly",
    )
    # 'clients' and 'customers' must be named explicitly so the model
    # understands what swap NOT to make.
    assert "clients" in prompt.lower()
    assert "customers" in prompt.lower()
    assert "domain noun" in prompt.lower() or "lock" in prompt.lower()


def test_build_jaccard_cleanup_prompt_includes_structural_variation_cue():
    """Prompt must tell the model it can vary structure rather than every
    word. See Phase 1 structural variation fix - 'p.s. last month we
    helped...' style rearrangements should be OK."""
    from app.spintax_runner import _build_jaccard_cleanup_prompt

    prompt = _build_jaccard_cleanup_prompt(
        block_v1=_BROKEN_BLOCK_VARS[0],
        block_variants=_BROKEN_BLOCK_VARS[1:],
        pair_distances=[0.13, 0.13, 0.0, 0.0],
        block_position=2,
        platform="instantly",
    )
    # Must explicitly relax "change every word" pressure.
    p = prompt.lower()
    assert "structure" in p
    assert "do not need to change every word" in p or "not 1.0" in p


# =============================================================================
# INTEGRATION TESTS - splice + qa() round-trip
# =============================================================================


def _splice_block(spintax_body: str, block_idx: int, new_inner: str) -> str:
    """Helper: splice a new inner block into an Instantly spintax body.

    Mirrors the splice path in spintax_runner.py:run() (extract_blocks +
    reassemble) so integration tests can verify the round-trip without
    needing the full LLM machinery.
    """
    from app.lint import reassemble
    return reassemble(spintax_body, {block_idx: new_inner}, "instantly")


def test_good_cleanup_splice_resolves_diversity_violations():
    """Integration: splicing GOOD_CLEANUP_RESPONSE into the broken block 6
    of BROKEN_BODY_TWO_BLOCKS produces a body where qa() reports no
    diversity violations on either block.

    This tests the splice round-trip end-to-end without needing to mock
    the LLM client - the Jaccard cleanup phase reduces to:
      1. Build new inner with V1 + parsed['v2'..'v5']
      2. Reassemble body
      3. qa() the result
    """
    from unittest.mock import patch
    from app.qa import qa

    v1 = _BROKEN_BLOCK_VARS[0]
    new_inner = (
        f" {v1} | {GOOD_CLEANUP_RESPONSE['v2']} | "
        f"{GOOD_CLEANUP_RESPONSE['v3']} | {GOOD_CLEANUP_RESPONSE['v4']} | "
        f"{GOOD_CLEANUP_RESPONSE['v5']}"
    )
    fixed_body = _splice_block(BROKEN_BODY_TWO_BLOCKS, 1, new_inner)

    with patch("app.qa.DIVERSITY_GATE_LEVEL", "error"):
        result = qa(fixed_body, PLAIN_BODY_TWO_BLOCKS, "instantly")

    diversity_errors = [
        e for e in result.get("errors", [])
        if "diversity" in e
    ]
    assert not diversity_errors, (
        f"After GOOD_CLEANUP splice, no diversity errors expected; "
        f"got: {diversity_errors}"
    )
    # Block 2 score should now be above the floor.
    scores = result.get("diversity_block_scores", [])
    assert scores[1] is not None and scores[1] >= 0.30, (
        f"block 2 score after splice should be >=0.30; got {scores[1]}"
    )


def test_partial_cleanup_splice_keeps_zero_pair_violation():
    """Integration: splicing PARTIAL_CLEANUP_RESPONSE (V4 still a word
    reorder) leaves the block 2 zero-pair violation intact. This is the
    'cleanup didn't help, retry next iteration or hand off to V2' path.
    """
    from unittest.mock import patch
    from app.qa import qa
    from app.spintax_runner import compute_jaccard_failing_blocks

    v1 = _BROKEN_BLOCK_VARS[0]
    new_inner = (
        f" {v1} | {PARTIAL_CLEANUP_RESPONSE['v2']} | "
        f"{PARTIAL_CLEANUP_RESPONSE['v3']} | "
        f"{PARTIAL_CLEANUP_RESPONSE['v4']} | "
        f"{PARTIAL_CLEANUP_RESPONSE['v5']}"
    )
    half_fixed_body = _splice_block(BROKEN_BODY_TWO_BLOCKS, 1, new_inner)

    with patch("app.qa.DIVERSITY_GATE_LEVEL", "error"):
        result = qa(half_fixed_body, PLAIN_BODY_TWO_BLOCKS, "instantly")

    # Block 2 still has a zero-pair (V4 unchanged), so cleanup phase should
    # still flag it as failing on the next iteration.
    failing = compute_jaccard_failing_blocks(result)
    assert 1 in failing, (
        f"Block 1 (0-indexed) should still be failing after partial "
        f"cleanup splice (V4 = pure word reorder); got failing={failing}"
    )


def test_cleanup_phase_attached_to_result_when_drift_clean():
    """Sanity: when no Jaccard violations exist (clean drift), the
    cleanup phase is recorded with fired=False and an empty sub-call list.
    Verifies the diagnostics dataclass is wired into SpintaxJobResult.
    """
    from app.jobs import (
        JaccardCleanupDiagnostics,
        SpintaxJobResult,
    )

    diags = JaccardCleanupDiagnostics()
    assert diags.fired is False
    assert diags.sub_calls == []
    assert diags.cleanup_cost_usd == 0.0

    # Verify the field exists on SpintaxJobResult
    result = SpintaxJobResult(
        spintax_body="test",
        jaccard_cleanup_diagnostics=diags,
    )
    assert result.jaccard_cleanup_diagnostics is diags
    assert result.jaccard_cleanup_diagnostics.fired is False


# =============================================================================
# UNIT TESTS - per-API-call timeouts (reasoning-stall defense)
# =============================================================================


def test_api_timeout_constants_under_gunicorn_worker_timeout():
    """The tool-loop API timeout must be strictly below the Render
    gunicorn --timeout (600s) so we fail-fast inside the worker rather
    than being SIGKILL'd. Sub-call timeout must be lower again because
    sub-calls regenerate single paragraphs, not full emails - any stall
    is almost certainly a model hang.
    """
    from app.spintax_runner import (
        TOOL_LOOP_API_TIMEOUT_SEC,
        SUBCALL_API_TIMEOUT_SEC,
    )

    GUNICORN_WORKER_TIMEOUT = 600  # set in Procfile
    assert TOOL_LOOP_API_TIMEOUT_SEC < GUNICORN_WORKER_TIMEOUT, (
        f"TOOL_LOOP_API_TIMEOUT_SEC={TOOL_LOOP_API_TIMEOUT_SEC} must be "
        f"under gunicorn's {GUNICORN_WORKER_TIMEOUT}s worker timeout"
    )
    assert SUBCALL_API_TIMEOUT_SEC < TOOL_LOOP_API_TIMEOUT_SEC, (
        f"SUBCALL_API_TIMEOUT_SEC={SUBCALL_API_TIMEOUT_SEC} must be tighter "
        f"than tool-loop timeout ({TOOL_LOOP_API_TIMEOUT_SEC}s)"
    )


@pytest.mark.asyncio
async def test_subcall_timeout_fires_on_hang():
    """Verify _run_per_block_revision_subcall raises asyncio.TimeoutError
    when the underlying client.responses.create() never returns. Mocks the
    OpenAI client with an awaitable that sleeps forever; asyncio.wait_for
    inside the helper must abort it.

    This is the load-bearing defense against the o3/gpt-5.5-pro reasoning
    stall (task #19): TCP connection ESTABLISHED, zero tokens streamed,
    SDK timeout extended for reasoning models. Without this wrap, a stalled
    call hangs the worker until gunicorn SIGKILLs it 10 minutes later.
    """
    import asyncio
    from unittest.mock import AsyncMock, MagicMock, patch
    from app.spintax_runner import _run_per_block_revision_subcall

    # Mock client whose responses.create() awaits forever.
    fake_client = MagicMock()

    async def _hang(*args, **kwargs):
        await asyncio.sleep(3600)
        return MagicMock()

    fake_client.responses = MagicMock()
    fake_client.responses.create = _hang
    fake_client.chat = MagicMock()
    fake_client.chat.completions = MagicMock()
    fake_client.chat.completions.create = _hang

    on_call = MagicMock()

    # Patch the timeout to 0.5s so the test runs fast.
    with patch("app.spintax_runner.SUBCALL_API_TIMEOUT_SEC", 0.5):
        # Force the chat-completions branch (use a model not in
        # RESPONSES_MODELS or ANTHROPIC_MODELS - "o3" qualifies).
        with pytest.raises(asyncio.TimeoutError):
            await _run_per_block_revision_subcall(
                fake_client,
                model="o3",
                prompt="ignored",
                on_api_call=on_call,
            )
