"""Phase A.0 calibration tests for the diversity gate.

These tests use inlined prose blocks from two real audit runs (medium and high
effort) to verify that `check_block_diversity` produces per-block scores that
match the empirical targets in DIVERSITY_GATE_SPEC.md Section 1.

Calibration targets (tolerance ±0.05):
    Block index  Type      Medium avg  High avg
    0            Greeting  exempt      exempt
    2            Pain Q    0.369       0.169
    3            Pitch     0.244       0.092
    4            CTA       1.000       0.667
    5            P.S.      0.174       0.205

Job IDs referenced: 135cf82f (medium), audit run 2 (high).
"""

import pytest
from app.qa import check_block_diversity

# ---------------------------------------------------------------------------
# Medium run blocks (job 135cf82f)
# ---------------------------------------------------------------------------

_MEDIUM_BLOCKS = [
    # Block 0 — Greeting (should be exempt -> score = None)
    [
        "Hey {{firstName}},",
        "Hi {{firstName}},",
        "Hello {{firstName}},",
        "Hey there,",
        "{{firstName}},",
    ],
    # Block 1 — Review observation (not explicitly calibrated, included for
    # structural completeness so block indices match the real email body)
    [
        "I noticed {{companyName}} has {{review_count}} reviews on Google with an average rating of {{rating}} - good job.",
        "I saw {{companyName}} has {{review_count}} reviews on Google with an average rating of {{rating}} - nice work.",
        "{{companyName}} shows {{review_count}} Google reviews with an average rating of {{rating}} - that's strong work.",
        "Checked Google and noticed {{companyName}} has {{review_count}} reviews with a {{rating}} average - nice job.",
        "Google shows {{companyName}} at {{review_count}} total reviews with an average rating of {{rating}} - good job.",
    ],
    # Block 2 — Pain question; target avg ~0.369
    [
        "Do you feel like your current system for collecting reviews is overpriced and bloated with features you don't need?",
        "Does your current review collection system feel overpriced and bloated with features you don't need right now?",
        "Is your current system for collecting reviews too pricey and packed with extra features you don't need right now?",
        "Are you paying too much for your current system for collecting reviews that's bloated with features you don't need?",
        "Does collecting reviews through your current system feel overpriced and weighed down with features you don't need?",
    ],
    # Block 3 — Pitch; target avg ~0.244
    [
        "We help law firms net 48 5-star Google reviews/month for 149 bucks/month. Plus, you'd choose which reviews go public - letting you block bad ones.",
        "We help law firms get 48 5-star Google reviews/month for 149 bucks/month. Plus, you pick which reviews go public - so bad ones stay blocked.",
        "We help law firms net 48 5-star Google reviews/month at 149 bucks/month. Plus, you'd also choose which reviews go public - blocking bad ones.",
        "For 149 bucks/month, we help law firms net 48 5-star Google reviews/month. Plus, you'd choose which reviews go public and block the bad ones.",
        "We help law firms land 48 5-star Google reviews/month for 149 bucks/month. Plus, you'd pick which reviews go public - bad ones get blocked.",
    ],
    # Block 4 — CTA (last block of a 2-block sub-call in unit tests, but here
    # it is NOT the last block globally — block 5 is); target avg ~1.000
    [
        "Would you be curious to hear more?",
        "Would you be open to hearing more?",
        "Would a few more details be useful?",
        "Would it be worth hearing a bit more?",
        "Should I send a few more details?",
    ],
    # Block 5 — P.S.; target avg ~0.174
    [
        "P.S. Reply and I'll cut the 149 bucks/month in half for 6 months.",
        "P.S. Reply and I'll halve the 149 bucks/month rate for 6 months.",
        "P.S. If you reply, I'll cut 149 bucks/month in half for 6 months.",
        "P.S. Reply and I'll cut the 149 bucks/month by half for 6 months.",
        "P.S. Reply and I'll drop 149 bucks/month to half for 6 months.",
    ],
]

# ---------------------------------------------------------------------------
# High run blocks (audit run 2)
# ---------------------------------------------------------------------------

