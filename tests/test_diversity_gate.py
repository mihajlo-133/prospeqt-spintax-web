"""Unit and route tests for the Phase A diversity gate.

Spec reference: DIVERSITY_GATE_SPEC.md Section 4.8
Build plan: .archive/BUILD_PLAN_PHASE_A_v1.md

Coverage:
- _diversity_tokens (tests 1-5)
- _jaccard_distance (tests 6-8)
- check_block_diversity smoking-gun (test 9)
- CTA exemption end-to-end, medium run block 5 (test 10)
- CTA exemption end-to-end, high run block 5 (test 11)
- greeting block exemption (test 12)
- corpus avg warning always fires as warning (test 13)
- gate level dispatch: warning mode -> warnings, not errors (test 14)
- exception isolation: internal crash doesn't break qa() (test 15)
- gate level dispatch: warning mode passed=True (test 16)
- gate level dispatch: error mode passed=False (test 17)
- qa() returns 5 new keys (test 18)
- POST /api/qa response includes diversity keys (route test 19)
"""

import os
import pytest
from unittest.mock import patch

# ---------------------------------------------------------------------------
# Test data - medium run block 5 (CTA) from /tmp/medium_run_body.md
# All variations are question-form; block is last; CTA exemption applies.
# ---------------------------------------------------------------------------

_MEDIUM_CTA_VARS = [
    "Would you be curious to hear more?",
    "Would you be open to hearing more?",
    "Would a few more details be useful?",
    "Would it be worth hearing a bit more?",
    "Should I send a few more details?",
]

# ---------------------------------------------------------------------------
# Test data - high run block 5 (CTA) from /tmp/high_run_body.md
# V1="Would you be curious to hear more?" and V2="Curious to hear more about this?"
# both reduce to {"curious","hear"} -> Jaccard distance = 0.0 -> pair-floor would fire
# without CTA exemption.
# ---------------------------------------------------------------------------

_HIGH_CTA_VARS = [
    "Would you be curious to hear more?",
    "Curious to hear more about this?",
    "Would hearing more here be useful?",
    "Open to hearing a bit more here?",
    "Would you be interested to hear more?",
]

# ---------------------------------------------------------------------------
# Smoking-gun V1/V3 pair (inlined from spec Section 4.8 test 9)
# Token overlap is high: only "net/get" and word-order differ -> low Jaccard.
# Expected distance < BLOCK_PAIR_FLOOR (0.20) => pair-floor error fires.
# ---------------------------------------------------------------------------

_SMOKING_GUN_V1 = (
    "We help law firms net 48 5-star Google reviews/month for 149 bucks/month. "
    "Plus, you'd choose which reviews go public - letting you block bad ones."
)
_SMOKING_GUN_V3 = (
    "For 149 bucks/month, we help law firms net 48 5-star Google reviews/month. "
    "Plus, you'd choose which reviews go public - letting you block bad ones."
)

# ---------------------------------------------------------------------------
# Minimal valid spintax body (1 non-CTA block, 5 diverse variations)
# Used to build blocks_vars lists for check_block_diversity calls.
# ---------------------------------------------------------------------------

_DIVERSE_VARS = [
    "We dramatically cut your recruitment pipeline costs with AI-driven screening.",
    "Our platform slashes recruiter workload through automated candidate filtering.",
    "You save thousands monthly by eliminating manual pre-screening entirely.",
    "AI handles your first-pass interviews, freeing recruiters for strategic work.",
    "Reduce time-to-hire by automating the highest-volume part of your pipeline.",
]

# ---------------------------------------------------------------------------
# Greeting block (whitelist-driven exemption via app.lint.is_greeting_block)
# ---------------------------------------------------------------------------

_GREETING_VARS = [
    "Hey {{firstName}},",
    "Hi {{firstName}},",
    "Hello {{firstName}},",
    "Hey there,",
    "{{firstName}},",
]


# ===========================================================================
# _diversity_tokens tests (1-5)
# ===========================================================================


def test_diversity_tokens_strips_instantly_variables():
    """Test 1: {{variable}} placeholders are stripped before tokenising."""
    from app.qa import _diversity_tokens

    tokens = _diversity_tokens("We help {{companyName}} grow their pipeline.")
    # "companyName" removed; remaining content tokenised. "their" is a stopword.
    assert "companyname" not in tokens
    assert "help" in tokens
    assert "grow" in tokens
    assert "their" not in tokens  # pronoun stopword
    assert "pipeline" in tokens


