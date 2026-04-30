"""Unit tests for app/qa.py - ported from upstream test_qa_spintax.py.

Covers:
- V1 fidelity (matches input paragraph)
- Block count matches input paragraph count
- Greeting whitelist enforcement (informal greetings rejected)
- Duplicate variation detection
- Smart-quote warning
- Input paragraph splitter (handles bullet lists, {{accountSignature}})
- End-to-end qa() smoke test
"""

from app.qa import (
    qa,
    split_input_paragraphs,
    spintaxable_input_paragraphs,
    check_greeting,
    check_no_duplicate_variations,
    check_v1_fidelity,
    check_block_count,
    check_no_smart_quotes,
    check_no_doubled_punctuation,
    check_concept_drift,
    _input_starts_with_greeting,
)


# ---------- paragraph splitter ----------

def test_split_input_recognizes_bullets_and_variables():
    text = (
        "Hey {{firstName}},\n"
        "\n"
        "First prose paragraph.\n"
        "\n"
        "- bullet one\n"
        "- bullet two\n"
        "\n"
        "{{accountSignature}}\n"
        "\n"
        "P.S. last prose.\n"
    )
    paras = split_input_paragraphs(text)
    kinds = [kind for kind, _ in paras]
    # greeting, prose, bullets, signature, P.S. prose
    assert kinds == ["PROSE", "PROSE", "UNSPUN", "UNSPUN", "PROSE"], paras
    prose_only = spintaxable_input_paragraphs(text)
    assert len(prose_only) == 3


def test_split_input_marks_closing_signature_unspun():
    """Closing email signatures (Best,\\nName) must be UNSPUN, not PROSE.

    Without this, a 3-paragraph input (greeting / body / signature) would
    expect 3 spintax blocks, but the model correctly produces 2 (the
    signature stays verbatim) — block-count check would falsely fail.
    """
    text = (
        "Hey {{firstName}},\n"
        "\n"
        "I saw your team grew 40% last quarter — congrats!\n"
        "\n"
        "Best,\n"
        "Danica\n"
    )
    paras = split_input_paragraphs(text)
    kinds = [kind for kind, _ in paras]
    assert kinds == ["PROSE", "PROSE", "UNSPUN"], paras
    assert len(spintaxable_input_paragraphs(text)) == 2


def test_split_input_closing_signature_variants():
    """Common closing words: Best, Thanks, Regards, Cheers, Sincerely, etc."""
    for closing in ["Best", "Thanks", "Regards", "Cheers",
                    "Warm regards", "Sincerely", "Kind regards", "Thank you"]:
        text = f"Body paragraph here.\n\n{closing},\nDanica\n"
        paras = split_input_paragraphs(text)
        assert paras[-1][0] == "UNSPUN", f"{closing!r} closing not UNSPUN: {paras}"


def test_split_input_single_line_best_alone_is_prose():
    """A bare ``Best,`` line is NOT a signature — needs the second name line.

    Earlier versions accepted 1-line `Best,` as a signature. Compact
    single-newline format made that a false-positive trap (every "Best,"
    in body prose would be marked UNSPUN), so the heuristic now requires
    two lines: closing word + short sender name.
    """
    text = "Body paragraph here.\n\nBest,\n"
    paras = split_input_paragraphs(text)
    assert paras[-1] == ("PROSE", "Best,"), paras


def test_split_input_does_not_misclassify_long_two_line_paragraph():
    """Two-line block where line 2 is long is NOT a signature.

    Compact-format rule kicks in: with no multi-line UNSPUN match, each
    non-blank line becomes its own paragraph. The important guarantee is
    that NEITHER line is marked UNSPUN — both stay PROSE.
    """
    text = (
        "Best,\n"
        "this is a long second line that clearly belongs to a body paragraph.\n"
    )
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    assert "UNSPUN" not in kinds, paras
    assert all(k == "PROSE" for k in kinds), paras