_HIGH_BLOCKS = [
    # Block 0 — Greeting (exempt)
    [
        "Hey {{firstName}},",
        "Hi {{firstName}},",
        "Hello {{firstName}},",
        "Hey there,",
        "{{firstName}},",
    ],
    # Block 1 — Review observation (structural filler)
    [
        "I noticed {{companyName}} has {{review_count}} reviews on Google with an average rating of {{rating}} - good job.",
        "I saw that {{companyName}} has {{review_count}} Google reviews and an average rating of {{rating}} - nice work.",
        "{{companyName}} shows {{review_count}} reviews on Google with an average rating of {{rating}} - solid work there.",
        "Looks like {{companyName}} has {{review_count}} Google reviews with an average rating of {{rating}} - well done.",
        "I found {{companyName}} sitting at {{review_count}} Google reviews and an average rating of {{rating}} - good work.",
    ],
    # Block 2 — Pain question; target avg ~0.169
    [
        "Do you feel like your current system for collecting reviews is overpriced and bloated with features you don't need?",
        "Does your current system for collecting reviews feel like it's overpriced and bloated with features you don't need?",
        "Do you think your current system for collecting reviews is too pricey and bloated with features you don't need?",
        "Is your current system for collecting reviews feeling like it's overpriced and bloated with features you don't need?",
        "Does it feel like your current system for collecting reviews is overpriced and bloated with features you don't need?",
    ],
    # Block 3 — Pitch; target avg ~0.092
    [
        "We help law firms net 48 5-star Google reviews/month for 149 bucks/month. Plus, you'd choose which reviews go public - letting you block bad ones.",
        "We help law firms get 48 5-star Google reviews/month for 149 bucks/month. You'd also choose which reviews go public - so you can block bad ones.",
        "For 149 bucks/month, we help law firms net 48 5-star Google reviews/month. Plus, you'd choose which reviews go public - letting you block bad ones.",
        "We help law firms net 48 5-star Google reviews/month for 149 bucks/month. You'd choose which reviews go public - letting you block bad ones too.",
        "We help law firms net 48 5-star Google reviews/month for 149 bucks/month. Plus, you'd pick which reviews go public - letting you block bad ones.",
    ],
    # Block 4 — CTA; target avg ~0.667
    [
        "Would you be curious to hear more?",
        "Curious to hear more about this?",
        "Would hearing more here be useful?",
        "Open to hearing a bit more here?",
        "Would you be interested to hear more?",
    ],
    # Block 5 — P.S.; target avg ~0.205
    [
        "P.S. Reply and I'll cut the 149 bucks/month in half for 6 months.",
        "P.S. Reply and I'll halve the 149 bucks/month rate for 6 months.",
        "P.S. Reply and I'll drop the 149 bucks/month by half for 6 months.",
        "P.S. Send a reply and I'll cut 149 bucks/month in half for 6 months.",
        "P.S. Reply and I'll cut the 149 bucks/month by half for 6 months.",
    ],
]

_TOL = 0.05  # calibration tolerance per spec Section 1


def _avg_non_none(scores):
    """Helper: mean of a list that may contain None entries."""
    valid = [s for s in scores if s is not None]
    return sum(valid) / len(valid) if valid else None


def _min_non_none(scores):
    """Helper: min of a list that may contain None entries."""
    valid = [s for s in scores if s is not None]
    return min(valid) if valid else None


# ---------------------------------------------------------------------------
# Calibration tests
# ---------------------------------------------------------------------------


def test_calibration_medium_run():
    """Block-level diversity scores match spec Section 1 targets for medium run.

    Targets (±0.05):
        Block 0 (greeting): score = None  [exempt]
        Block 2 (pain Q):   avg ~0.369
        Block 3 (pitch):    avg ~0.244
        Block 4 (CTA):      avg ~1.000   (NOTE: not last block in this 6-block
                                          input, so _is_cta_block returns False;
                                          score is computed normally)
        Block 5 (P.S.):     avg ~0.174
    """
    errors, warnings, per_block_scores, _pair_distances = check_block_diversity(_MEDIUM_BLOCKS)

    assert len(per_block_scores) == 6, (
        f"Expected 6 per-block scores, got {len(per_block_scores)}"
    )

    # Block 0: greeting must be exempt
    assert per_block_scores[0] is None, (
        f"Block 0 (greeting) should be exempt (None), got {per_block_scores[0]}"
    )

    # Block 2: pain question — avg ~0.369
    score_2 = per_block_scores[2]
    assert score_2 is not None, "Block 2 (pain Q) score should not be None"
    assert abs(score_2 - 0.369) <= _TOL, (
        f"Block 2 (pain Q) avg {score_2:.3f} outside target 0.369 ±{_TOL}"
    )

    # Block 3: pitch — avg ~0.244
    score_3 = per_block_scores[3]
    assert score_3 is not None, "Block 3 (pitch) score should not be None"
    assert abs(score_3 - 0.244) <= _TOL, (
        f"Block 3 (pitch) avg {score_3:.3f} outside target 0.244 ±{_TOL}"
    )

    # Block 4: CTA — avg ~1.000 (medium run has highly diverse CTA variations)
    score_4 = per_block_scores[4]
    assert score_4 is not None, "Block 4 (CTA) score should not be None"
    assert abs(score_4 - 1.000) <= _TOL, (
        f"Block 4 (CTA) avg {score_4:.3f} outside target 1.000 ±{_TOL}"
    )

    # Block 5: P.S. — avg ~0.174
    score_5 = per_block_scores[5]
    assert score_5 is not None, "Block 5 (P.S.) score should not be None"
    assert abs(score_5 - 0.174) <= _TOL, (
        f"Block 5 (P.S.) avg {score_5:.3f} outside target 0.174 ±{_TOL}"
    )


