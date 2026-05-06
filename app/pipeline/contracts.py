"""Pydantic models that define the data contract between pipeline stages.

Every stage's input and output is typed via these models so modules can
be built and tested in isolation. Changing a contract is a breaking
change across the pipeline; treat this file as load-bearing.

Stage flow:
    plain_body -> Splitter -> BlockList
    plain_body -> Profiler -> Profile
    BlockList + Profile -> SynonymPoolGenerator -> SynonymPool
    Block + BlockPoolEntry + Profile -> BlockSpintaxer -> VariantSet
    BlockList + list[VariantSet] -> Assembler -> AssembledSpintax
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Splitter contracts
# ---------------------------------------------------------------------------


class Block(BaseModel):
    """A single sentence-level block of an email.

    Produced by the splitter. `lockable` is set in code AFTER the LLM
    returns: True if the block has spintaxable content, False if it is
    a pure placeholder (e.g., "{{customLink}}") or too short to vary.
    """

    id: str = Field(description="Stable id, e.g. 'block_1', 'block_2'")
    text: str = Field(description="Sentence text exactly as it appears in V1")
    lockable: bool = Field(
        default=True,
        description=(
            "True if this block can be spintaxed. False for pure-placeholder "
            "blocks that should pass through V1 unchanged. Set in code."
        ),
    )


class BlockList(BaseModel):
    """Splitter output: ordered list of blocks for one email."""

    blocks: list[Block] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Profiler contracts
# ---------------------------------------------------------------------------


class Profile(BaseModel):
    """Profiler output: tone descriptor + words to lock during spintax.

    `proper_nouns` is the union of a regex pre-pass (capitalized
    multi-word phrases) and the LLM's additions. The spintaxer must
    preserve every entry in `locked_common_nouns` and `proper_nouns`
    exactly.
    """

    tone: str = Field(description="Short phrase: register and voice")
    audience_hint: str | None = Field(
        default=None,
        description="Inferred audience (e.g. 'law firms') or None",
    )
    locked_common_nouns: list[str] = Field(
        default_factory=list,
        description="Domain-specific common nouns the spintaxer must NOT swap",
    )
    proper_nouns: list[str] = Field(
        default_factory=list,
        description="Brand / company / product names to preserve exactly",
    )


# ---------------------------------------------------------------------------
# Synonym pool contracts
# ---------------------------------------------------------------------------


class BlockPoolEntry(BaseModel):
    """The synonym pool and syntax options for ONE block.

    `synonyms` maps original content words to lists of register-matched
    substitutes. Function words, locked nouns, and proper nouns are NOT
    keys here (they are not substitutable).

    `syntax_options` lists alternative phrasings of the entire sentence
    that preserve meaning. The spintaxer can pick one as a starting
    template, then apply synonym substitutions.
    """

    synonyms: dict[str, list[str]] = Field(default_factory=dict)
    syntax_options: list[str] = Field(default_factory=list)


class SynonymPool(BaseModel):
    """Synonym pool generator output: one BlockPoolEntry per lockable block.

    Keyed by Block.id (e.g. 'block_1'). Unlockable blocks are absent
    from this dict.
    """

    blocks: dict[str, BlockPoolEntry] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Block spintaxer contracts
# ---------------------------------------------------------------------------


class VariantSet(BaseModel):
    """Block spintaxer output for ONE block.

    `variants[0]` is V1 and must equal the original block text.
    `variants[1..4]` are V2-V5 generated from the synonym pool and
    syntax options. The list is always length 5.
    """

    block_id: str = Field(description="Matches Block.id from BlockList")
    variants: list[str] = Field(
        description="Exactly 5 entries: V1 (original), V2, V3, V4, V5",
    )


# ---------------------------------------------------------------------------
# Assembler contract
# ---------------------------------------------------------------------------


class AssembledSpintax(BaseModel):
    """Final assembler output: the spintax string ready for validators."""

    spintax: str = Field(description="Joined {V1|V2|V3|V4|V5} blocks")


# ---------------------------------------------------------------------------
# Pipeline-level diagnostics (attached to the job result for observability)
# ---------------------------------------------------------------------------


class StageDuration(BaseModel):
    """How long a single LLM stage took, in milliseconds."""

    duration_ms: int = 0


class SplitterDiagnostics(StageDuration):
    block_count: int = 0
    lockable_count: int = 0


class ProfilerDiagnostics(StageDuration):
    tone: str = ""
    locked_nouns: list[str] = Field(default_factory=list)
    proper_nouns: list[str] = Field(default_factory=list)


class SynonymPoolDiagnostics(StageDuration):
    total_synonyms: int = 0
    blocks_covered: int = 0


class BlockSpintaxerDiagnostics(BaseModel):
    blocks_completed: int = 0
    blocks_retried: int = 0
    max_retries_per_block: int = 0
    p95_block_duration_ms: int = 0


class PipelineDiagnostics(BaseModel):
    """Top-level diagnostics object surfaced at /api/status for beta jobs."""

    pipeline: str = "beta_v1"
    splitter: SplitterDiagnostics = Field(default_factory=SplitterDiagnostics)
    profiler: ProfilerDiagnostics = Field(default_factory=ProfilerDiagnostics)
    synonym_pool: SynonymPoolDiagnostics = Field(
        default_factory=SynonymPoolDiagnostics
    )
    block_spintaxer: BlockSpintaxerDiagnostics = Field(
        default_factory=BlockSpintaxerDiagnostics
    )


# ---------------------------------------------------------------------------
# Pipeline error keys (raised by stage modules, mapped to job error_key)
# ---------------------------------------------------------------------------


ERR_SPLITTER = "splitter_error"
ERR_PROFILER = "profiler_error"
ERR_SYNONYM_POOL = "synonym_pool_error"
ERR_BLOCK_SPINTAX = "block_spintax_error"


class PipelineStageError(Exception):
    """Raised by any pipeline stage on unrecoverable failure.

    Carries the canonical error_key the runner attaches to the job.
    """

    def __init__(self, error_key: str, detail: str = "") -> None:
        super().__init__(detail or error_key)
        self.error_key = error_key
        self.detail = detail


# Lockable detection threshold. Blocks whose text minus placeholders is
# shorter than this are treated as pure-placeholder pass-throughs.
MIN_SPINTAXABLE_CHARS = 8