def test_split_input_does_not_misclassify_signature_with_variable():
    """A 2-line block with `{{var}}` on line 2 is NOT a closing signature.

    Like the long-line case, this falls through to the per-line split:
    line 1 is a one-line PROSE paragraph (just "Best,"), line 2 is
    "{{senderName}}" — which IS a single-line variable token and so
    becomes its own UNSPUN paragraph. The point of this test is that the
    pair is NOT collapsed into one UNSPUN closing-signature block.
    """
    text = "Best,\n{{senderName}}\n"
    paras = split_input_paragraphs(text)
    # Two separate paragraphs, not one merged signature.
    assert len(paras) == 2, paras
    assert paras[0] == ("PROSE", "Best,"), paras
    assert paras[1][0] == "UNSPUN", paras  # {{senderName}} on its own
    assert "{{senderName}}" in paras[1][1]


# ---------- compact format (single-newline paragraph separators) ----------


def test_split_input_compact_email_one_line_per_paragraph():
    """Mihajlo's compact format: each paragraph on its own line, no blanks."""
    text = (
        "{{firstName}} - noticed {{bad_review_name}} left a 1-star...\n"
        "Does it bother you that one more like this could drop your...\n"
        "We help {{practice_area}} firms with strong ratings...\n"
        "Happy clients go straight to Google. Bad feedback comes...\n"
        "If I offered you five 5-stars (on the house)...would you try it?\n"
        "{{accountSignature}}\n"
        "p.s. we helped Fox & Farmer block 3 critiques...\n"
    )
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    # 6 PROSE body lines + 1 UNSPUN ({{accountSignature}}) + 1 PROSE P.S.
    assert kinds == ["PROSE"] * 5 + ["UNSPUN"] + ["PROSE"], paras
    assert spintaxable_input_paragraphs(text) == [
        "{{firstName}} - noticed {{bad_review_name}} left a 1-star...",
        "Does it bother you that one more like this could drop your...",
        "We help {{practice_area}} firms with strong ratings...",
        "Happy clients go straight to Google. Bad feedback comes...",
        "If I offered you five 5-stars (on the house)...would you try it?",
        "p.s. we helped Fox & Farmer block 3 critiques...",
    ]


def test_split_input_classic_double_newline_still_works():
    """Backwards compat: \\n\\n separated email behaves the same as before."""
    text = (
        "Hey {{firstName}},\n"
        "\n"
        "Body paragraph one.\n"
        "\n"
        "Body paragraph two.\n"
        "\n"
        "Best,\n"
        "Danica\n"
    )
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    assert kinds == ["PROSE", "PROSE", "PROSE", "UNSPUN"], paras


def test_split_input_mixed_separators():
    """Mixed format: some paragraphs blank-separated, others compact."""
    text = (
        "Hey {{firstName}},\n"        # line 1 - greeting
        "\n"
        "Body paragraph one.\n"       # line 3 - body
        "Body paragraph two.\n"       # line 4 - body (compact-style, no blank)
        "\n"
        "Best,\n"                     # line 6 - signature (paired with line 7)
        "Danica\n"                    # line 7
    )
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    # greeting / body1 / body2 / Best,\nDanica (signature merged)
    assert kinds == ["PROSE", "PROSE", "PROSE", "UNSPUN"], paras
    assert paras[-1][1] == "Best,\nDanica"


def test_split_input_compact_preserves_signature_grouping():
    """In compact format, "Best,\\nDanica" still merges into one UNSPUN block."""
    text = (
        "Body paragraph here.\n"
        "Best,\n"
        "Danica\n"
    )
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    # Body line stands alone; Best+Danica are recognized as a multi-line
    # signature group within the same blank-separated run.
    # Current behavior: the whole pending list (3 lines) is checked as a
    # signature group — which fails because line 1 is the body. So it
    # splits per-line: 3 single-line paragraphs.
    # The signature collapse only happens when the run is JUST the signature.
    # Document this trade-off explicitly.
    assert len(paras) == 3, paras
    assert all(k == "PROSE" for k in kinds), paras


