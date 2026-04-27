"""Unit tests for app/lint.py - ported from upstream test_spintax_lint.py.

Covers:
- Basic PASS/FAIL behavior
- Tolerance floor for short blocks
- Em-dash detection
- Banned word detection
- Variation count enforcement
- Nested brace / depth-aware parser
- EmailBison variable casing
- Invisible character detection
- Greeting block exemption
- Empty / no-block input
"""

from app.lint import (
    lint,
    extract_blocks,
    _split_variations,
    _extract_instantly_blocks,
    _extract_emailbison_blocks,
    is_greeting_block,
)


# ---------- PASS cases ----------

def test_instantly_minimal_pass():
    text = (
        "{{RANDOM | Hello there friend. | Hello there buddy. | "
        "Hello there mate. | Hello there pal. | Hello there dear. }}"
    )
    errors, warnings = lint(text, "instantly", 0.05, 3)
    assert errors == [], errors


def test_emailbison_minimal_pass():
    text = "{aaa|bbb|ccc|ddd|eee}"
    errors, warnings = lint(text, "emailbison", 0.05, 3)
    assert errors == [], errors


# ---------- length tolerance ----------

def test_length_fail_outside_tolerance():
    # base=30 chars, var 3 is 18 chars (40% shorter) - well outside 5% and 3-char floor
    text = (
        "{{RANDOM | " + "a" * 30 + " | " + "b" * 30 + " | " + "c" * 18 + " | "
        + "d" * 30 + " | " + "e" * 30 + "}}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("variation 3" in e and "length" in e.lower() for e in errors), errors


def test_length_floor_allows_short_block_small_diff():
    # base=20 chars, other variations within +/-3 chars (floor > 5% of 20 = 1)
    text = (
        "{{RANDOM | " + "a" * 20 + " | " + "b" * 23 + " | " + "c" * 17 + " | "
        + "d" * 21 + " | " + "e" * 19 + "}}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    length_errors = [e for e in errors if "length" in e.lower()]
    assert length_errors == [], f"Floor of 3 should permit these short-block diffs: {length_errors}"


def test_length_floor_does_not_cover_large_diffs_on_short_block():
    # base=20 chars, var 3 is 15 chars (diff 5, over 3-char floor)
    text = (
        "{{RANDOM | " + "a" * 20 + " | " + "b" * 20 + " | " + "c" * 15 + " | "
        + "d" * 20 + " | " + "e" * 20 + "}}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("variation 3" in e for e in errors), errors


# ---------- invisible characters ----------

def test_zero_width_space_is_error():
    # Model attempt to pad length with U+200B zero-width space
    text = (
        "{{RANDOM | Hello there friend. | Hello there buddy. | "
        "Hello there pal​​. | Hello there man. | Hello there dude. }}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("invisible" in e.lower() and "variation 3" in e for e in errors), errors


def test_word_joiner_is_error():
    text = (
        "{{RANDOM | Hey there friend. | Hey there buddy. | "
        "Hey there pal⁠. | Hey there man. | Hey there dude. }}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("invisible" in e.lower() for e in errors), errors


# ---------- em-dash ----------

def test_em_dash_is_error():
    text = (
        "{{RANDOM | Hello there friend. | Hello there buddy. | "
        "Hello — there mate. | Hello there pal. | Hello there dear. }}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("em-dash" in e for e in errors)


# ---------- banned words ----------

def test_banned_word_is_error():
    text = (
        "{{RANDOM | Hello there friend. | Hello there buddy. | "
        "Hello utilize there. | Hello there pal. | Hello there dear. }}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("utilize" in e for e in errors)


# ---------- variation count ----------

def test_only_four_variations_fails():
    text = "{{RANDOM | aa | bb | cc | dd}}"
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("expected 5" in e or "variation count" in e for e in errors)


# ---------- depth-aware parsing ----------

def test_nested_variables_do_not_break_block_boundary():
    # variables like {{firstName}} inside each variation
    text = (
        "{{RANDOM | Hey {{firstName}}, how are things today? | "
        "Hi {{firstName}}, hope things go well! | "
        "Hello {{firstName}}, trust all is well. | "
        "Hi there {{firstName}}, hope it's good. | "
        "Hey there {{firstName}}, how're things?? }}"
    )
    blocks = _extract_instantly_blocks(text)
    assert len(blocks) == 1, f"Expected 1 block, got {len(blocks)}"
    _, block_text = blocks[0]
    variations = _split_variations(block_text, "instantly")
    assert len(variations) == 5, f"Expected 5 variations, got {len(variations)}"
    # Each variation must still contain its variable
    assert all("{{firstName}}" in v for v in variations)


def test_emailbison_nested_variable_not_split_on_inner_pipe():
    # EmailBison uses {v1|v2|...} - we must not split on pipes inside {VAR}
    text = "{Hello {FIRST}, how's it|Hi {FIRST}, what's up|Hey {FIRST}, good day|Hello {FIRST} buddy|Hi {FIRST}!}"
    blocks = _extract_emailbison_blocks(text)
    assert len(blocks) == 1, f"Expected 1 block, got {len(blocks)}"
    _, inner = blocks[0]
    variations = _split_variations(inner, "emailbison")
    assert len(variations) == 5, variations


# ---------- variable casing ----------

def test_emailbison_lowercase_variable_flagged():
    text = "{Hello {firstName},|Hi {firstName},|Hey {firstName},|Hello {firstName}!|Hi {firstName}?}"
    errors, _ = lint(text, "emailbison", 0.05, 3)
    assert any("firstName" in e and "CAPS" in e for e in errors)


def test_emailbison_uppercase_variable_ok():
    text = "{Hello {FIRSTNAME},|Hi {FIRSTNAME},|Hey {FIRSTNAME},|Hello {FIRSTNAME}!|Hi {FIRSTNAME}?}"
    errors, _ = lint(text, "emailbison", 0.05, 3)
    casing_errors = [e for e in errors if "CAPS" in e]
    assert casing_errors == []


# ---------- empty input ----------

def test_empty_input_yields_error():
    errors, _ = lint("", "instantly", 0.05, 3)
    assert errors, "Empty input should produce at least one error"


def test_text_without_spintax_blocks_yields_error():
    errors, _ = lint("Just some plain text.", "instantly", 0.05, 3)
    assert any("no spintax blocks" in e for e in errors)


# ---------- greeting block exemption ----------

def test_greeting_block_exempt_from_length_check():
    # "Hey {{firstName}}," is 18 chars, "Hey there," is 10 chars.
    # Without exemption the 8-char diff would fail. With exemption it passes.
    text = (
        "{{RANDOM | Hey {{firstName}}, | Hi {{firstName}}, | "
        "Hello {{firstName}}, | Hey there, | {{firstName}}, }}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    length_errors = [e for e in errors if "length" in e.lower()]
    assert length_errors == [], f"Greeting block should be exempt: {length_errors}"


def test_non_greeting_block_still_length_checked():
    # Even if one line has `Hey {{firstName}},` in it, if it's actually a
    # longer sentence, the block is not a greeting block and length applies.
    text = (
        "{{RANDOM | Hey {{firstName}}, how is everything? | "
        "Hey there | "
        "Hi buddy! | "
        "Hello hello | "
        "Yo wassup }}"
    )
    errors, _ = lint(text, "instantly", 0.05, 3)
    length_errors = [e for e in errors if "length" in e.lower()]
    assert length_errors != [], "Non-greeting block must still be length-checked"


# ---------- is_greeting_block unit tests ----------

def test_is_greeting_block_true():
    variations = [
        "Hey {{firstName}},",
        "Hi {{firstName}},",
        "Hello {{firstName}},",
        "Hey there,",
        "{{firstName}},",
    ]
    assert is_greeting_block(variations) is True


def test_is_greeting_block_false_with_non_greeting():
    variations = ["Hello world", "Hi there friend", "Good morning", "Hey mate", "Howdy"]
    assert is_greeting_block(variations) is False


def test_is_greeting_block_empty_returns_false():
    assert is_greeting_block([]) is False


# ---------- extract_blocks dispatch ----------

def test_extract_blocks_instantly():
    text = "{{RANDOM | a | b | c | d | e}}"
    blocks = extract_blocks(text, "instantly")
    assert len(blocks) == 1


def test_extract_blocks_emailbison():
    text = "{a|b|c|d|e}"
    blocks = extract_blocks(text, "emailbison")
    assert len(blocks) == 1


# ---------- additional edge-case coverage ----------

def test_instantly_unclosed_block_returns_no_blocks():
    # Block opened but never closed - parser should not crash, returns empty.
    text = "{{RANDOM | var1 | var2 | var3 | var4 | var5"
    blocks = _extract_instantly_blocks(text)
    assert blocks == []


def test_emailbison_no_top_level_pipe_not_extracted():
    # A single-brace group with no pipe at depth 0 is not a spintax block.
    text = "{FIRSTNAME}"
    blocks = _extract_emailbison_blocks(text)
    assert blocks == []


def test_emailbison_unclosed_brace_skipped():
    # Unclosed brace - parser reaches end of string without closing; should
    # fall through the while/else and advance i past the opening brace.
    text = "{var1|var2"
    blocks = _extract_emailbison_blocks(text)
    assert blocks == []


def test_check_length_empty_variation_1():
    # Variation 1 is empty string - should produce a specific error.
    from app.lint import check_length
    variations = ["", "b", "c", "d", "e"]
    errors = check_length(variations, 0.05, 3)
    assert any("empty" in e.lower() for e in errors), errors


def test_spam_trigger_produces_warning():
    # "urgent" is in SPAM_TRIGGERS - should appear as a warning, not error.
    text = (
        "{{RANDOM | This is urgent please read. | Hello there buddy. | "
        "Hello there mate. | Hello there pal. | Hello there dear. }}"
    )
    errors, warnings = lint(text, "instantly", 0.05, 3)
    assert any("urgent" in w for w in warnings), warnings


def test_greeting_block_wrong_variation_count_flagged():
    # A greeting block with only 4 variations should flag count error.
    text = "{{RANDOM | Hey {{firstName}}, | Hi {{firstName}}, | Hello {{firstName}}, | Hey there,}}"
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("variation count" in e or "expected 5" in e for e in errors), errors


# ---------- edge cases for coverage ----------

def test_has_no_top_level_pipe_returns_no_emailbison_block():
    # A single-brace group with no pipe at depth 0 is not a spintax block.
    # This exercises the _has_top_level_pipe -> False path and the emailbison
    # extractor's path where the brace group is skipped.
    text = "{nopiphere}"
    blocks = extract_blocks(text, "emailbison")
    assert blocks == []


def test_emailbison_extractor_skips_non_brace_chars():
    # Text that doesn't start with '{' exercises the i+=1 / continue path
    # in _extract_emailbison_blocks.
    text = "hello world {a|b|c|d|e} trailing"
    blocks = extract_blocks(text, "emailbison")
    assert len(blocks) == 1


def test_instantly_unclosed_block_returns_empty():
    # An unclosed {{RANDOM | ... block exercises the `if not closed: break` path.
    text = "{{RANDOM | a | b | c | d "
    blocks = extract_blocks(text, "instantly")
    assert blocks == []


def test_check_length_empty_variation1():
    # Variation 1 is empty - exercises the base_len == 0 guard.
    from app.lint import check_length
    issues = check_length(["", "b", "c", "d", "e"], 0.05, 3)
    assert any("empty" in e.lower() for e in issues)


def test_spam_trigger_flagged_as_warning():
    # Using a known spam trigger word produces a warning (not error).
    from app.lint import SPAM_TRIGGERS
    if not SPAM_TRIGGERS:
        return  # nothing to test if list is empty
    trigger = list(SPAM_TRIGGERS)[0]
    text = (
        f"{{{{RANDOM | Hello {trigger} friend. | Hello there buddy. | "
        f"Hello there mate. | Hello there pal. | Hello there dear. }}}}"
    )
    errors, warnings = lint(text, "instantly", 0.05, 3)
    assert any(trigger.lower() in w.lower() for w in warnings)


def test_greeting_block_with_wrong_count_flagged():
    # A greeting block that has != 5 variations gets a count error.
    text = "{{RANDOM | Hey {{firstName}}, | Hi {{firstName}}, | Hello {{firstName}}, | Hey there,}}"
    errors, _ = lint(text, "instantly", 0.05, 3)
    assert any("variation count" in e for e in errors)


def test_emailbison_unclosed_brace_skipped():
    # An unclosed '{' in EmailBison text exercises the else: i+=1 path in the
    # inner while loop (j reaches end of text without finding '}'.
    text = "{unclosed"
    blocks = extract_blocks(text, "emailbison")
    assert blocks == []