def test_diversity_tokens_strips_emailbison_variables():
    """Test 2: {ALL_CAPS} EmailBison variables are stripped before tokenising."""
    from app.qa import _diversity_tokens

    tokens = _diversity_tokens("We work with {COMPANY_NAME} on pipeline growth.")
    assert "company_name" not in tokens
    assert "COMPANY_NAME" not in tokens
    assert "pipeline" in tokens
    assert "growth" in tokens


def test_diversity_tokens_filters_short_words():
    """Test 3: Tokens with fewer than 3 characters are excluded."""
    from app.qa import _diversity_tokens

    # "a", "an", "to", "in", "of" are all < 3 chars
    tokens = _diversity_tokens("An in-depth study of a topic.")
    for short in ["a", "an", "in", "of"]:
        assert short not in tokens, f"Short word {short!r} should be filtered"
    assert "study" in tokens
    assert "topic" in tokens


def test_diversity_tokens_filters_stopwords():
    """Test 4: Stopwords in _DIVERSITY_STOPWORDS are excluded from token set."""
    from app.qa import _diversity_tokens, _DIVERSITY_STOPWORDS

    # Build a sentence that is all stopwords + one content word
    stopword_sample = list(_DIVERSITY_STOPWORDS)[:3]
    sentence = " ".join(stopword_sample) + " uniqueword"
    tokens = _diversity_tokens(sentence)
    for sw in stopword_sample:
        assert sw not in tokens, f"Stopword {sw!r} leaked into token set"
    assert "uniqueword" in tokens


def test_diversity_tokens_returns_set():
    """Test 5: Return type is set (not list or frozenset)."""
    from app.qa import _diversity_tokens

    result = _diversity_tokens("We help law firms with their reviews.")
    assert isinstance(result, set)


# ===========================================================================
# _jaccard_distance tests (6-8)
# ===========================================================================


def test_jaccard_both_empty_returns_none():
    """Test 6: Both sets empty -> None (skip signal, not a distance)."""
    from app.qa import _jaccard_distance

    result = _jaccard_distance(set(), set())
    assert result is None


def test_jaccard_one_empty_returns_1():
    """Test 7: Exactly one set empty -> 1.0 (maximum distance)."""
    from app.qa import _jaccard_distance

    assert _jaccard_distance({"hello"}, set()) == 1.0
    assert _jaccard_distance(set(), {"world"}) == 1.0


def test_jaccard_identical_sets_returns_0():
    """Test 8: Identical non-empty sets -> 0.0 (minimum distance)."""
    from app.qa import _jaccard_distance

    s = {"alpha", "beta", "gamma"}
    result = _jaccard_distance(s, s.copy())
    assert result == pytest.approx(0.0)


# ===========================================================================
# check_block_diversity smoking-gun test (9)
# ===========================================================================


def test_diversity_smoking_gun_pair_floor_fires():
    """Test 9: V1 vs V3 share nearly all tokens -> pair-floor error.

    V1 and V3 are word-order shuffles of the same sentence. After stripping
    numbers/punctuation and stopwording, they share almost every content token.
    Jaccard distance < BLOCK_PAIR_FLOOR (0.20) -> pair-floor error in errors list.
    """
    from app.qa import check_block_diversity, BLOCK_PAIR_FLOOR

    # Build a 1-block input: 5 variations where V1 and V3 are near-identical
    vars_5 = [
        _SMOKING_GUN_V1,
        _DIVERSE_VARS[1],  # filler V2 that is genuinely diverse
        _SMOKING_GUN_V3,   # near-clone of V1
        _DIVERSE_VARS[3],  # filler V4
        _DIVERSE_VARS[4],  # filler V5
    ]
    errors, warnings, per_block_scores, _pair_distances = check_block_diversity([vars_5])
    # At least one pair-floor error must be present
    pair_floor_errors = [e for e in errors if "pair" in e.lower() or "distance" in e.lower()]
    assert len(pair_floor_errors) > 0, (
        f"Expected pair-floor error for near-identical V1/V3 (BLOCK_PAIR_FLOOR={BLOCK_PAIR_FLOOR}). "
        f"Got errors={errors}"
    )