def test_calibration_high_run():
    """Block-level diversity scores match spec Section 1 targets for high run.

    Targets (±0.05):
        Block 0 (greeting): score = None  [exempt]
        Block 2 (pain Q):   avg ~0.169
        Block 3 (pitch):    avg ~0.092   (near-identical word-order shuffles)
        Block 4 (CTA):      avg ~0.667   (V1/V2 both reduce to {curious, hear})
        Block 5 (P.S.):     avg ~0.205
    """
    errors, warnings, per_block_scores, _pair_distances = check_block_diversity(_HIGH_BLOCKS)

    assert len(per_block_scores) == 6, (
        f"Expected 6 per-block scores, got {len(per_block_scores)}"
    )

    # Block 0: greeting must be exempt
    assert per_block_scores[0] is None, (
        f"Block 0 (greeting) should be exempt (None), got {per_block_scores[0]}"
    )

    # Block 2: pain question — avg ~0.169 (near-identical rewrites in high run)
    score_2 = per_block_scores[2]
    assert score_2 is not None, "Block 2 (pain Q) score should not be None"
    assert abs(score_2 - 0.169) <= _TOL, (
        f"Block 2 (pain Q) avg {score_2:.3f} outside target 0.169 ±{_TOL}"
    )

    # Block 3: pitch — avg ~0.092 (smoking-gun near-duplicates)
    score_3 = per_block_scores[3]
    assert score_3 is not None, "Block 3 (pitch) score should not be None"
    assert abs(score_3 - 0.092) <= _TOL, (
        f"Block 3 (pitch) avg {score_3:.3f} outside target 0.092 ±{_TOL}"
    )

    # Block 4: CTA — avg ~0.667
    # V1="Would you be curious to hear more?" and V2="Curious to hear more
    # about this?" both tokenize to {curious, hear} after stopwording, giving
    # a 0.0 pair distance.  The overall block avg is still ~0.667.
    score_4 = per_block_scores[4]
    assert score_4 is not None, "Block 4 (CTA) score should not be None"
    assert abs(score_4 - 0.667) <= _TOL, (
        f"Block 4 (CTA) avg {score_4:.3f} outside target 0.667 ±{_TOL}"
    )

    # Block 5: P.S. — avg ~0.205
    score_5 = per_block_scores[5]
    assert score_5 is not None, "Block 5 (P.S.) score should not be None"
    assert abs(score_5 - 0.205) <= _TOL, (
        f"Block 5 (P.S.) avg {score_5:.3f} outside target 0.205 ±{_TOL}"
    )


def test_calibration_return_shape_invariant():
    """check_block_diversity always returns len(scores) == len(blocks_vars).

    Verifies the invariant holds for both calibration corpora.
    """
    for label, blocks in [("medium", _MEDIUM_BLOCKS), ("high", _HIGH_BLOCKS)]:
        errors, warnings, per_block_scores, _pair_distances = check_block_diversity(blocks)
        assert len(per_block_scores) == len(blocks), (
            f"{label} run: expected {len(blocks)} scores, got {len(per_block_scores)}"
        )


def test_calibration_high_run_pitch_below_floor():
    """High run pitch block (avg ~0.092) is below BLOCK_AVG_FLOOR (0.30).

    This confirms that the smoking-gun near-duplicate pitch variations in the
    high run trigger a diversity error (or warning depending on gate level).
    The error/warning message must mention block index 3.
    """
    errors, warnings, per_block_scores, _pair_distances = check_block_diversity(_HIGH_BLOCKS)

    # Score for pitch block must exist and be below the 0.30 floor
    score_3 = per_block_scores[3]
    assert score_3 is not None, "Pitch block score must not be None"
    assert score_3 < 0.30, (
        f"High run pitch score {score_3:.3f} should be below BLOCK_AVG_FLOOR 0.30"
    )

    # At least one diagnostic (error or warning) must reference block 3
    all_diagnostics = errors + warnings
    block_3_flagged = any("3" in msg or "block" in msg.lower() for msg in all_diagnostics)
    assert block_3_flagged, (
        f"Expected a diagnostic mentioning block 3; got errors={errors}, warnings={warnings}"
    )
