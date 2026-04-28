"""Route package for the Prospeqt Spintax Web API.

What this does:
    Re-exports the APIRouter instances from each route module so that
    app/main.py can mount them with a single import.

What it depends on:
    - app/routes/lint (lint_router)
    - app/routes/qa (qa_router)
    - app/routes/spintax (spintax_router) - Phase 2
    - app/routes/admin (admin_router) - Phase 2
    - app/routes/batch (batch_router) - Phase 4
    - app/routes/docs (docs_router) - public API documentation surfaces

What depends on it:
    - app/main.py uses `from app.routes import (lint_router, qa_router,
      spintax_router, admin_router, batch_router, docs_router)`
"""

from app.routes.admin import router as admin_router
from app.routes.batch import router as batch_router
from app.routes.docs import router as docs_router
from app.routes.lint import router as lint_router
from app.routes.qa import router as qa_router
from app.routes.spintax import router as spintax_router

__all__ = [
    "lint_router",
    "qa_router",
    "spintax_router",
    "admin_router",
    "batch_router",
    "docs_router",
]