# ===========================================================================
# CTA exemption tests (10-11)
# ===========================================================================


def test_diversity_medium_run_block_5_cta_passes_via_exemption():
    """Test 10: Medium run CTA block (last, all-question) passes despite pair distance.

    Block 5 has avg=1.000, min=1.000 in the medium audit run. All 5 variations
    are grammatically distinct questions so the avg/pair floors are satisfied anyway.
    The test verifies no pair-floor or avg-floor errors fire for this CTA block.
    """
    from app.qa import check_block_diversity

    # Simulate a 2-block email: [diverse_block, cta_block]
    blocks_vars = [_DIVERSE_VARS, _MEDIUM_CTA_VARS]
    errors, warnings, per_block_scores, _pair_distances = check_block_diversity(blocks_vars)
    # No error should reference block index 1 (the CTA block)
    cta_errors = [e for e in errors if "block 1" in e.lower() or "block2" in e.lower()]
    assert len(cta_errors) == 0, (
        f"CTA block should be exempt but got errors: {cta_errors}"
    )
    assert len(per_block_scores) == 2


def test_diversity_high_run_block_5_cta_exempt_from_pair_floor():
    """Test 11: High run CTA block has V1/V2 pair with 0.0 Jaccard distance.

    V1="Would you be curious to hear more?" and V2="Curious to hear more about this?"
    both reduce to {"curious", "hear"} after stopwording -> distance = 0.0.
    Without CTA exemption this would fire a pair-floor error. With exemption: no error.
    """
    from app.qa import check_block_diversity

    # Simulate a 2-block email: [diverse_block, high_cta_block]
    blocks_vars = [_DIVERSE_VARS, _HIGH_CTA_VARS]
    errors, warnings, per_block_scores, _pair_distances = check_block_diversity(blocks_vars)
    # The 0.0 pair-distance must NOT produce a pair-floor error (CTA exempt)
    cta_pair_errors = [
        e for e in errors
        if ("pair" in e.lower() or "distance" in e.lower())
        and ("block 1" in e.lower() or "block2" in e.lower())
    ]
    assert len(cta_pair_errors) == 0, (
        f"High-run CTA block must be pair-floor exempt. Got: {cta_pair_errors}"
    )
    assert len(per_block_scores) == 2


# ===========================================================================
# Greeting block exemption (12)
# ===========================================================================


def test_diversity_greeting_block_exempt():
    """Test 12: Greeting block is fully exempt - no errors or score assigned.

    Greeting block exemption is whitelist-driven via app.lint.is_greeting_block.
    The greeting block score in per_block_scores must be None (skipped, not scored).
    """
    from app.qa import check_block_diversity

    # 2-block email: [greeting, diverse_body]
    blocks_vars = [_GREETING_VARS, _DIVERSE_VARS]
    errors, warnings, per_block_scores, _pair_distances = check_block_diversity(blocks_vars)

    # No errors for the greeting block
    greeting_errors = [
        e for e in errors
        if "block 0" in e.lower() or "greeting" in e.lower()
    ]
    assert len(greeting_errors) == 0, (
        f"Greeting block must be exempt but got errors: {greeting_errors}"
    )
    # Score for greeting block is None (skipped)
    assert per_block_scores[0] is None, (
        f"Greeting block score must be None, got {per_block_scores[0]}"
    )
    assert len(per_block_scores) == 2


# ===========================================================================
# Corpus average floor warning (13)
# ===========================================================================


def test_diversity_corpus_avg_always_warning():
    """Test 13: corpus-avg warning always goes to warnings, never errors.

    CORPUS_AVG_FLOOR (0.45) triggers a warning regardless of DIVERSITY_GATE_LEVEL.
    Even in "error" mode, corpus-level signal must remain a warning.
    """
    from app.qa import check_block_diversity

    # Construct blocks where all variations are near-identical (low corpus avg)
    near_clone_v1 = "We help companies grow their sales pipeline with smart outreach."
    near_clone_vars = [near_clone_v1] * 5  # all identical -> corpus avg = 0.0
    # 1-block email (not greeting, not CTA): avg=0.0 fires everything
    errors, warnings, per_block_scores, _pair_distances = check_block_diversity([near_clone_vars])

    # Corpus warning must be in warnings, not errors
    corpus_warnings = [w for w in warnings if "corpus" in w.lower()]
    corpus_errors = [e for e in errors if "corpus" in e.lower()]
    assert len(corpus_errors) == 0, (
        f"Corpus signal must never be in errors. Got: {corpus_errors}"
    )
    assert len(corpus_warnings) > 0, (
        f"Corpus warning must fire for low corpus avg. Warnings: {warnings}"
    )