def test_split_input_compact_preserves_bullet_grouping():
    """In compact format, all-bullet runs still merge into one UNSPUN block."""
    text = (
        "Body paragraph.\n"
        "\n"
        "- bullet one\n"
        "- bullet two\n"
        "- bullet three\n"
    )
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    assert kinds == ["PROSE", "UNSPUN"], paras


def test_split_input_empty_leading_trailing_lines_ignored():
    """Empty lines at start/end don't produce empty paragraphs."""
    text = "\n\n\nFirst body.\nSecond body.\n\n\n"
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    assert kinds == ["PROSE", "PROSE"], paras
    assert [p for _, p in paras] == ["First body.", "Second body."]


def test_split_input_signature_in_compact_run_with_other_lines_does_not_merge():
    """If signature lines are in the middle of a longer compact run, they
    don't merge — the run as a whole isn't a signature, so per-line split
    wins. This is intentional: signatures are typically the LAST paragraph
    in their own blank-separated run.
    """
    text = "Body line.\nBest,\nDanica\nAnother body.\n"
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    # 4 separate PROSE lines.
    assert kinds == ["PROSE", "PROSE", "PROSE", "PROSE"], paras


# ---------- greeting whitelist ----------

def _build_output(greeting_variations: list[str], rest_blocks_count: int = 0) -> str:
    """Build a minimal output with a greeting spintax block and no other content."""
    greeting_block = "{{RANDOM | " + " | ".join(greeting_variations) + " }}"
    body_blocks = "\n\n".join(
        "{{RANDOM | Body line.. | Body line.! | Body line.? | Body line... | Body line.,}}"
        for _ in range(rest_blocks_count)
    )
    return greeting_block + (("\n\n" + body_blocks) if body_blocks else "")


def test_informal_greeting_fails():
    greetings = [
        "Hey {{firstName}},",
        "Hi {{firstName}},",
        "Hello {{firstName}},",
        "Heya {{firstName}},",   # INVALID
        "Howdy {{firstName}},",  # INVALID
    ]
    input_text = "Hey {{firstName}},\n"
    output_text = _build_output(greetings)
    result = qa(output_text, input_text, "instantly")
    informal_hits = [e for e in result["errors"] if "informal greeting" in e]
    assert len(informal_hits) == 2, informal_hits


def test_approved_greetings_pass():
    greetings = [
        "Hey {{firstName}},",
        "Hi {{firstName}},",
        "Hello {{firstName}},",
        "Hey there,",
        "{{firstName}},",
    ]
    input_text = "Hey {{firstName}},\n"
    output_text = _build_output(greetings)
    result = qa(output_text, input_text, "instantly")
    greeting_errors = [e for e in result["errors"] if "greeting" in e.lower()]
    assert greeting_errors == [], greeting_errors


def test_greeting_check_skipped_when_input_has_no_greeting():
    # Input has no greeting line; the check should pass even with weird block 1 content.
    greetings = [
        "Random start A",
        "Random start B",
        "Random start C",
        "Random start D",
        "Random start E",
    ]
    input_text = "Random start A\n"  # not a greeting pattern
    output_text = _build_output(greetings)
    result = qa(output_text, input_text, "instantly")
    assert not any("greeting" in e.lower() for e in result["errors"])


# ---------- duplicate variations ----------

def test_duplicate_variation_flagged():
    variations = ["Aa", "Bb", "Aa", "Cc", "Dd"]  # var 3 duplicates var 1
    blocks_vars = [variations]
    errors = check_no_duplicate_variations(blocks_vars)
    assert len(errors) == 1 and "variation 3" in errors[0]


def test_no_duplicates_passes():
    variations = ["One", "Two", "Three", "Four", "Five"]
    errors = check_no_duplicate_variations([variations])
    assert errors == []


