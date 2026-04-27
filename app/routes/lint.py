"""Route handler for POST /api/lint.

What this does:
    Thin shim: validates the request body via Pydantic, calls app.lint.lint(),
    and shapes the result into a LintResponse. No business logic lives here.

What it depends on:
    - fastapi (APIRouter, HTTPException)
    - app.lint.lint (pure function, no I/O)
    - app.api_models (LintRequest, LintResponse)

What depends on it:
    - app.routes.__init__ re-exports this module's router as lint_router
    - app.main.py mounts it under the /api prefix
"""

from fastapi import APIRouter

from app.api_models import LintRequest, LintResponse
from app.lint import lint

router = APIRouter(tags=["lint"])


@router.post("/api/lint", response_model=LintResponse, summary="Lint spintax copy")
def lint_endpoint(body: LintRequest) -> LintResponse:
    """Run the deterministic spintax linter on the provided copy.

    Returns a structured result with errors and warnings. `passed` is True
    only when `errors` is empty. Warnings are advisory and do not affect the
    passed flag.

    - **text**: The spintax copy to lint (must contain at least one block).
    - **platform**: `"instantly"` or `"emailbison"` - determines spintax syntax.
    - **tolerance**: Length tolerance fraction (default `0.05` = 5%).
    - **tolerance_floor**: Minimum absolute char tolerance (default `3`).
    """
    errors, warnings = lint(
        body.text,
        body.platform,
        body.tolerance,
        body.tolerance_floor,
    )
    return LintResponse(
        errors=errors,
        warnings=warnings,
        passed=len(errors) == 0,
        error_count=len(errors),
        warning_count=len(warnings),
    )