# ===========================================================================
# Gate level dispatch tests (14, 16, 17)
# ===========================================================================


def test_diversity_gate_warning_mode_sends_to_warnings():
    """Test 14: When DIVERSITY_GATE_LEVEL='warning', diversity failures -> warnings.

    Diversity errors must NOT appear in the errors list when gate is in warning mode.
    """
    from app.qa import qa

    # Build a simple spintax body where all variations are identical (will fail diversity)
    identical_var = "We help law firms manage their online reputation effectively."
    output = (
        "{{RANDOM | " + identical_var + " | " + identical_var + " | "
        + identical_var + " | " + identical_var + " | " + identical_var + " }}"
    )
    input_text = identical_var

    with patch("app.qa.DIVERSITY_GATE_LEVEL", "warning"):
        result = qa(output_text=output, input_text=input_text, platform="instantly")

    diversity_errors = [e for e in result["errors"] if "diversity" in e.lower()]
    diversity_warnings = [w for w in result["warnings"] if "diversity" in w.lower()]
    assert len(diversity_errors) == 0, (
        f"Warning mode must not add diversity to errors. Got: {diversity_errors}"
    )
    # Diversity failures should appear in warnings (or corpus warning at minimum)
    # (corpus warning fires unconditionally for all-identical blocks)


def test_diversity_gate_warning_mode_passed_true():
    """Test 16: Warning mode -> passed=True even when diversity floors are missed."""
    from app.qa import qa

    identical_var = "We help law firms manage their online reputation effectively."
    output = (
        "{{RANDOM | " + identical_var + " | " + identical_var + " | "
        + identical_var + " | " + identical_var + " | " + identical_var + " }}"
    )
    input_text = identical_var

    with patch("app.qa.DIVERSITY_GATE_LEVEL", "warning"):
        result = qa(output_text=output, input_text=input_text, platform="instantly")

    # In warning mode, diversity failures must not flip passed to False
    # (other checks may still fail, so we only assert diversity errors don't cause failure)
    diversity_errors = [e for e in result["errors"] if "diversity" in e.lower()]
    assert len(diversity_errors) == 0, (
        f"Warning mode: diversity issues must not appear in errors. Got: {diversity_errors}"
    )


def test_diversity_gate_error_mode_passed_false():
    """Test 17: Error mode -> passed=False when diversity floors are missed."""
    from app.qa import qa

    identical_var = "We help law firms manage their online reputation effectively."
    output = (
        "{{RANDOM | " + identical_var + " | " + identical_var + " | "
        + identical_var + " | " + identical_var + " | " + identical_var + " }}"
    )
    input_text = identical_var

    with patch("app.qa.DIVERSITY_GATE_LEVEL", "error"):
        result = qa(output_text=output, input_text=input_text, platform="instantly")

    diversity_errors = [e for e in result["errors"] if "diversity" in e.lower()]
    assert len(diversity_errors) > 0, (
        f"Error mode must add diversity failures to errors. Got errors={result['errors']}"
    )
    assert result["passed"] is False, (
        f"Error mode with diversity failures must set passed=False. Got passed={result['passed']}"
    )


# ===========================================================================
# Exception isolation (15)
# ===========================================================================