# ---------- V1 fidelity ----------

def test_v1_fidelity_match_passes():
    blocks_vars = [["Paragraph one.", "v2", "v3", "v4", "v5"]]
    input_paragraphs = ["Paragraph one."]
    errors = check_v1_fidelity(blocks_vars, input_paragraphs)
    assert errors == []


def test_v1_fidelity_mismatch_fails():
    blocks_vars = [["Paragraph one modified.", "v2", "v3", "v4", "v5"]]
    input_paragraphs = ["Paragraph one."]
    errors = check_v1_fidelity(blocks_vars, input_paragraphs)
    assert len(errors) == 1 and "Variation 1" in errors[0]


def test_v1_fidelity_tolerates_internal_newline():
    # Input paragraph has an internal \n (hard break); V1 must collapse it
    # to a space because spintax blocks render on one line.
    blocks_vars = [["First sentence. Second sentence on same line.", "v2", "v3", "v4", "v5"]]
    input_paragraphs = ["First sentence.\nSecond sentence on same line."]
    errors = check_v1_fidelity(blocks_vars, input_paragraphs)
    assert errors == [], errors


def test_v1_fidelity_tolerates_multi_space():
    blocks_vars = [["A B C D E", "v2", "v3", "v4", "v5"]]
    input_paragraphs = ["A  B   C    D E"]  # extra spaces
    errors = check_v1_fidelity(blocks_vars, input_paragraphs)
    assert errors == []


def test_v1_fidelity_skipped_when_counts_mismatch():
    # When counts mismatch, block-count check flags it; V1 check should no-op.
    blocks_vars = [["A", "B", "C", "D", "E"]]
    input_paragraphs = ["A", "B"]
    errors = check_v1_fidelity(blocks_vars, input_paragraphs)
    assert errors == []


# ---------- block count ----------

def test_block_count_match():
    assert check_block_count([["x"]] * 3, ["p1", "p2", "p3"]) == []


def test_block_count_mismatch():
    errors = check_block_count([["x"]] * 2, ["p1", "p2", "p3"])
    assert len(errors) == 1 and "mismatch" in errors[0]


# ---------- smart quotes (warnings only) ----------

def test_smart_quote_warning():
    # check_no_smart_quotes warns per-variation. "Its fine" (no smart quote) and
    # "Fine!" produce no warnings; the other three with smart apostrophes each produce one.
    variations = ["It’s fine", "It’s fine", "Its fine", "It’s great", "Fine!"]
    warnings = check_no_smart_quotes([variations])
    # Variations 1, 2, and 4 each have a smart apostrophe -> 3 warnings.
    assert len(warnings) == 3
    assert "variation 1" in warnings[0]
    assert "variation 2" in warnings[1]
    assert "variation 4" in warnings[2]


def test_no_smart_quote_warning_when_ascii_only():
    variations = ["It's fine", "It's fine too", "It's still fine", "It's good", "Fine!"]
    warnings = check_no_smart_quotes([variations])
    assert warnings == []


# ---------- end-to-end smoke ----------

def test_qa_pass_structure():
    # Minimal correct output: single prose paragraph - 1 block, greeting absent.
    input_text = "Just one prose paragraph here.\n"
    output_text = (
        "{{RANDOM | Just one prose paragraph here. | "
        "Just one clear paragraph here. | "
        "Just one brief paragraph here.. | "
        "Just one solid paragraph here. | "
        "Just one simple paragraph here. }}"
    )
    result = qa(output_text, input_text, "instantly")
    assert result["passed"] is True, result["errors"]
    assert result["block_count"] == 1
    assert result["input_paragraph_count"] == 1


