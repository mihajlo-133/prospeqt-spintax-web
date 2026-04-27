"""HTML page routes for the Prospeqt Spintax Web tool.

What this does:
    Serves the user-facing HTML pages: GET / (main UI) and GET /login.
    Both routes are public at the FastAPI level - the auth check is
    performed inside the handler via the is_authed dependency, which
    decides between rendering the template and issuing a 302 redirect.

    GET /:
        - If session cookie is valid: render index.html
        - Otherwise: 302 redirect to /login

    GET /login:
        - If session cookie is valid: 302 redirect to /
        - Otherwise: render login.html

What it depends on:
    fastapi.APIRouter, fastapi.Request, fastapi.Depends
    fastapi.responses.RedirectResponse, HTMLResponse
    fastapi.templating.Jinja2Templates (instance owned by app.main)
    app.dependencies.is_authed (auth check, returns bool)

What depends on it:
    app/main.py mounts pages_router via include_router(pages_router).
    tests/test_routes_pages.py exercises both routes.

Phase 3 design constraint:
    Templates are pure Jinja shells. The spintax output never passes
    through Jinja {{ ... }} - it is set via JS textContent / innerHTML
    from the JSON poll response. This avoids the Jinja vs spintax
    {{firstName}} conflict documented in the planning session.
"""

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.dependencies import is_authed

# Single Jinja2Templates instance for the page routes. Mirrors the
# instance app.main.py uses for url_for() resolution. Pointing both
# at the same directory keeps the static URL helper consistent.
templates: Jinja2Templates = Jinja2Templates(directory="templates")

router: APIRouter = APIRouter()


@router.get("/", response_class=HTMLResponse, response_model=None)
def index(
    request: Request,
    authed: bool = Depends(is_authed),
) -> HTMLResponse | RedirectResponse:
    """Serve the main spintax generator UI.

    Auth-gated: redirects to /login if the session cookie is missing
    or invalid. Render policy is delegated to the is_authed dependency
    so this handler stays a thin shim.

    response_model=None disables FastAPI's automatic response-model
    generation; it cannot serialize a Union[HTMLResponse, RedirectResponse]
    return type, but we want the union for mypy + reader clarity.
    """
    if not authed:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request, "index.html")


@router.get("/login", response_class=HTMLResponse, response_model=None)
def login_page(
    request: Request,
    authed: bool = Depends(is_authed),
) -> HTMLResponse | RedirectResponse:
    """Serve the login form.

    If the user already has a valid session cookie, redirect to / to
    avoid re-prompting for a password. Otherwise render the login
    form which posts to /admin/login via fetch().
    """
    if authed:
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse(request, "login.html")


@router.get("/batch", response_class=HTMLResponse, response_model=None)
def batch_page(
    request: Request,
    authed: bool = Depends(is_authed),
) -> HTMLResponse | RedirectResponse:
    """Serve the batch spintax UI.

    Auth-gated like /. The page drives the POST /api/spintax/batch flow
    end-to-end: paste/upload .md -> dry_run parse -> confirm -> spin ->
    poll -> download .zip.
    """
    if not authed:
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse(request, "batch.html")