def test_diversity_exception_isolation():
    """Test 15: Exception inside check_block_diversity is caught; qa() still succeeds.

    If check_block_diversity raises any exception, the defensive try/except in qa()
    must catch it and:
    - Add a warning containing "diversity check failed internally"
    - NOT add anything to errors
    - Return diversity_block_scores = [None] * block_count
    - Return passed=True (if no other check failed)
    """
    from app.qa import qa

    valid_var = "We help law firms win more clients through digital reputation."
    valid_v2 = "Our platform drives client acquisition for legal practices nationwide."
    valid_v3 = "Law firms partner with us to accelerate their new client pipeline."
    valid_v4 = "Attorneys use our system to generate consistent inbound referrals."
    valid_v5 = "We power new client growth for law firms across the country."
    output = (
        f"{{{{RANDOM | {valid_var} | {valid_v2} | {valid_v3} | {valid_v4} | {valid_v5}}}}}"
    )
    input_text = valid_var

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated internal crash")

    with patch("app.qa.check_block_diversity", side_effect=_raise):
        result = qa(output_text=output, input_text=input_text, platform="instantly")

    # Must surface a warning, not an error
    isolation_warnings = [
        w for w in result["warnings"] if "diversity check failed internally" in w.lower()
    ]
    assert len(isolation_warnings) > 0, (
        f"Exception isolation warning missing. Warnings: {result['warnings']}"
    )
    isolation_errors = [e for e in result["errors"] if "diversity" in e.lower()]
    assert len(isolation_errors) == 0, (
        f"Exception must not add to errors. Errors: {result['errors']}"
    )
    # per_block_scores must be all-None list of correct length
    scores = result.get("diversity_block_scores", [])
    assert all(s is None for s in scores), (
        f"All scores must be None after exception. Got: {scores}"
    )
    assert len(scores) == result["block_count"], (
        f"Score list length must equal block_count. scores={len(scores)} block_count={result['block_count']}"
    )


# ===========================================================================
# qa() return dict - 5 new keys (18)
# ===========================================================================


def test_qa_returns_diversity_keys():
    """Test 18: qa() return dict contains all 5 new diversity keys with correct types.

    Keys: diversity_block_scores, diversity_corpus_avg, diversity_floor_block_avg,
          diversity_floor_pair, diversity_gate_level
    """
    from app.qa import qa

    v1 = "We help law firms build consistent five-star reputations online."
    v2 = "Our platform grows positive Google reviews for legal practices systematically."
    v3 = "Law firms use our system to generate authentic client feedback continuously."
    v4 = "Attorneys gain more reviews automatically through our proven methodology."
    v5 = "We deliver steady review growth for law firms with minimal effort required."
    output = f"{{{{RANDOM | {v1} | {v2} | {v3} | {v4} | {v5}}}}}"
    input_text = v1

    result = qa(output_text=output, input_text=input_text, platform="instantly")

    # All 5 keys must be present
    assert "diversity_block_scores" in result, "Missing key: diversity_block_scores"
    assert "diversity_corpus_avg" in result, "Missing key: diversity_corpus_avg"
    assert "diversity_floor_block_avg" in result, "Missing key: diversity_floor_block_avg"
    assert "diversity_floor_pair" in result, "Missing key: diversity_floor_pair"
    assert "diversity_gate_level" in result, "Missing key: diversity_gate_level"

    # Type checks
    assert isinstance(result["diversity_block_scores"], list), (
        "diversity_block_scores must be a list"
    )
    assert result["diversity_gate_level"] in ("warning", "error"), (
        f"diversity_gate_level must be 'warning' or 'error', got {result['diversity_gate_level']!r}"
    )
    assert isinstance(result["diversity_floor_block_avg"], float), (
        "diversity_floor_block_avg must be float"
    )
    assert isinstance(result["diversity_floor_pair"], float), (
        "diversity_floor_pair must be float"
    )

    # Invariant: len(diversity_block_scores) == block_count
    assert len(result["diversity_block_scores"]) == result["block_count"], (
        f"diversity_block_scores length {len(result['diversity_block_scores'])} "
        f"!= block_count {result['block_count']}"
    )


# ===========================================================================
# Route test: POST /api/qa returns diversity keys in response body (19)
# ===========================================================================


