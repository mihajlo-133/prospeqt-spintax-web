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
