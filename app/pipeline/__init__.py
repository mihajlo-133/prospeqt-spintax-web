"""Block-first spintax pipeline (beta).

This package implements the beta_v1 spintax pipeline described in
BETA_BLOCK_FIRST_SPEC.md. It runs in parallel to the alpha (whole-email)
path in app/spintax_runner.py and is selected via the SPINTAX_PIPELINE
env var.

Pipeline stages:
    1. splitter        - LLM-based sentence splitter
    2. profiler        - tone + locked nouns + proper nouns
    3. synonym_pool    - per-block synonym pool (batched LLM call)
    4. block_spintaxer - per-block parallel V1-V5 generation
    5. assembler       - stitch blocks back into spintax format
    6. validators      - reuse Jaccard/length/lint/drift from app/qa/

The pipeline_runner module orchestrates 1-5 + retries. spintax_runner_v2
wraps pipeline_runner with the same job state machine alpha uses.

All inter-stage data shapes are defined in contracts.py. Stages depend on
contracts only, so they can be built and tested independently.
"""