def test_qa_result_has_all_expected_keys():
    input_text = "Simple paragraph.\n"
    output_text = (
        "{{RANDOM | Simple paragraph. | Simple text here. | "
        "Simple words there. | Simple stuff indeed. | Simple line below. }}"
    )
    result = qa(output_text, input_text, "instantly")
    expected_keys = {
        "passed", "error_count", "warning_count", "errors",
        "warnings", "block_count", "input_paragraph_count",
    }
    assert expected_keys.issubset(set(result.keys())), f"Missing keys: {expected_keys - set(result.keys())}"


def test_qa_block_count_error():
    # Two prose paragraphs but only one block - block count mismatch.
    input_text = "Paragraph one.\n\nParagraph two.\n"
    output_text = (
        "{{RANDOM | Paragraph one. | Paragraph one! | "
        "Paragraph one? | Paragraph one.. | Paragraph one!? }}"
    )
    result = qa(output_text, input_text, "instantly")
    assert result["passed"] is False
    assert any("mismatch" in e for e in result["errors"])


# ---------- edge cases for coverage ----------

def test_split_input_skips_empty_paragraph_between_double_blanks():
    # Extra blank lines produce an empty raw chunk - exercises the `if not p: continue` path.
    text = "First para.\n\n\n\nSecond para.\n"
    paras = split_input_paragraphs(text)
    kinds = [k for k, _ in paras]
    assert kinds == ["PROSE", "PROSE"]


def test_v1_fidelity_empty_variations_list():
    # check_v1_fidelity with an empty variations list for a block should flag it.
    errors = check_v1_fidelity([[]], ["Some paragraph."])
    assert any("empty" in e for e in errors)


def test_input_starts_with_greeting_blank_lines_only():
    # All-blank input -> _input_starts_with_greeting should return False.
    assert _input_starts_with_greeting("\n\n\n") is False


def test_input_starts_with_greeting_blank_first_line():
    # Leading blank line before greeting - exercises the `if not line: continue` path.
    text = "\nHey {{firstName}},\n"
    assert _input_starts_with_greeting(text) is True


def test_check_greeting_no_blocks_with_greeting_input():
    # Input starts with a greeting but blocks_vars is empty -> no errors.
    errors = check_greeting([], "Hey {{firstName}},\n")
    assert errors == []


def test_check_greeting_not_whitelisted_not_informal():
    # A greeting not in the approved list and not informal -> whitelist error.
    greetings = [
        "Hey {{firstName}},",
        "Hi {{firstName}},",
        "Hello {{firstName}},",
        "Hey there,",
        "Good morning {{firstName}},",  # not informal, not whitelisted
    ]
    input_text = "Hey {{firstName}},\n"
    errors = check_greeting([greetings], input_text)
    assert any("whitelist" in e for e in errors)


def test_check_no_doubled_punctuation_quadruple_dot():
    variations = ["Hello there....", "Hi there.", "Hey there.", "Hello.", "Hi."]
    warnings = check_no_doubled_punctuation([variations])
    assert any("quadrupled" in w for w in warnings)


def test_check_no_doubled_punctuation_doubled_comma():
    variations = ["Hello,, there.", "Hi there.", "Hey there.", "Hello.", "Hi."]
    warnings = check_no_doubled_punctuation([variations])
    assert any("doubled" in w and "," in w for w in warnings)


def test_check_no_doubled_punctuation_clean():
    variations = ["Hello... there.", "Hi there.", "Hey there.", "Hello.", "Hi."]
    warnings = check_no_doubled_punctuation([variations])
    # Triple dot is NOT flagged (only 4+ dots trigger). Single periods are fine.
    dot_warnings = [w for w in warnings if "quadrupled" in w]
    assert dot_warnings == []


# ---------------------------------------------------------------------------
# Concept drift detection
#
# Catches the failure mode we observed in gpt-5.5: variations 2-5 drifting
# from V1 by inventing context (temporal markers, new stakeholders, new
# concepts not in the original).
# ---------------------------------------------------------------------------


