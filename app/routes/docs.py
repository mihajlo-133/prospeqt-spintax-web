"""Public documentation routes for the Prospeqt Spintax API.

What this does:
    Serves the three public doc surfaces:
        GET /docs           -> HTML reference page (templates/docs.html)
        GET /llms.txt       -> LLM-optimized markdown (static/llms.txt)
        GET /openapi.json   -> Hand-built OpenAPI 3.1 spec

    All three routes are PUBLIC (no auth, no session cookie). They mirror
    the spam-checker tool's doc surfaces. The content is canonical for
    AI-agent integrations - keep it in sync with the route handlers.

What it depends on:
    fastapi.APIRouter, fastapi.responses (FileResponse, JSONResponse)
    templates/docs.html and static/llms.txt files on disk
    app.api_models for the schema definitions referenced by the OpenAPI spec

What depends on it:
    app/main.py mounts docs_router via include_router(docs_router) and
    must do so without `dependencies=[Depends(require_auth)]` so the
    routes stay public.
    tests/test_routes_docs.py exercises all three routes unauthenticated.

Design notes:
    - docs.html is served as a STATIC file (FileResponse), NOT through
      Jinja. The page has zero template variables and the content
      contains literal {{firstName}} / {{companyName}} placeholders that
      would conflict with Jinja's template syntax.
    - llms.txt is served as a static file with text/plain mime type.
      Same reason: it contains literal {{firstName}} placeholders.
    - The OpenAPI spec is built by hand (not via FastAPI's
      `app.openapi()`) so we can:
        * control the order of paths and schemas,
        * exclude /admin/* from the public surface,
        * embed the custom x-agent-guidance / x-drift-revision /
          x-error-codes extensions at the top level.
"""

from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import FileResponse, JSONResponse

# Project layout: app/routes/docs.py -> ../../templates and ../../static
_REPO_ROOT: Path = Path(__file__).resolve().parent.parent.parent
_DOCS_HTML_PATH: Path = _REPO_ROOT / "templates" / "docs.html"
_LLMS_TXT_PATH: Path = _REPO_ROOT / "static" / "llms.txt"

router: APIRouter = APIRouter()


# ---------------------------------------------------------------------------
# GET /docs - HTML reference
# ---------------------------------------------------------------------------


