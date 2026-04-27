"""POST /admin/login route.

What this does:
    Validates the admin password and sets a signed session cookie.
    Returns LoginResponse. Cookie carries all session state - no
    server-side session.

What it depends on:
    app.auth.verify_password, app.auth.set_session_cookie
    app.api_models.LoginRequest, LoginResponse

What depends on it:
    app.main mounts this router (public, no auth dependency).

Security notes:
    - 401 (not 403) on wrong password: credentials are wrong, not forbidden.
    - No rate limiting in Phase 2 (team-only tool, shared password).
    - Cookie is HttpOnly + SameSite=lax (set in app.auth.set_session_cookie).
"""

from fastapi import APIRouter, HTTPException, Response

from app import auth
from app.api_models import LoginRequest, LoginResponse

router = APIRouter(tags=["admin"])


@router.post("/admin/login", response_model=LoginResponse)
async def admin_login(
    body: LoginRequest,
    response: Response,
) -> LoginResponse:
    """Verify the admin password and set a signed session cookie.

    Returns 200 + LoginResponse(success=True) on match.
    Raises 401 on mismatch (or empty password).
    Pydantic raises 422 if 'password' field is missing.
    """
    if not auth.verify_password(body.password):
        raise HTTPException(status_code=401, detail="invalid password")
    auth.set_session_cookie(response)
    return LoginResponse(success=True)
