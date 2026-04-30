"""FastAPI application entry point.

What this does:
    Instantiates the FastAPI app, mounts all routes, and exposes the
    canonical DEFAULT_MODEL constant for callers that need the default
    OpenAI model name.

    Phase 0: GET /health only.
    Phase 1: POST /api/lint, POST /api/qa (sync, no auth).
    Phase 2: POST /api/spintax, GET /api/status/{job_id}, POST /admin/login.
        ALL /api/* routes (including lint and qa) are gated behind the
        session cookie via Depends(require_auth).
    Phase 3: GET /, GET /login (HTML page routes), Jinja2 templates,
        and StaticFiles mount at /static for main.css and main.js.

What it depends on:
    - fastapi (external)
    - app/config.py for settings (DEFAULT_MODEL is sourced from there)
    - app/routes (lint_router, qa_router, spintax_router, admin_router,
      pages_router)
    - app/dependencies.require_auth (cookie gate for /api/* routes)

What depends on it:
    - Procfile gunicorn entry: `gunicorn app.main:app -k uvicorn.workers.UvicornWorker`
    - tests/test_health.py, tests/test_smoke.py, tests/test_app_config.py
    - tests/test_routes_lint.py, tests/test_routes_qa.py
    - tests/test_routes_spintax.py, tests/test_routes_admin.py (Phase 2)
    - tests/test_auth.py, tests/test_failure_modes.py (Phase 2)
    - tests/test_routes_pages.py (Phase 3)

Rule 3 compliance:
    DEFAULT_MODEL is exported here so route handlers can read it as
    `from app.main import DEFAULT_MODEL`. The model literal appears in
    app/config.py only - never inline in route code.

Auth model (Phase 2 + 3):
    - GET /health is PUBLIC (no auth, regression-guarded by tests)
    - POST /admin/login is PUBLIC (login is the gateway)
    - GET /login is PUBLIC (login page renders the form). If a valid
      cookie is already present the handler 302-redirects to /.
    - GET / is PUBLIC at the routing layer; the handler checks
      app.dependencies.is_authed and 302-redirects unauthenticated
      requests to /login. Render policy lives in the dependency.
    - ALL /api/* routes require a valid session cookie. Phase 1's
      /api/lint and /api/qa are gated retroactively at the mount
      level via include_router(dependencies=[Depends(require_auth)]).
"""

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.dependencies import require_auth
from app.routes import (
    admin_router,
    batch_router,
    docs_router,
    lint_router,
    qa_router,
    spintax_router,
)
from app.routes.pages import router as pages_router
from app.tools.wordhippo_client import close_fetchers

# Re-exported so route handlers and tests can grab the default without
# reaching into pydantic settings every time. Single source of truth lives
# in app/config.py.
DEFAULT_MODEL: str = settings.default_model


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup/shutdown hooks.

    Opens nothing on startup — the WordHippo httpx client is lazy-initialized
    on first tool call. On shutdown, closes the shared client so TCP
    connections drain cleanly instead of being killed by the worker exit.
    """
    yield
    await close_fetchers()


app = FastAPI(
    title="Prospeqt Spintax Web",
    description=(
        "Web service that wraps the Prospeqt spintax tooling. "
        "Paste plain email copy in, get spintax-formatted output back."
    ),
    version="0.3.0",
    # Disable FastAPI's auto-generated Swagger / ReDoc UIs.
    # Our public docs surfaces are GET /docs (HTML page) and
    # GET /openapi.json (hand-built spec) - both served by docs_router.
    # Leaving the auto-Swagger at /docs would collide with our HTML page.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    lifespan=lifespan,
)

# Phase 3: serve CSS / JS / icons from /static. Mounted before the page
# routes so url_for('static', path='main.css') resolves correctly.
app.mount("/static", StaticFiles(directory="static"), name="static")

# Phase 3 router: GET / and GET /login. PUBLIC at the routing layer;
# handlers redirect unauthenticated requests via app.dependencies.is_authed.
app.include_router(pages_router)

# Public documentation surfaces: GET /docs, GET /llms.txt, GET /openapi.json.
# No auth gate. Mounted early so routing is unambiguous.
app.include_router(docs_router)

# Phase 1 routers, gated retroactively in Phase 2.
app.include_router(lint_router, dependencies=[Depends(require_auth)])
app.include_router(qa_router, dependencies=[Depends(require_auth)])

# Phase 2 routers. spintax_router declares require_auth on each route;
# admin_router is intentionally PUBLIC (login is the gateway).
app.include_router(spintax_router)
app.include_router(admin_router)

# Phase 4 router: batch spintax endpoints (BATCH_API_SPEC.md).
# Each route declares require_auth so this matches spintax_router.
app.include_router(batch_router)


@app.get("/health")
def health() -> dict[str, str]:
    """Liveness probe.

    Used by Render's health check and UptimeRobot's 5-minute keepalive
    ping (which prevents cold starts on the free tier).

    Contract:
        - HTTP 200
        - Body: {"status": "ok"} - exactly these keys, no extras
        - Content-Type: application/json
        - PUBLIC: no auth required (Phase 2 regression test guards this)
    """
    return {"status": "ok"}