@router.get("/docs", include_in_schema=False)
def docs_html() -> FileResponse:
    """Serve the HTML API reference.

    Returned as a static file (NOT Jinja-rendered) because the page has
    no template variables and the content contains literal {{firstName}}
    placeholders that would collide with Jinja syntax.

    Public: no auth gate. Mounted in app/main.py without require_auth.
    """
    return FileResponse(
        _DOCS_HTML_PATH,
        media_type="text/html; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# GET /llms.txt - LLM-optimized markdown
# ---------------------------------------------------------------------------


@router.get("/llms.txt", include_in_schema=False)
def llms_txt() -> FileResponse:
    """Serve the LLM-optimized markdown documentation.

    Plain markdown, served with text/plain mime type. The file contains
    literal {{firstName}} / {{accountSignature}} placeholders that
    document the spintax variable syntax for AI agents - serving as a
    raw file (not through Jinja) preserves them verbatim.

    Public: no auth gate.
    """
    return FileResponse(
        _LLMS_TXT_PATH,
        media_type="text/plain; charset=utf-8",
    )


# ---------------------------------------------------------------------------
# GET /openapi.json - hand-built OpenAPI 3.1 spec
# ---------------------------------------------------------------------------


def _build_openapi_spec() -> dict:
    """Construct the OpenAPI 3.1 spec dict.

    Built by hand (not via FastAPI's app.openapi()) so we can control
    path order, exclude /admin/* from the public surface, and embed the
    custom x-agent-guidance / x-drift-revision / x-error-codes
    extensions at the top level.
    """

    return {
        "openapi": "3.1.0",
        "info": {
            "title": "Prospeqt Spintax API",
            "version": "0.3.0",
            "summary": (
                "Convert plain email copy into platform-specific spintax via "
                "OpenAI / Anthropic reasoning models, with deterministic lint "
                "and QA endpoints."
            ),
            "description": (
                "Stateless HTTP API that wraps OpenAI reasoning models "
                "(o3, gpt-5.x) and Anthropic Claude (opus-4-7, sonnet-4-6) "
                "behind a job interface for converting plain email copy into "
                "Instantly or EmailBison spintax syntax. Also exposes batch "
                "processing for whole markdown sequence files and standalone "
                "deterministic lint and QA endpoints for already-spun copy."
            ),
            "license": {"name": "Proprietary"},
            "contact": {"url": "https://github.com/mihajlo-133/prospeqt-spintax-web"},
        },
        "servers": [
            {
                "url": "https://prospeqt-spintax.onrender.com",
                "description": "Production",
            }
        ],
        "security": [{"bearerAuth": []}],
        "paths": {
            "/api/spintax": {
                "post": {
                    "operationId": "submitSpintaxJob",
                    "summary": "Submit one plain email body for spintax generation.",
                    "description": (
                        "Async. Returns a job_id immediately; poll "
                        "/api/status/{job_id} until status is 'done' or "
                        "'failed'. Single-body jobs typically finish in "
                        "30-90s."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/SpintaxRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Job created.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/SpintaxResponse"}
                                }
                            },
                        },
                        "401": {
                            "description": "Missing or invalid bearer token.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                                }
                            },
                        },
                        "422": {
                            "description": "Invalid input (empty text, bad platform).",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                                }
                            },
                        },
                        "429": {
                            "description": (
                                "Daily spend cap reached. Body includes "
                                "cap_usd, spent_usd, resets_at."
                            ),
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/ErrorEnvelope"}
                                }
                            },
                        },
                    },
                }
            },
            "/api/status/{job_id}": {
                "get": {
                    "operationId": "getJobStatus",
                    "summary": "Poll a single job's state.",
                    "description": (
                        "Jobs are retained in memory for 1 hour after "
                        "creation, then evicted. Terminal states are 'done' "
                        "and 'failed'."
                    ),
                    "parameters": [
                        {
                            "name": "job_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                            "description": "UUID returned from POST /api/spintax.",
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Current job state.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/JobStatusResponse"}
                                }
                            },
                        },
                        "401": {"description": "Missing or invalid bearer token."},
                        "404": {"description": "Job not found or expired (TTL 1h)."},
                    },
                }
            },
            "/api/spintax/batch": {
                "post": {
                    "operationId": "submitBatchJob",
                    "summary": "Spin a whole markdown sequence file.",
                    "description": (
                        "Parses a markdown document into segments and email "
                        "bodies, then spins each Email-1 body concurrently. "
                        "Emails 2-N are passed through unchanged (re-spinning "
                        "follow-ups produces drift). Returns a batch_id; poll "
                        "GET /api/spintax/batch/{batch_id}."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/BatchRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Batch created (or dry-run completed).",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/BatchSubmitResponse"}
                                }
                            },
                        },
                        "401": {"description": "Missing or invalid bearer token."},
                        "422": {
                            "description": (
                                "Empty md, bad platform, or parser found "
                                "zero segments. Body for zero segments: "
                                "{error: 'no_segments_found', message, "
                                "warnings}."
                            )
                        },
                        "500": {"description": "Parser crashed unexpectedly."},
                    },
                }
            },
            "/api/spintax/batch/{batch_id}": {
                "get": {
                    "operationId": "getBatchStatus",
                    "summary": "Poll a batch's state.",
                    "parameters": [
                        {
                            "name": "batch_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Current batch state.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/BatchStatusResponse"}
                                }
                            },
                        },
                        "401": {"description": "Missing or invalid bearer token."},
                        "404": {"description": "Batch not found."},
                    },
                }
            },
            "/api/spintax/batch/{batch_id}/cancel": {
                "post": {
                    "operationId": "cancelBatch",
                    "summary": "Cancel a running batch.",
                    "description": (
                        "In-flight bodies finish naturally; queued bodies "
                        "are skipped. Idempotent - calling on a terminal "
                        "batch returns cancelled: false with a message."
                    ),
                    "parameters": [
                        {
                            "name": "batch_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": ("Cancellation result: {batch_id, status, cancelled}.")
                        },
                        "401": {"description": "Missing or invalid bearer token."},
                        "404": {"description": "Batch not found."},
                    },
                }
            },
            "/api/spintax/batch/{batch_id}/download": {
                "get": {
                    "operationId": "downloadBatchZip",
                    "summary": "Download the result zip.",
                    "description": (
                        "Streams the final .zip containing the spun "
                        "markdown plus a report.md summary."
                    ),
                    "parameters": [
                        {
                            "name": "batch_id",
                            "in": "path",
                            "required": True,
                            "schema": {"type": "string"},
                        }
                    ],
                    "responses": {
                        "200": {
                            "description": "Zip stream.",
                            "content": {"application/zip": {}},
                        },
                        "401": {"description": "Missing or invalid bearer token."},
                        "404": {"description": "Batch not found."},
                        "409": {
                            "description": (
                                "Batch still running. Body: "
                                "{error: 'batch_not_complete', message, "
                                "status}."
                            )
                        },
                    },
                }
            },
            "/api/lint": {
                "post": {
                    "operationId": "lintSpintax",
                    "summary": "Deterministic lint of already-spun copy.",
                    "description": (
                        "Synchronous. No LLM, no cost. Returns errors and "
                        "warnings on the spintax syntax and length-balance "
                        "of variations."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/LintRequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "Lint result.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/LintResponse"}
                                }
                            },
                        },
                        "401": {"description": "Missing or invalid bearer token."},
                        "422": {"description": "Invalid input."},
                    },
                }
            },
            "/api/qa": {
                "post": {
                    "operationId": "qaSpintax",
                    "summary": "Deterministic QA against the original plain input.",
                    "description": (
                        "Synchronous. Verifies V1 fidelity, block count, "
                        "greeting whitelist, duplicate variations, smart "
                        "quotes, doubled punctuation, and concept drift."
                    ),
                    "requestBody": {
                        "required": True,
                        "content": {
                            "application/json": {
                                "schema": {"$ref": "#/components/schemas/QARequest"}
                            }
                        },
                    },
                    "responses": {
                        "200": {
                            "description": "QA result.",
                            "content": {
                                "application/json": {
                                    "schema": {"$ref": "#/components/schemas/QAResponse"}
                                }
                            },
                        },
                        "401": {"description": "Missing or invalid bearer token."},
                        "422": {"description": "Invalid input."},
                    },
                }
            },
        },
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "opaque token (BATCH_API_KEY)",
                }
            },
            "schemas": {
                # ── Spintax ─────────────────────────────────────────────
                "SpintaxRequest": {
                    "type": "object",
                    "required": ["text", "platform"],
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Plain email body to spin.",
                        },
                        "platform": {
                            "type": "string",
                            "enum": ["instantly", "emailbison"],
                            "description": (
                                "Determines spintax syntax: instantly = "
                                "{a|b|c}, emailbison = [spin|a|b|c]."
                            ),
                        },
                        "model": {
                            "type": "string",
                            "nullable": True,
                            "description": ("Model name. Defaults to server OPENAI_MODEL env var."),
                        },
                        "reasoning_effort": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                            "default": "medium",
                            "description": (
                                "Honored for OpenAI o-series and gpt-5.x. Ignored otherwise."
                            ),
                        },
                    },
                },
                "SpintaxResponse": {
                    "type": "object",
                    "required": ["job_id"],
                    "properties": {
                        "job_id": {
                            "type": "string",
                            "description": ("UUID. Poll /api/status/{job_id}."),
                        }
                    },
                },
                "LintResultEmbed": {
                    "type": "object",
                    "required": ["passed", "errors", "warnings"],
                    "properties": {
                        "passed": {"type": "boolean"},
                        "errors": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "QAResultEmbed": {
                    "type": "object",
                    "required": ["passed", "errors", "warnings"],
                    "properties": {
                        "passed": {"type": "boolean"},
                        "errors": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "SpintaxJobResult": {
                    "type": "object",
                    "required": [
                        "spintax_body",
                        "lint",
                        "qa",
                        "tool_calls",
                        "api_calls",
                        "cost_usd",
                    ],
                    "properties": {
                        "spintax_body": {"type": "string"},
                        "lint": {"$ref": "#/components/schemas/LintResultEmbed"},
                        "qa": {"$ref": "#/components/schemas/QAResultEmbed"},
                        "tool_calls": {"type": "integer"},
                        "api_calls": {"type": "integer"},
                        "cost_usd": {"type": "number"},
                        "drift_revisions": {"type": "integer", "default": 0},
                        "drift_unresolved": {
                            "type": "array",
                            "items": {"type": "string"},
                            "default": [],
                        },
                    },
                },
                "JobStatusResponse": {
                    "type": "object",
                    "required": ["job_id", "status", "cost_usd", "elapsed_sec"],
                    "properties": {
                        "job_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "queued",
                                "drafting",
                                "linting",
                                "iterating",
                                "qa",
                                "done",
                                "failed",
                            ],
                        },
                        "progress": {
                            "type": "object",
                            "nullable": True,
                            "additionalProperties": True,
                        },
                        "result": {
                            "$ref": "#/components/schemas/SpintaxJobResult",
                            "nullable": True,
                        },
                        "error": {
                            "type": "string",
                            "nullable": True,
                            "description": ("Machine-readable error key. See x-error-codes."),
                        },
                        "error_detail": {
                            "type": "string",
                            "nullable": True,
                            "description": ("Human-readable provider message."),
                        },
                        "cost_usd": {"type": "number"},
                        "elapsed_sec": {"type": "number"},
                    },
                },
                # ── Batch ───────────────────────────────────────────────
                "BatchRequest": {
                    "type": "object",
                    "required": ["md", "platform"],
                    "properties": {
                        "md": {
                            "type": "string",
                            "minLength": 1,
                            "description": ("Full markdown sequence document."),
                        },
                        "platform": {
                            "type": "string",
                            "enum": ["instantly", "emailbison"],
                        },
                        "model": {"type": "string", "nullable": True},
                        "concurrency": {
                            "type": "integer",
                            "minimum": 1,
                            "maximum": 16,
                            "default": 4,
                        },
                        "dry_run": {
                            "type": "boolean",
                            "default": False,
                            "description": (
                                "If true, parse only and return the "
                                "structure WITHOUT firing any spintax jobs."
                            ),
                        },
                    },
                },
                "BatchSegmentSummary": {
                    "type": "object",
                    "required": [
                        "name",
                        "section",
                        "email_count",
                        "emails_to_spin",
                        "warnings",
                    ],
                    "properties": {
                        "name": {"type": "string"},
                        "section": {"type": "string"},
                        "email_count": {"type": "integer"},
                        "emails_to_spin": {
                            "type": "integer",
                            "description": (
                                "Bodies that will actually call OpenAI (Email 1 only)."
                            ),
                        },
                        "warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "BatchParsedSummary": {
                    "type": "object",
                    "required": [
                        "segments",
                        "total_bodies",
                        "total_bodies_to_spin",
                        "warnings",
                    ],
                    "properties": {
                        "segments": {
                            "type": "array",
                            "items": {"$ref": "#/components/schemas/BatchSegmentSummary"},
                        },
                        "total_bodies": {"type": "integer"},
                        "total_bodies_to_spin": {"type": "integer"},
                        "warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                    },
                },
                "BatchSubmitResponse": {
                    "type": "object",
                    "required": [
                        "batch_id",
                        "parsed",
                        "status",
                        "fired",
                        "total_jobs",
                    ],
                    "properties": {
                        "batch_id": {"type": "string"},
                        "parsed": {"$ref": "#/components/schemas/BatchParsedSummary"},
                        "status": {"type": "string"},
                        "fired": {"type": "boolean"},
                        "total_jobs": {"type": "integer"},
                    },
                },
                "BatchStatusResponse": {
                    "type": "object",
                    "required": [
                        "batch_id",
                        "status",
                        "platform",
                        "model",
                        "completed",
                        "failed",
                        "in_progress",
                        "retrying",
                        "queued",
                        "total",
                        "retries_used",
                        "elapsed_sec",
                        "cost_usd_so_far",
                        "cost_usd_estimated_total",
                        "parsed",
                    ],
                    "properties": {
                        "batch_id": {"type": "string"},
                        "status": {
                            "type": "string",
                            "enum": [
                                "parsed",
                                "running",
                                "done",
                                "failed",
                                "cancelled",
                            ],
                        },
                        "platform": {"type": "string"},
                        "model": {"type": "string"},
                        "completed": {"type": "integer"},
                        "failed": {"type": "integer"},
                        "in_progress": {"type": "integer"},
                        "retrying": {"type": "integer"},
                        "queued": {"type": "integer"},
                        "total": {"type": "integer"},
                        "retries_used": {"type": "integer"},
                        "elapsed_sec": {"type": "number"},
                        "cost_usd_so_far": {"type": "number"},
                        "cost_usd_estimated_total": {"type": "number"},
                        "failure_reason": {"type": "string", "nullable": True},
                        "download_url": {"type": "string", "nullable": True},
                        "parsed": {"$ref": "#/components/schemas/BatchParsedSummary"},
                    },
                },
                # ── Lint ────────────────────────────────────────────────
                "LintRequest": {
                    "type": "object",
                    "required": ["text", "platform"],
                    "properties": {
                        "text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Spintax copy to lint.",
                        },
                        "platform": {
                            "type": "string",
                            "enum": ["instantly", "emailbison"],
                        },
                        "tolerance": {
                            "type": "number",
                            "minimum": 0.0,
                            "maximum": 1.0,
                            "default": 0.05,
                        },
                        "tolerance_floor": {
                            "type": "integer",
                            "minimum": 0,
                            "default": 3,
                        },
                    },
                },
                "LintResponse": {
                    "type": "object",
                    "required": [
                        "errors",
                        "warnings",
                        "passed",
                        "error_count",
                        "warning_count",
                    ],
                    "properties": {
                        "errors": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "passed": {"type": "boolean"},
                        "error_count": {"type": "integer"},
                        "warning_count": {"type": "integer"},
                    },
                },
                # ── QA ──────────────────────────────────────────────────
                "QARequest": {
                    "type": "object",
                    "required": ["output_text", "input_text", "platform"],
                    "properties": {
                        "output_text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Generated spintax copy.",
                        },
                        "input_text": {
                            "type": "string",
                            "minLength": 1,
                            "description": "Original plain copy that was spun.",
                        },
                        "platform": {
                            "type": "string",
                            "enum": ["instantly", "emailbison"],
                        },
                    },
                },
                "QAResponse": {
                    "type": "object",
                    "required": [
                        "passed",
                        "error_count",
                        "warning_count",
                        "errors",
                        "warnings",
                        "block_count",
                        "input_paragraph_count",
                    ],
                    "properties": {
                        "passed": {"type": "boolean"},
                        "error_count": {"type": "integer"},
                        "warning_count": {"type": "integer"},
                        "errors": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "warnings": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "block_count": {"type": "integer"},
                        "input_paragraph_count": {"type": "integer"},
                    },
                },
                # ── Errors ──────────────────────────────────────────────
                "ErrorEnvelope": {
                    "type": "object",
                    "required": ["error", "message"],
                    "properties": {
                        "error": {
                            "type": "string",
                            "description": "Machine-readable error key.",
                        },
                        "message": {
                            "type": "string",
                            "description": "Human-readable error message.",
                        },
                        "details": {
                            "type": "object",
                            "additionalProperties": True,
                            "nullable": True,
                        },
                    },
                },
            },
        },
        "x-agent-guidance": {
            "when_to_use": [
                "Convert plain email copy into Instantly or EmailBison spintax syntax.",
                "Spin a whole markdown sequence file (multiple segments and emails) in one batch and download the result as a zip.",
                "Lint already-written spintax copy for syntax errors and length-balance issues.",
                "QA-check spintax output against the original plain input for fidelity, drift, duplicate variations, and platform-specific markup violations.",
            ],
            "when_not_to_use": [
                "Writing email copy from scratch (this only spins existing copy).",
                "Cross-language translation (the spinner preserves the input language).",
                "General LLM completions (this is a constrained tool-call loop with hard format rules).",
                "Real-time UX where latency matters (single-body jobs take 30-90 seconds).",
            ],
            "polling_pattern": {
                "interval_seconds": 10,
                "terminal_states": ["done", "failed"],
                "ttl_seconds": 3600,
                "notes": [
                    "Single-body jobs typically finish in 30-90 seconds.",
                    "Batches scale linearly with total_bodies / concurrency.",
                    "Do not poll beyond a terminal state - the response will not change.",
                    "Jobs are evicted from memory 1 hour after creation. Pull the result well before then.",
                ],
            },
            "model_selection_advice": {
                "default": "o3",
                "fast_and_cheap": "o3-mini",
                "highest_quality": "gpt-5.5-pro",
                "anthropic_alternative": "claude-opus-4-7",
                "when_drift_persists": (
                    "Switch to o3-pro or gpt-5.5-pro. Weaker models can fail the drift loop."
                ),
                "reasoning_effort_supported_by": [
                    "o1",
                    "o1-mini",
                    "o3",
                    "o3-mini",
                    "o3-pro",
                    "o4-mini",
                    "gpt-5",
                    "gpt-5-mini",
                    "gpt-5.5",
                    "gpt-5.5-pro",
                ],
                "reasoning_effort_ignored_by": [
                    "gpt-4.1",
                    "gpt-4.1-mini",
                    "claude-opus-4-7",
                    "claude-sonnet-4-6",
                ],
            },
            "error_recovery_pattern": {
                "retry_once": [
                    "openai_timeout",
                    "openai_quota",
                    "malformed_response",
                    "internal_error",
                ],
                "do_not_retry_same_model": ["max_tool_calls"],
                "do_not_retry_tell_operator": ["auth_failed", "low_balance"],
                "fix_request_then_retry": ["bad_request", "model_not_found"],
            },
            "defaults_recommendation": {
                "model": "o3",
                "platform": "instantly",
                "reasoning_effort": "medium",
                "concurrency": 4,
            },
        },
        "x-drift-revision": {
            "summary": ("Self-correction loop that catches concept drift after generation."),
            "max_revisions": 3,
            "trigger": (
                "QA reports concept-drift warnings (variations 2-N introduce "
                "nouns or content words not present in V1)."
            ),
            "loop_behavior": [
                "Runner generates the initial spintax draft.",
                "Runner runs the deterministic linter; the model fixes lint errors via tool calls.",
                "Runner runs QA. If drift warnings are zero, exit clean.",
                "If drift warnings exist and revisions remaining, send a revision prompt and regenerate.",
                "Repeat up to MAX_DRIFT_REVISIONS (3). Exit on the first clean QA pass.",
            ],
            "result_field_meaning": {
                "drift_revisions == 0": ("Model was clean on first try. Highest-quality output."),
                "drift_revisions in {1,2,3}": (
                    "Model needed corrections but converged. Output is acceptable."
                ),
                "drift_unresolved is empty AND drift_revisions > 0": (
                    "Drift was caught and fixed. Use the output."
                ),
                "drift_unresolved is non-empty": (
                    "Model could NOT resolve drift in 3 attempts. Output is "
                    "returned anyway. Treat the listed phrases as suspect. "
                    "Re-run on a stronger model, shorten the input, or "
                    "accept the drift if acceptable."
                ),
            },
        },
        "x-error-codes": {
            "openai_timeout": {
                "description": ("Provider request exceeded the per-call timeout."),
                "example_error_detail": "Request timed out after 120s",
                "recovery": (
                    "Retry once. If it persists, switch to a smaller/faster "
                    "model (o3-mini or gpt-4.1-mini)."
                ),
            },
            "openai_quota": {
                "description": "Provider quota or rate limit hit.",
                "example_error_detail": ("Rate limit reached for o3 in organization org_..."),
                "recovery": ("Wait a few seconds and retry. Reduce batch concurrency if frequent."),
            },
            "max_tool_calls": {
                "description": (
                    "Model exhausted the 10-tool-call ceiling without producing valid output."
                ),
                "example_error_detail": ("Reached max tool calls (10) without finishing"),
                "recovery": (
                    "Re-run with a stronger model (o3-pro or gpt-5.5). Do "
                    "NOT retry on the same model."
                ),
            },
            "malformed_response": {
                "description": ("Provider returned something the runner couldn't parse."),
                "example_error_detail": ("Could not extract spintax_body from response"),
                "recovery": ("Retry once. If it persists, simplify the input copy."),
            },
            "auth_failed": {
                "description": "Provider rejected the API key.",
                "example_error_detail": ("Incorrect API key provided: sk-... / invalid x-api-key"),
                "recovery": ("Server config issue. Tell Mihajlo. Do NOT retry."),
            },
            "low_balance": {
                "description": ("Provider account is out of credits or billing failed."),
                "example_error_detail": (
                    "Your credit balance is too low to access the Anthropic API"
                ),
                "recovery": (
                    "Server config issue. Tell Mihajlo. Switch model to an "
                    "OpenAI model in the meantime."
                ),
            },
            "bad_request": {
                "description": (
                    "Provider rejected the request shape (typically a "
                    "model-specific parameter mismatch)."
                ),
                "example_error_detail": (
                    "Unsupported value: 'reasoning_effort' is not supported with this model"
                ),
                "recovery": (
                    "Adjust the request - e.g., omit reasoning_effort for non-reasoning models."
                ),
            },
            "model_not_found": {
                "description": "Provider doesn't recognize the model name.",
                "example_error_detail": (
                    "The model 'gpt-99' does not exist or you do not have access to it"
                ),
                "recovery": ("Check the Models table. The model name is case-sensitive."),
            },
            "internal_error": {
                "description": ("Anything else (uncategorized exception)."),
                "example_error_detail": "KeyError: 'choices'",
                "recovery": (
                    "Retry once. If it persists, capture job_id and elapsed_sec and report it."
                ),
            },
        },
    }


_OPENAPI_SPEC: dict = _build_openapi_spec()


@router.get("/openapi.json", include_in_schema=False)
def openapi_json() -> JSONResponse:
    """Serve the hand-built OpenAPI 3.1 spec.

    Built once at import time (the spec is a static dict) and returned
    via JSONResponse with the documented application/json content type.

    Public: no auth gate.
    """
    return JSONResponse(_OPENAPI_SPEC, media_type="application/json")