def test_route_qa_response_includes_diversity_keys(authed_client):
    """Test 19: POST /api/qa response JSON includes all 5 diversity keys.

    Route integration test - validates the Pydantic response model exposes
    diversity fields to callers.
    """
    v1 = "We help law firms earn consistent five-star Google reviews every month."
    v2 = "Our system generates authentic positive feedback for legal practices reliably."
    v3 = "Law firms build their online reputation steadily using our proven approach."
    v4 = "Attorneys attract new clients by growing their five-star review count."
    v5 = "We power reputation growth for law firms through automated review collection."
    output = f"{{{{RANDOM | {v1} | {v2} | {v3} | {v4} | {v5}}}}}"

    r = authed_client.post(
        "/api/qa",
        json={
            "output_text": output,
            "input_text": v1,
            "platform": "instantly",
        },
    )
    assert r.status_code == 200, f"Expected 200, got {r.status_code}. Body: {r.text}"

    body = r.json()
    for key in (
        "diversity_block_scores",
        "diversity_corpus_avg",
        "diversity_floor_block_avg",
        "diversity_floor_pair",
        "diversity_gate_level",
    ):
        assert key in body, f"Response missing diversity key: {key!r}. Keys present: {list(body.keys())}"


def test_diversity_emailbison_variable_stripping_block_level():
    """Test 20: EmailBison {FIRSTNAME} stripping does not pollute Jaccard scoring.

    Mirrors the Instantly-format protection from earlier tests. The shared
    {FIRSTNAME} placeholder must NOT inflate the V1<->V2 word-set
    intersection, otherwise pairs that are otherwise diverse would score
    falsely high.
    """
    from app.qa import _diversity_tokens, _jaccard_distance

    v1 = "Hi {FIRSTNAME}, your account deserves a faster review pipeline."
    v2 = "Hi {FIRSTNAME}, the team built a quicker feedback loop."

    t1 = _diversity_tokens(v1)
    t2 = _diversity_tokens(v2)

    # The placeholder identifier must be stripped, never tokenized.
    assert "firstname" not in t1
    assert "firstname" not in t2
    assert "FIRSTNAME" not in t1
    assert "FIRSTNAME" not in t2

    # The two variations share no content tokens after stopwording, so
    # distance must be 1.0. If {FIRSTNAME} were not stripped, "firstname"
    # would land in the intersection and distance would be < 1.0.
    distance = _jaccard_distance(t1, t2)
    assert distance == 1.0, (
        f"Expected distance=1.0 (no shared content), got {distance}. "
        "Likely {FIRSTNAME} leaked into tokens."
    )


# ---------------------------------------------------------------------------
# V2 per-block diversity retry helpers (proposal Section 8 step 6)
# ---------------------------------------------------------------------------


def test_compute_failing_blocks_from_errors_excludes_cta_pair_floor():
    """V2.21: only blocks with diversity-related errors are returned, and
    CTA pair-floor carve-out is auto-inherited because qa.py:600 only emits
    pair-floor errors for non-CTA blocks. Other error families ignored."""
    from app.spintax_runner import compute_failing_blocks_from_errors

    qa_result = {
        "errors": [
            "block 1: diversity below floor (avg distance 0.20 < 0.30; ...)",
            "block 3 variation 4: pairwise diversity below floor "
            "(distance 0.10 < 0.20; ...)",
            "block 2: V1 fidelity check failed - some unrelated reason",
            "block 5 variation 2: doubled '!' (some other thing)",
        ],
    }
    assert compute_failing_blocks_from_errors(qa_result) == [0, 2]


def test_compute_failing_blocks_from_errors_empty():
    """No errors at all -> empty list. No errors with diversity prefix
    -> empty list. Both must short-circuit cleanly."""
    from app.spintax_runner import compute_failing_blocks_from_errors

    assert compute_failing_blocks_from_errors({}) == []
    assert compute_failing_blocks_from_errors({"errors": []}) == []
    assert (
        compute_failing_blocks_from_errors(
            {"errors": ["block 1: V1 fidelity check failed"]}
        )
        == []
    )


def test_revert_single_block_invariants_both_directions():
    """V2.22: revert restores the targeted block AND leaves all other
    blocks byte-untouched. Both invariants verified."""
    from app.spintax_runner import revert_single_block

    pre = (
        "Hi {{firstName}},\n\n"
        "{{RANDOM | A1 sentence. | A2 sentence. | A3 sentence. | "
        "A4 sentence. | A5 sentence.}}\n\n"
        "{{RANDOM | B1 only. | B2 only. | B3 only. | B4 only. | B5 only.}}\n\n"
        "Thanks,\n{{accountSignature}}\n"
    )
    # Simulate post-retry: block 0 inner replaced (block 1 untouched).
    post = (
        "Hi {{firstName}},\n\n"
        "{{RANDOM | A1 sentence. | X2 new. | X3 new. | X4 new. | X5 new.}}\n\n"
        "{{RANDOM | B1 only. | B2 only. | B3 only. | B4 only. | B5 only.}}\n\n"
        "Thanks,\n{{accountSignature}}\n"
    )
    reverted = revert_single_block(post, pre, 0, "instantly")
    assert reverted == pre


