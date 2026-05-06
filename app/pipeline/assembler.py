"""Stage 5 — Assembler (pure code, no LLM calls).

Takes the ordered BlockList and per-block VariantSets produced by Stage 4
(block spintaxer) and stitches them into a single platform-correct spintax
string.

Platform syntax (the format the QA / lint validators expect):

  - ``instantly``  -> ``{{RANDOM | V1 | V2 | V3 | V4 | V5}}``
  - ``emailbison`` -> ``{V1|V2|V3|V4|V5}`` (single brace)

Algorithm:
  For each block in block_list.blocks (original order preserved):
    - lockable=False  -> emit block.text literally (no spintax wrapping).
    - lockable=True with all 5 variants identical -> emit V1 only.
    - lockable=True with at least one distinct variant -> emit the
      platform-specific wrapper above.
  Fragments are joined with a single newline to preserve the
  paragraph structure of the original email body.

Whitespace / paragraph structure note:
  The splitter (paragraph-level since Wave 5) strips the inter-paragraph
  newline separators when producing block.text - each block holds one
  paragraph's content verbatim. The assembler reinstates a single newline
  between fragments, so the output spintax has the same paragraph layout
  as the input email. Email platforms (Instantly, EmailBison) render the
  resulting newlines as paragraph breaks.
"""

from __future__ import annotations

from app.pipeline.contracts import AssembledSpintax, Block, BlockList, VariantSet

# Platform -> wrapper format.  ``{vars}`` is replaced by the joined variants.
_PLATFORM_WRAPPERS: dict[str, str] = {
    "instantly": "{{{{RANDOM | {vars}}}}}",
    "emailbison": "{{{vars}}}",
}
# Per-platform separator between variants inside the wrapper. Instantly's
# real renderer is whitespace-tolerant; spaced pipes match the format
# documented for the platform and what the lint validators emit too.
_PLATFORM_SEPARATORS: dict[str, str] = {
    "instantly": " | ",
    "emailbison": "|",
}


def assemble_spintax(
    block_list: BlockList,
    variant_sets: list[VariantSet],
    *,
    platform: str = "emailbison",
) -> AssembledSpintax:
    """Stitch per-block V1-V5 results into the final spintax string.

    For each block in block_list (in original order):
      - If lockable=False: emit V1 only (no curly-brace wrapping). The
        block.text is emitted exactly as-is, including any ``{{placeholder}}``
        tokens it may contain.
      - If lockable=True with all 5 variants identical: emit V1 only.
      - If lockable=True with at least one distinct variant: emit the
        platform-specific wrapper.

    Blocks are joined with a single space. See module docstring for the
    whitespace caveat.

    Args:
        block_list: Ordered list of blocks from Stage 1.
        variant_sets: One VariantSet per lockable block from Stage 4.
        platform: ``"instantly"`` or ``"emailbison"``. Determines the wrapper
            syntax used for blocks with non-identical variants. Defaults to
            ``"emailbison"`` for backwards compatibility with Wave 2 callers
            that did not pass platform.

    Returns:
        AssembledSpintax with the final joined spintax string.

    Raises:
        ValueError: If platform is unknown, a lockable block is missing from
            variant_sets, or if a VariantSet does not contain exactly 5
            variants.
    """
    if platform not in _PLATFORM_WRAPPERS:
        raise ValueError(
            f"unknown platform {platform!r}; "
            f"expected one of {sorted(_PLATFORM_WRAPPERS)}"
        )
    wrapper = _PLATFORM_WRAPPERS[platform]
    sep = _PLATFORM_SEPARATORS[platform]

    # Build O(1) lookup from block_id -> VariantSet.
    vs_by_id: dict[str, VariantSet] = {vs.block_id: vs for vs in variant_sets}

    fragments: list[str] = []

    for block in block_list.blocks:
        if not block.lockable:
            # Pure placeholder or too-short block - pass through verbatim.
            fragments.append(block.text)
            continue

        # Lockable block - we need a VariantSet.
        vs = vs_by_id.get(block.id)
        if vs is None:
            raise ValueError(
                f"missing variant set for block '{block.id}' - "
                "orchestrator must supply a VariantSet for every lockable block"
            )

        if len(vs.variants) != 5:
            raise ValueError(
                f"VariantSet for block '{block.id}' has {len(vs.variants)} variants "
                f"but exactly 5 are required (V1-V5)"
            )

        # Collapse to V1 if all variants are identical - avoids redundant spintax.
        if len(set(vs.variants)) == 1:
            fragments.append(vs.variants[0])
        else:
            fragments.append(wrapper.format(vars=sep.join(vs.variants)))

    return AssembledSpintax(spintax="\n".join(fragments))
