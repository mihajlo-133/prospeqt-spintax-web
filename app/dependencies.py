"""FastAPI dependency functions for auth gating.

What this does:
    require_auth: FastAPI Depends() function that raises 401 unless the
    request carries EITHER a valid signed session cookie (web UI) OR a
    matching `Authorization: Bearer <BATCH_API_KEY>` header (headless
    API access from Claude Code).

    is_authed: FastAPI Depends() function that returns True/False
    instead of raising. Used by user-facing page routes (GET /,
    GET /login) which redirect instead of returning 401.

What it depends on:
    app.auth.is_authenticated
    app.config.settings (batch_api_key)

What depends on it:
    app/main.py wires require_auth onto Phase 1 routers (lint, qa).
    app/routes/spintax.py declares it on each route handler.
    app/routes/batch.py declares it on each route handler.
    app/routes/admin.py is unauth (login is the gateway).
    app/routes/pages.py uses is_authed for its 302 redirect logic.

Bearer token model:
    - Single shared key (BATCH_API_KEY env var). Distribute to teammates.
    - Constant-time comparison via hmac.compare_digest to avoid timing leaks.
    - Empty key disables bearer auth (cookie-only mode).
    - Cookies remain the primary path for the web UI; the bearer header
      is only checked when present.
"""

import hmac

from fastapi import HTTPException, Request

from app.config import settings


def _bearer_token_valid(request: Request) -> bool:
    """True if the request has a matching Authorization: Bearer header.

    Returns False (not raises) when:
      - BATCH_API_KEY env var is empty (bearer auth disabled)
      - Header is missing
      - Header doesn't start with 'Bearer '
      - Token doesn't match the configured key
    """
    expected = settings.batch_api_key
    if not expected:
        return False
    auth_header = request.headers.get("Authorization") or ""
    if not auth_header.startswith("Bearer "):
        return False
    presented = auth_header[len("Bearer "):].strip()
    if not presented:
        return False
    # Constant-time comparison to prevent timing attacks.
    return hmac.compare_digest(presented, expected)


def require_auth(request: Request) -> None:
    """FastAPI dependency. Raises 401 unless EITHER bearer token OR session cookie is valid.

    Imported lazily inside the function body to keep import order clean
    (auth.py imports config.py at module load).
    """
    from app.auth import is_authenticated

    if _bearer_token_valid(request):
        return
    if is_authenticated(request):
        return
    raise HTTPException(status_code=401, detail="authentication required")


def is_authed(request: Request) -> bool:
    """FastAPI dependency. Returns True if the request carries a valid
    session cookie, False otherwise. Never raises.

    Used by user-facing page routes (GET /, GET /login) which redirect
    instead of returning 401. Keeps the auth check policy in one place
    so route handlers stay thin shims.
    """
    from app.auth import is_authenticated

    return is_authenticated(request)
