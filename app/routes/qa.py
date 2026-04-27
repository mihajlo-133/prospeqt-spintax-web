"""Route handler for POST /api/qa.

What this does:
    Thin shim: validates the request body via Pydantic, calls app.qa.qa(),
    and shapes the result into a QAResponse. No business logic lives here.

What it depends on:
    - fastapi (APIRouter)
    - app.qa.qa (pure function, no I/O)
    - app.api_models (QARequest, QAResponse)

What depends on it:
    - app.routes.__init__ re-exports this module's router as qa_router
    - app.main.py mounts it under the /api prefix
"""

from fastapi import APIRouter

from app.api_models import QARequest, QAResponse
from app.qa import qa

router = APIRouter(tags=["qa"])


@router.post("/api/qa", response_model=QAResponse, summary="QA-check spintax output")
def qa_endpoint(body: QARequest) -> QAResponse:
    """Run QA checks on spintax output against the original input.

    Checks V1 fidelity, block count, greeting whitelist, duplicate variations,
    smart quotes, and doubled punctuation. `passed` is True only when
    `errors` is empty.

    - **output_text**: The generated spintax copy to check.
    - **input_text**: The original plain email that was spun.
    - **platform**: `"instantly"` or `"emailbison"` - determines spintax syntax.
    """
    result = qa(body.output_text, body.input_text, body.platform)
    return QAResponse(**result)