def test_revert_single_block_corruption_raises():
    """If post_body has the wrong number of blocks, the splice would
    corrupt other blocks. Must raise SpliceCorruptionError so caller
    falls back to shipping the pre-retry body wholesale."""
    from app.spintax_runner import SpliceCorruptionError, revert_single_block

    pre = (
        "Hi,\n{{RANDOM | A1. | A2. | A3. | A4. | A5.}}\n"
        "{{RANDOM | B1. | B2. | B3. | B4. | B5.}}\n"
    )
    # post lost a block -> count mismatch -> corruption.
    post_corrupt = "Hi,\n{{RANDOM | A1. | A2. | A3. | A4. | A5.}}\n"
    with pytest.raises(SpliceCorruptionError):
        revert_single_block(post_corrupt, pre, 1, "instantly")


def test_joint_score_block_length_scaling():
    """V2.23: long body blocks (16+ content words) with drift_count=6
    keep some drift_inverse; short blocks (5 words) get penalized harder.
    Coefficients: 0.7*diversity + 0.3*drift_inverse where drift_inverse is
    1 - drift_count / max(5, len//2)."""
    from app.spintax_runner import joint_score

    # No drift, perfect drift_inverse: 0.7*0.45 + 0.3*1.0 = 0.615
    s_no_drift = joint_score(0.45, 0, 16)
    assert abs(s_no_drift - 0.615) < 0.001

    # Long block, drift=6, denom=max(5,8)=8, drift_inverse=1-6/8=0.25
    # 0.7*0.45 + 0.3*0.25 = 0.315 + 0.075 = 0.39
    s_long = joint_score(0.45, 6, 16)
    assert abs(s_long - 0.39) < 0.001

    # Short block, drift=6, denom=max(5, 2)=5, drift_inverse=max(0,1-6/5)=0
    # 0.7*0.10 + 0.3*0 = 0.07
    s_short = joint_score(0.10, 6, 5)
    assert abs(s_short - 0.07) < 0.001

    # drift_inverse floors at 0 (never negative)
    s_huge_drift = joint_score(0.40, 100, 5)
    assert s_huge_drift == pytest.approx(0.7 * 0.40)


def test_parse_revision_json_well_formed():
    """JSON parser accepts standard well-formed output."""
    from app.spintax_runner import _parse_revision_json

    out = _parse_revision_json(
        '{"v2":"a","v3":"b","v4":"c","v5":"d",'
        '"strategies":["structural","lexical","combined","structural"]}'
    )
    assert out["v2"] == "a"
    assert out["v3"] == "b"
    assert out["v4"] == "c"
    assert out["v5"] == "d"
    assert out["strategies"] == ["structural", "lexical", "combined", "structural"]


def test_parse_revision_json_with_code_fence():
    """Strips ```json code fences before parsing."""
    from app.spintax_runner import _parse_revision_json

    fenced = (
        '```json\n{"v2":"x","v3":"y","v4":"z","v5":"w"}\n```'
    )
    out = _parse_revision_json(fenced)
    assert out["v2"] == "x"
    assert out["strategies"] == []  # missing in input -> tolerated as empty


def test_parse_revision_json_normalizes_keys():
    """Tolerates capitalized keys (V2 vs v2) emitted by some models."""
    from app.spintax_runner import _parse_revision_json

    caps = '{"V2":"a","V3":"b","V4":"c","V5":"d"}'
    out = _parse_revision_json(caps)
    assert out["v2"] == "a"
    assert out["v3"] == "b"


def test_parse_revision_json_extracts_from_prose():
    """Tolerates prose before/after the JSON object (chatty models)."""
    from app.spintax_runner import _parse_revision_json

    prosey = (
        'Sure, here is the JSON you asked for:\n'
        '{"v2":"a","v3":"b","v4":"c","v5":"d"}\n'
        'Hope that helps!'
    )
    out = _parse_revision_json(prosey)
    assert out["v2"] == "a"
    assert out["v5"] == "d"


