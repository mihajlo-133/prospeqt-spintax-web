"""Unit tests for Stage 5 — Assembler (pure code, no async, no mocks).

All tests are synchronous. No external services, no LLM calls.
"""

import pytest

from app.pipeline.assembler import assemble_spintax
from app.pipeline.contracts import AssembledSpintax, Block, BlockList, VariantSet


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_block(block_id: str, text: str, lockable: bool = True) -> Block:
    return Block(id=block_id, text=text, lockable=lockable)


def make_vs(block_id: str, variants: list[str]) -> VariantSet:
    return VariantSet(block_id=block_id, variants=variants)


FIVE_DISTINCT = ["V1 text", "V2 text", "V3 text", "V4 text", "V5 text"]
FIVE_IDENTICAL = ["x", "x", "x", "x", "x"]


# ---------------------------------------------------------------------------
# Test 1: Happy path — 2 lockable blocks each with 5 distinct variants
# ---------------------------------------------------------------------------


def test_happy_path_two_lockable_blocks():
    v1 = ["Alpha one", "Alpha two", "Alpha three", "Alpha four", "Alpha five"]
    v2 = ["Beta one", "Beta two", "Beta three", "Beta four", "Beta five"]

    block_list = BlockList(blocks=[make_block("block_1", "Alpha one"), make_block("block_2", "Beta one")])
    variant_sets = [make_vs("block_1", v1), make_vs("block_2", v2)]

    result = assemble_spintax(block_list, variant_sets)

    assert isinstance(result, AssembledSpintax)
    assert result.spintax == "{Alpha one|Alpha two|Alpha three|Alpha four|Alpha five}\n{Beta one|Beta two|Beta three|Beta four|Beta five}"


# ---------------------------------------------------------------------------
# Test 2: All-identical variants -> V1 only (no curly-brace wrapping)
# ---------------------------------------------------------------------------


def test_identical_variants_emit_v1_only():
    block_list = BlockList(blocks=[make_block("block_1", "x")])
    variant_sets = [make_vs("block_1", FIVE_IDENTICAL)]

    result = assemble_spintax(block_list, variant_sets)

    assert result.spintax == "x"
    assert "{" not in result.spintax


# ---------------------------------------------------------------------------
# Test 3: lockable=False passthrough — placeholder preserved verbatim
# ---------------------------------------------------------------------------


def test_lockable_false_passthrough():
    placeholder_text = "{{firstName}}"
    block_list = BlockList(blocks=[make_block("block_1", placeholder_text, lockable=False)])
    variant_sets = []  # no variant sets needed for non-lockable blocks

    result = assemble_spintax(block_list, variant_sets)

    assert result.spintax == "{{firstName}}"


# ---------------------------------------------------------------------------
# Test 4: Mixed — lockable+distinct, lockable+identical, unlockable
# ---------------------------------------------------------------------------


def test_mixed_block_types():
    block_list = BlockList(
        blocks=[
            make_block("block_1", "V1 text", lockable=True),
            make_block("block_2", "same", lockable=True),
            make_block("block_3", "{{placeholder}}", lockable=False),
        ]
    )
    variant_sets = [
        make_vs("block_1", FIVE_DISTINCT),
        make_vs("block_2", ["same", "same", "same", "same", "same"]),
    ]

    result = assemble_spintax(block_list, variant_sets)

    # block_1: distinct -> spintax, block_2: identical -> V1, block_3: passthrough.
    # Fragments now separated by \n (paragraph-preserving) since Wave 5.
    assert result.spintax == "{V1 text|V2 text|V3 text|V4 text|V5 text}\nsame\n{{placeholder}}"


# ---------------------------------------------------------------------------
# Test 5: Block order preserved — list order, not id sort order
# ---------------------------------------------------------------------------


def test_block_order_preserved():
    # block_3 appears FIRST in the list, block_1 appears SECOND
    block_list = BlockList(
        blocks=[
            make_block("block_3", "Third", lockable=True),
            make_block("block_1", "First", lockable=True),
        ]
    )
    v3 = ["Third", "Third-2", "Third-3", "Third-4", "Third-5"]
    v1 = ["First", "First-2", "First-3", "First-4", "First-5"]
    variant_sets = [
        make_vs("block_3", v3),
        make_vs("block_1", v1),
    ]

    result = assemble_spintax(block_list, variant_sets)

    # Output must start with block_3's spintax, then block_1's spintax.
    # Wave 5: fragments are joined with newline, not space.
    parts = result.spintax.split("}\n{")
    assert parts[0].startswith("{Third")
    assert parts[1].startswith("First")


# ---------------------------------------------------------------------------
# Test 6: Missing variant set raises ValueError
# ---------------------------------------------------------------------------


def test_missing_variant_set_raises():
    block_list = BlockList(blocks=[make_block("block_2", "Some text", lockable=True)])
    variant_sets = []  # block_2 not provided

    with pytest.raises(ValueError, match="missing variant set for block 'block_2'"):
        assemble_spintax(block_list, variant_sets)


# ---------------------------------------------------------------------------
# Test 7: Wrong variant count raises ValueError
# ---------------------------------------------------------------------------


def test_wrong_variant_count_raises():
    block_list = BlockList(blocks=[make_block("block_1", "text", lockable=True)])
    variant_sets = [make_vs("block_1", ["v1", "v2", "v3", "v4"])]  # only 4, need 5

    with pytest.raises(ValueError, match="4 variants"):
        assemble_spintax(block_list, variant_sets)


# ---------------------------------------------------------------------------
# Test 8: Single-block email — no leading/trailing spaces
# ---------------------------------------------------------------------------


def test_single_block_no_extra_spaces():
    block_list = BlockList(blocks=[make_block("block_1", "Hello there", lockable=True)])
    variant_sets = [make_vs("block_1", FIVE_DISTINCT)]

    result = assemble_spintax(block_list, variant_sets)

    assert not result.spintax.startswith(" ")
    assert not result.spintax.endswith(" ")
    assert result.spintax == "{V1 text|V2 text|V3 text|V4 text|V5 text}"


# ---------------------------------------------------------------------------
# Test 9: Empty block_list -> empty spintax string
# ---------------------------------------------------------------------------


def test_empty_block_list():
    block_list = BlockList(blocks=[])
    variant_sets = []

    result = assemble_spintax(block_list, variant_sets)

    assert isinstance(result, AssembledSpintax)
    assert result.spintax == ""


# ---------------------------------------------------------------------------
# Test 10: Curly-brace placeholder inside variant text preserved
# ---------------------------------------------------------------------------


def test_inner_placeholder_preserved_in_spintax_wrapper():
    variants = [
        "Hi {{firstName}}, we help teams",
        "Hello {{firstName}}, our platform",
        "Hey {{firstName}}, companies use us",
        "Dear {{firstName}}, here is what",
        "Greetings {{firstName}}, we noticed",
    ]
    block_list = BlockList(blocks=[make_block("block_1", variants[0], lockable=True)])
    variant_sets = [make_vs("block_1", variants)]

    result = assemble_spintax(block_list, variant_sets)

    # Outer spintax wrapper present
    assert result.spintax.startswith("{")
    assert result.spintax.endswith("}")
    # Each inner {{firstName}} placeholder preserved
    assert "{{firstName}}" in result.spintax
    # Confirm structure: {v1|v2|v3|v4|v5}
    inner = result.spintax[1:-1]
    parts = inner.split("|")
    assert len(parts) == 5
    assert all("{{firstName}}" in p for p in parts)
