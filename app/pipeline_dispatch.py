"""Pipeline selection helper used by /api/spintax and /api/spintax/batch.

Picks alpha vs beta_v1 based on the per-request override or the
SPINTAX_PIPELINE env var. Routes call `resolve_pipeline()` to get the
runner coroutine they should fire.

Beta (spintax_runner_v2.run) is wired in once task #18 lands. Until
then, requesting pipeline='beta_v1' raises a 400 with a clear message
so the API never silently falls back to alpha.
"""

from __future__ import annotations

from typing import Any, Awaitable, Callable, Literal

from fastapi import HTTPException

from app import spintax_runner
from app.config import settings

# Type alias for any runner that matches the spintax_runner.run signature.
Runner = Callable[..., Awaitable[None]]

PipelineName = Literal["alpha", "beta_v1"]


def resolve_pipeline(override: str | None = None) -> tuple[PipelineName, Runner]:
    """Return the (pipeline_name, runner) tuple for this request.

    Args:
        override: optional per-request override. None = use env default.

    Returns:
        (name, run_callable) - name is 'alpha' or 'beta_v1';
        run_callable has the same signature as spintax_runner.run.

    Raises:
        HTTPException(400) if beta_v1 is requested but not yet wired in,
        or if the override value is unsupported.
    """

    name: PipelineName
    if override is None:
        env_value = settings.spintax_pipeline
        if env_value not in ("alpha", "beta_v1"):
            # config validator should have caught this; defensive belt.
            raise HTTPException(
                status_code=500,
                detail=f"SPINTAX_PIPELINE has invalid value: {env_value!r}",
            )
        name = env_value  # type: ignore[assignment]
    elif override in ("alpha", "beta_v1"):
        name = override  # type: ignore[assignment]
    else:
        raise HTTPException(
            status_code=400,
            detail=f"pipeline must be 'alpha' or 'beta_v1', got {override!r}",
        )

    if name == "alpha":
        return name, spintax_runner.run

    # beta_v1 path. Lazy import so the module does not have to exist
    # for alpha-only deployments (it lands in task #18).
    try:
        from app import spintax_runner_v2  # type: ignore[attr-defined]
    except ImportError as exc:
        raise HTTPException(
            status_code=400,
            detail=(
                "pipeline='beta_v1' is not yet available in this build. "
                "Use pipeline='alpha' or omit the field."
            ),
        ) from exc

    return name, spintax_runner_v2.run


def request_pipeline(
    body_pipeline_field: Any,
) -> tuple[PipelineName, Runner]:
    """Helper for FastAPI route handlers.

    Pulls the override out of a request body (if present) and resolves.
    """

    override = body_pipeline_field if isinstance(body_pipeline_field, str) else None
    return resolve_pipeline(override)