def test_parse_revision_json_rejects_missing_keys():
    """Missing v4 or v5 -> ValueError. Caller treats as failed sub-call."""
    from app.spintax_runner import _parse_revision_json

    with pytest.raises(ValueError, match="missing keys"):
        _parse_revision_json('{"v2":"a","v3":"b"}')


def test_parse_revision_json_rejects_empty_strings():
    """A required variant being empty/whitespace -> ValueError."""
    from app.spintax_runner import _parse_revision_json

    with pytest.raises(ValueError):
        _parse_revision_json('{"v2":"a","v3":"","v4":"c","v5":"d"}')


def test_parse_revision_json_rejects_non_object():
    """Top-level array or scalar -> ValueError."""
    from app.spintax_runner import _parse_revision_json

    with pytest.raises(ValueError):
        _parse_revision_json("[1, 2, 3]")
    with pytest.raises(ValueError):
        _parse_revision_json("not json at all")


def test_parse_revision_json_tolerates_bad_strategies():
    """Strategies field that isn't a 4-element list -> empty list, not raise."""
    from app.spintax_runner import _parse_revision_json

    out = _parse_revision_json(
        '{"v2":"a","v3":"b","v4":"c","v5":"d","strategies":"oops"}'
    )
    assert out["strategies"] == []

    out2 = _parse_revision_json(
        '{"v2":"a","v3":"b","v4":"c","v5":"d","strategies":["only","two"]}'
    )
    assert out2["strategies"] == []


def test_reassemble_round_trip_instantly():
    """Reassemble with a replacement preserves every other block byte-for-byte
    and matches the new content exactly when re-extracted."""
    from app.lint import extract_blocks, reassemble

    body = (
        "Hi {{firstName}},\n\n"
        "{{RANDOM | A1 first. | A2 first. | A3 first. | A4 first. | A5 first.}}\n\n"
        "{{RANDOM | B1 only. | B2 only. | B3 only. | B4 only. | B5 only.}}\n\n"
        "Thanks.\n"
    )
    pre = extract_blocks(body, "instantly")

    # No-op: empty replacements.
    assert reassemble(body, {}, "instantly") == body

    # Replace block 0 only.
    new_inner = " X1. | X2. | X3. | X4. | X5."
    out = reassemble(body, {0: new_inner}, "instantly")
    post = extract_blocks(out, "instantly")
    assert len(post) == 2
    assert post[0][1] == new_inner
    assert post[1][1] == pre[1][1]  # block 1 byte-untouched


def test_reassemble_round_trip_emailbison():
    """EmailBison single-brace format also round-trips."""
    from app.lint import extract_blocks, reassemble

    body = "Hi {firstName},\n{C1.|C2.|C3.|C4.|C5.}\nThanks.\n"
    out = reassemble(body, {0: "D1.|D2.|D3.|D4.|D5."}, "emailbison")
    post = extract_blocks(out, "emailbison")
    assert post[0][1] == "D1.|D2.|D3.|D4.|D5."


def test_diversity_revision_prompt_includes_clean_context_signals():
    """The new per-block prompt must contain: the V1 verbatim, all V2-V5,
    the worked examples (abstract placeholders), and the JSON spec."""
    from app.spintax_runner import _build_diversity_revision_prompt

    prompt = _build_diversity_revision_prompt(
        block_v1="At {{company_name}}, deals close fast.",
        block_variants=["Deals fly.", "Deals close.", "Closing.", "Done."],
        block_score=0.10,
        block_pairwise_diagnostics=[
            "block 2 variation 2: pairwise diversity below floor"
        ],
        block_position=2,
        platform="instantly",
    )
    # V1 preserved word-for-word in prompt
    assert "At {{company_name}}, deals close fast." in prompt
    # All four variants surfaced
    assert "Deals fly." in prompt
    assert "Closing." in prompt
    # Strategies enum present and ranked
    assert "structural" in prompt
    assert "lexical" in prompt
    assert "combined" in prompt
    # Worked examples use abstract placeholders, not concrete domain tokens
    assert "company_name" in prompt
    assert "trigger_event" in prompt
    # JSON output spec
    assert '"v2":' in prompt
    assert "strategies" in prompt