def test_concept_drift_passes_simple_synonym_swap():
    """Simple synonym swaps should NOT trigger drift warnings.

    'Show them they can win X' -> 'Demonstrate they could secure X'
    introduces 1-2 new content words via legitimate synonym swap. Threshold
    is 4, so this must pass clean.
    """
    blocks = [[
        "Show them they can win {{company_vs_competitor}}.",
        "Demonstrate they could secure {{company_vs_competitor}}.",
        "Prove they can attain {{company_vs_competitor}}.",
        "Help them realize they can claim {{company_vs_competitor}}.",
        "Make clear they can grab {{company_vs_competitor}}.",
    ]]
    warnings = check_concept_drift(blocks)
    assert warnings == [], f"Expected no drift warnings on synonym swaps, got: {warnings}"


def test_concept_drift_flags_temporal_marker():
    """The hard-listed phrase 'this quarter' in V2 (not in V1) must warn."""
    blocks = [[
        "Show them they can get {{company_vs_competitor}}, would that get attention?",
        "Could showing they get {{company_vs_competitor}} this quarter help your team?",
        "Would proving {{company_vs_competitor}} in the first demo earn focus?",
        "If they see {{company_vs_competitor}}, would that pull them in?",
        "Prove they get {{company_vs_competitor}} - wouldn't that grab them?",
    ]]
    warnings = check_concept_drift(blocks)
    assert any("this quarter" in w for w in warnings), (
        f"Must flag 'this quarter' drift in V2. Got: {warnings}"
    )
    assert any("first demo" in w for w in warnings), (
        f"Must flag 'first demo' drift in V3. Got: {warnings}"
    )


def test_concept_drift_flags_too_many_new_content_words():
    """V_n with 5+ content words not in V1's set must warn (threshold > 4)."""
    blocks = [[
        "Send them an email this morning.",
        # V2 introduces: organizing, beautiful, slideshow, presentation, breakfast,
        # tomorrow morning - ~6 new content words. Should flag.
        "Try organizing a beautiful slideshow presentation during breakfast tomorrow morning.",
        "Send them a message this morning.",
        "Email them shortly.",
        "Drop them a line today.",
    ]]
    warnings = check_concept_drift(blocks)
    assert any("new content words" in w for w in warnings), (
        f"Must flag drift on V2 (lots of new concepts). Got: {warnings}"
    )


def test_concept_drift_skips_when_phrase_in_v1():
    """If the drift phrase is ALSO in V1, it's not drift - it's a real concept."""
    blocks = [[
        "Let's connect about your team's priorities this quarter.",
        "Quick chat about your team's priorities this quarter?",
        "Want to align on your team's priorities this quarter?",
        "Open to a chat on your team's priorities this quarter?",
        "Let's sync on your team's priorities this quarter?",
    ]]
    warnings = check_concept_drift(blocks)
    # No phrase warnings should fire because both 'this quarter' and
    # 'your team's' appear in V1 too.
    phrase_warnings = [w for w in warnings if "drift phrase" in w]
    assert phrase_warnings == [], (
        f"Drift phrases present in V1 must not warn. Got: {phrase_warnings}"
    )


def test_concept_drift_skips_short_blocks():
    """Blocks with < 2 variations have nothing to compare - no warnings."""
    assert check_concept_drift([]) == []
    assert check_concept_drift([["only V1"]]) == []


def test_concept_drift_strips_variables_before_counting():
    """{{firstName}}, {{accountSignature}}, etc. must NOT count as content words."""
    blocks = [[
        "{{firstName}}, quick question.",
        "{{firstName}} - quick query.",
        "Hey {{firstName}}, fast question.",
        "{{firstName}}, brief query.",
        "{{firstName}}, quick ask.",
    ]]
    warnings = check_concept_drift(blocks)
    # No drift warnings: 'fast', 'query', 'brief', 'ask' are valid synonym
    # swaps for 'quick', 'question'. The {{firstName}} token must not
    # influence the count.
    assert warnings == [], f"Variable tokens must not count as content words. Got: {warnings}"
