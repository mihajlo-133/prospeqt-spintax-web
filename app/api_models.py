"""Pydantic request and response models for the API routes.

What this does:
    Defines the request bodies and response shapes for all API routes.
    All validation (field types, allowed values, defaults) lives here.
    Route handlers stay thin shims.

    Phase 1: LintRequest/Response, QARequest/Response.
    Phase 2 additions: SpintaxRequest, SpintaxResponse, JobStatusResponse,
    SpintaxJobResult, LintResultEmbed, QAResultEmbed, LoginRequest,
    LoginResponse, ErrorEnvelope.

What it depends on:
    - pydantic (ships with fastapi[standard])

What depends on it:
    - app/routes/lint.py (LintRequest, LintResponse)
    - app/routes/qa.py (QARequest, QAResponse)
    - app/routes/spintax.py (SpintaxRequest, SpintaxResponse,
      JobStatusResponse, SpintaxJobResult, LintResultEmbed, QAResultEmbed,
      ErrorEnvelope)
    - app/routes/admin.py (LoginRequest, LoginResponse)
"""

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, field_validator

# The two supported platforms. Used as a Literal type for validation.
VALID_PLATFORMS = {"instantly", "emailbison"}


class LintRequest(BaseModel):
    """Request body for POST /api/lint."""

    text: Annotated[str, Field(description="Spintax email copy to lint.")]
    platform: Annotated[
        str,
        Field(description="Target platform: 'instantly' or 'emailbison'."),
    ]
    tolerance: Annotated[
        float,
        Field(
            default=0.05,
            ge=0.0,
            le=1.0,
            description="Length tolerance as fraction (0.05 = 5%). Default 0.05.",
        ),
    ] = 0.05
    tolerance_floor: Annotated[
        int,
        Field(
            default=3,
            ge=0,
            description=(
                "Minimum absolute char tolerance. Protects short blocks. "
                "Effective tolerance = max(base*tolerance, floor). Default 3."
            ),
        ),
    ] = 3

    @field_validator("platform")
    @classmethod
    def platform_must_be_valid(cls, v: str) -> str:
        if v not in VALID_PLATFORMS:
            raise ValueError(f"platform must be one of {sorted(VALID_PLATFORMS)!r}, got {v!r}")
        return v

    @field_validator("text")
    @classmethod
    def text_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be empty")
        return v


class LintResponse(BaseModel):
    """Response shape for POST /api/lint."""

    errors: list[str] = Field(description="Hard errors. Non-empty means FAIL.")
    warnings: list[str] = Field(description="Soft warnings. Non-empty is advisory only.")
    passed: bool = Field(description="True if errors list is empty.")
    error_count: int = Field(description="Number of errors.")
    warning_count: int = Field(description="Number of warnings.")


class QARequest(BaseModel):
    """Request body for POST /api/qa."""

    output_text: Annotated[str, Field(description="Generated spintax copy to QA.")]
    input_text: Annotated[str, Field(description="Original plain email that was spun.")]
    platform: Annotated[
        str,
        Field(description="Target platform: 'instantly' or 'emailbison'."),
    ]

    @field_validator("platform")
    @classmethod
    def platform_must_be_valid(cls, v: str) -> str:
        if v not in VALID_PLATFORMS:
            raise ValueError(f"platform must be one of {sorted(VALID_PLATFORMS)!r}, got {v!r}")
        return v

    @field_validator("output_text", "input_text")
    @classmethod
    def text_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text fields must not be empty")
        return v


class QAResponse(BaseModel):
    """Response shape for POST /api/qa.

    Mirrors the dict returned by app.qa.qa() with explicit field declarations.
    """

    passed: bool = Field(description="True if all QA checks passed.")
    error_count: int = Field(description="Number of QA errors.")
    warning_count: int = Field(description="Number of QA warnings.")
    errors: list[str] = Field(description="QA error messages.")
    warnings: list[str] = Field(description="QA warning messages.")
    block_count: int = Field(description="Number of spintax blocks found in output.")
    input_paragraph_count: int = Field(
        description="Number of spintaxable paragraphs found in input."
    )


# ---------------------------------------------------------------------------
# Phase 2: spintax job request/response models
# ---------------------------------------------------------------------------


class SpintaxRequest(BaseModel):
    """Request body for POST /api/spintax."""

    text: Annotated[str, Field(description="Plain email body to convert to spintax.")]
    platform: Annotated[
        str,
        Field(description="Target platform: 'instantly' or 'emailbison'."),
    ]
    model: str | None = Field(
        default=None,
        description="OpenAI model name. If None, uses DEFAULT_MODEL env var.",
    )
    reasoning_effort: Literal["low", "medium", "high"] = Field(
        default="medium",
        description=("Reasoning effort for o-series models. Ignored for non-reasoning models."),
    )

    @field_validator("platform")
    @classmethod
    def platform_must_be_valid(cls, v: str) -> str:
        if v not in VALID_PLATFORMS:
            raise ValueError(f"platform must be one of {sorted(VALID_PLATFORMS)!r}, got {v!r}")
        return v

    @field_validator("text")
    @classmethod
    def text_must_not_be_empty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be empty")
        return v


class SpintaxResponse(BaseModel):
    """Response body for POST /api/spintax (immediate, before generation completes)."""

    job_id: str = Field(description="UUID of the created job. Poll /api/status/{job_id}.")


class LintResultEmbed(BaseModel):
    """Lint result embedded inside a completed job result."""

    passed: bool
    errors: list[str]
    warnings: list[str]


class QAResultEmbed(BaseModel):
    """QA result embedded inside a completed job result."""

    passed: bool
    errors: list[str]
    warnings: list[str]


class SpintaxJobResult(BaseModel):
    """Shape of the result field in a completed job.

    Only present when status == 'done'.
    """

    spintax_body: str
    lint: LintResultEmbed
    qa: QAResultEmbed
    tool_calls: int
    # Phase 4 split: lint_spintax retries vs the 8 spintax agent tools.
    # tool_calls = lint_calls + agent_tool_calls.
    lint_calls: int = 0
    agent_tool_calls: int = 0
    agent_tool_breakdown: dict[str, int] = {}
    api_calls: int = 0
    cost_usd: float = 0.0
    drift_revisions: int = 0
    drift_unresolved: list[str] = []


class JobStatusResponse(BaseModel):
    """Response body for GET /api/status/{job_id}."""

    job_id: str
    status: str  # JobStatus literal value
    progress: dict[str, Any] | None = None  # reserved for Phase 3 UI
    result: SpintaxJobResult | None = None  # only when status == "done"
    error: str | None = None  # only when status == "failed"
    error_detail: str | None = None  # human-readable provider message when status == "failed"
    cost_usd: float
    elapsed_sec: float


# ---------------------------------------------------------------------------
# Phase 2: admin auth request/response models
# ---------------------------------------------------------------------------


class LoginRequest(BaseModel):
    """Request body for POST /admin/login."""

    password: str = Field(description="Admin password (ADMIN_PASSWORD env var).")


class LoginResponse(BaseModel):
    """Response body for POST /admin/login.

    The session cookie is set via Set-Cookie header on the response.
    """

    success: bool


class ErrorEnvelope(BaseModel):
    """Consistent shape for non-422 error responses (429, 401, 500)."""

    error: str = Field(description="Machine-readable error key.")
    message: str = Field(description="Human-readable error message.")
    details: dict[str, Any] | None = Field(default=None, description="Extra context.")
