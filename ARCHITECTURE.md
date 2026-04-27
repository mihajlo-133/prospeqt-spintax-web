# Prospeqt Spintax Web Tool - Architecture

**Phase 0 Spec** | Author: architect agent | Date: 2026-04-26

---

## 1. Folder Structure

```
prospeqt-spintax-web/
├── app/
│   ├── __init__.py              # Empty - marks app as a package
│   ├── main.py                  # FastAPI app, lifespan, routes (GET /health only in Phase 0)
│   ├── config.py                # Settings via pydantic-settings, env var loading
│   ├── lint.py                  # COPY of spintax_lint.py - deterministic linter (no external imports)
│   ├── qa.py                    # COPY of qa_spintax.py - QA checks (imports app.lint, not source repo)
│   ├── jobs.py                  # In-memory job store: create/update/get/list (swappable interface)
│   ├── spintax_runner.py        # Async wrapper around OpenAI tool-calling loop (Phase 2 impl)
│   ├── spend.py                 # Daily spend tracker (in-memory, resets midnight UTC)
│   ├── auth.py                  # ADMIN_PASSWORD check, session cookie helpers
│   └── skills/                  # Copied skill markdown files (read at import time)
│       ├── SKILL.md
│       ├── _rules-length.md
│       ├── _rules-ai-patterns.md
│       ├── _rules-spam-words.md
│       ├── _format-instantly.md
│       └── _format-emailbison.md
├── static/
│   ├── main.js                  # randomize() from spintax_compare_html.py + poll loop (Phase 4)
│   └── style.css                # Prospeqt design tokens (Phase 3)
├── templates/
│   └── index.html               # UI shell (Phase 3)
├── tests/
│   ├── conftest.py              # TestClient fixture, mock job store, env var overrides
│   ├── test_health.py           # GET /health -> 200 {"status": "ok"} (Phase 0)
│   ├── test_smoke.py            # App starts, router mounted, no import errors (Phase 0)
│   ├── test_lint.py             # Pure lint() function tests - Phase 1
│   ├── test_qa.py               # Pure qa() function tests - Phase 1
│   ├── test_jobs.py             # Job state machine transitions - Phase 2
│   ├── test_spend.py            # Spend cap enforcement, reset - Phase 2
│   ├── test_routes_lint.py      # POST /api/lint route - Phase 1
│   ├── test_routes_qa.py        # POST /api/qa route - Phase 1
│   ├── test_routes_spintax.py   # POST /api/spintax + GET /api/status - Phase 2
│   ├── test_auth.py             # POST /admin/login, cookie, 401 gating - Phase 2
│   └── fixtures/
│       └── openai/
│           ├── o3_pass_first_try.json
│           ├── o3_iterate_once.json
│           ├── o3_iterate_max.json
│           ├── o3_timeout.json
│           ├── o3_quota.json
│           └── o3_malformed.json
├── Procfile                     # gunicorn app.main:app -k uvicorn.workers.UvicornWorker --bind 0.0.0.0:$PORT
├── requirements.txt             # fastapi[standard]>=0.135.0, uvicorn, gunicorn, openai, pytest, respx
├── runtime.txt                  # python-3.11.9
├── .gitignore
└── README.md
```

---

## 2. Module Responsibilities

### `app/main.py`
The FastAPI application entry point. Owns: app instantiation, lifespan context manager (starts background refresh loop in Phase 2), route mounts, and Phase 0's single route `GET /health`. Does NOT own: business logic, database, settings validation. Every route handler is thin - it calls into the business logic layer and returns.

### `app/config.py`
Pydantic-settings Config class. Owns: reading env vars (`ADMIN_PASSWORD`, `OPENAI_API_KEY`, `DAILY_SPEND_CAP_USD`, `DEFAULT_MODEL`, `DEFAULT_PLATFORM`, `PORT`), providing defaults, type validation. Does NOT own: any I/O beyond reading env. All other modules import `get_settings()` to read config - never `os.environ` directly.

### `app/lint.py`
Verbatim copy of `spintax_lint.py` from the source repo, with one change: the `sys.path.insert` removed (not needed in package context). Owns: all linting logic - block extraction, variation splitting, all check functions, the public `lint()` entry point. Does NOT own: any I/O, any OpenAI calls, any HTTP. Pure functions only.

Why copy instead of import? The source file lives in a separate repo that will not be a dependency of this service. Self-contained deploy on Render requires all logic in this repo. Keeping it as a copy means: no `sys.path` hackery, no git submodule, no relative import across repo boundaries.

**Update policy:** When `spintax_lint.py` in the source repo is updated, copy the new version here and run `pytest` to catch regressions.

### `app/qa.py`
Verbatim copy of `qa_spintax.py` from the source repo, with one change: `from spintax_lint import ...` becomes `from app.lint import ...`. Owns: all QA checks - V1 fidelity, block count, greeting whitelist, duplicate detection, smart quotes, doubled punctuation. The public `qa()` entry point returns a dict. Does NOT own: any I/O, any HTTP. Pure functions only.

Same copy rationale as `app/lint.py`.

### `app/jobs.py`
In-memory job store. Owns: the `Job` dataclass (job_id, status, created_at, updated_at, input_text, platform, model, result, error), the four public functions `create()`, `update()`, `get()`, `list()`, job TTL eviction. Does NOT own: the spintax generation logic, HTTP handlers, spend tracking. The job dict is a module-level variable protected by a threading lock.

**Swap contract:** To move to Redis in Phase 2+, replace only this module. The signatures of `create()`, `update()`, `get()`, `list()` must not change. All callers (`spintax_runner.py`, route handlers) import from `app.jobs` - they never touch the storage backend directly.

### `app/spintax_runner.py`
Async wrapper around the OpenAI tool-calling loop (extracted from `spintax_openai_v3.py`). Owns: building the system prompt (calls `build_system_prompt(platform, skills_dir)`), the async `run(job_id, plain_body, platform, model, reasoning_effort, tolerance, tolerance_floor, max_tool_calls)` coroutine that drives the `lint_spintax` tool loop, cost tracking, calling `jobs.update()` on each state transition. Does NOT own: job creation (caller's responsibility), HTTP, spend enforcement (caller checks before invoking).

Phase 0: module skeleton with docstring and TODO only.

### `app/spend.py`
Daily spend tracker. Owns: in-memory accumulator of USD spent today, `check_and_add(cost_usd) -> bool` (returns False if adding would exceed cap), `remaining() -> float`, `reset_at() -> datetime`, midnight UTC reset logic. Does NOT own: cap configuration (reads from `config.py`). Module-level state with a lock.

Phase 0: skeleton only.

### `app/auth.py`
Authentication helpers. Owns: `verify_password(candidate) -> bool` (constant-time compare against `ADMIN_PASSWORD`), `set_session_cookie(response)`, `get_session_from_request(request) -> bool`. Does NOT own: route definitions, session storage (cookie-based, stateless). Phase 0: skeleton only.

---

## 3. Public API per Module

### `app/lint.py` (copied from source)

```python
def lint(
    text: str,
    platform: str,         # "instantly" | "emailbison"
    tolerance: float,
    tolerance_floor: int = 3,
) -> tuple[list[str], list[str]]:
    """Run all checks. Return (errors, warnings) as lists of strings."""

def extract_blocks(
    text: str,
    platform: str,
) -> list[tuple[int, str]]:
    """Return list of (char_offset, inner_text) for each spintax block."""

def _split_variations(
    block_inner: str,
    platform: str,
) -> list[str]:
    """Split a block's inner text on top-level pipes only."""

def is_greeting_block(variations: list[str]) -> bool:
    """True if every variation matches an approved greeting pattern."""
```

### `app/qa.py` (copied from source)

```python
def qa(
    output_text: str,
    input_text: str,
    platform: str,          # "instantly" | "emailbison"
) -> dict:
    """Run all QA checks. Returns dict with keys:
    passed, error_count, warning_count, errors, warnings,
    block_count, input_paragraph_count.
    """

def spintaxable_input_paragraphs(text: str) -> list[str]:
    """Return prose paragraphs from input text (filter UNSPUN)."""
```

### `app/jobs.py`

```python
from dataclasses import dataclass
from datetime import datetime
from typing import Literal

JobStatus = Literal[
    "queued", "drafting", "linting", "iterating", "qa", "done", "failed"
]

@dataclass
class Job:
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    input_text: str
    platform: str           # "instantly" | "emailbison"
    model: str              # e.g. "o3"
    result: str | None      # final spintax body, set when status == "done"
    error: str | None       # error message, set when status == "failed"
    cost_usd: float         # accumulated cost, updated on each API call
    tool_calls: int         # accumulated tool calls, updated on each iteration

def create(
    input_text: str,
    platform: str,
    model: str,
) -> Job:
    """Create a new job with status='queued'. Returns the Job object."""

def update(
    job_id: str,
    status: JobStatus | None = None,
    result: str | None = None,
    error: str | None = None,
    cost_usd_delta: float = 0.0,
    tool_calls_delta: int = 0,
) -> Job:
    """Update fields on an existing job. Raises KeyError if not found.
    Sets updated_at to utcnow(). Deltas are added to existing values."""

def get(job_id: str) -> Job | None:
    """Return Job by ID or None if not found."""

def list(limit: int = 50) -> list[Job]:
    """Return most-recent jobs first, up to limit."""
```

### `app/spend.py`

```python
def check_and_add(cost_usd: float) -> bool:
    """Add cost to today's total. Return True if within cap, False if cap exceeded.
    If False, does NOT add the cost (caller should reject the request)."""

def remaining() -> float:
    """Return remaining USD budget for today."""

def reset_at() -> datetime:
    """Return UTC datetime when today's budget resets (next midnight UTC)."""

def today_total() -> float:
    """Return USD spent so far today."""
```

### `app/auth.py`

```python
def verify_password(candidate: str) -> bool:
    """Constant-time compare candidate against ADMIN_PASSWORD setting."""

def set_session_cookie(response: Response) -> None:
    """Set httponly, samesite=strict session cookie on response."""

def is_authenticated(request: Request) -> bool:
    """Return True if request carries a valid session cookie."""
```

### `app/spintax_runner.py`

```python
async def run(
    job_id: str,
    plain_body: str,
    platform: str,              # "instantly" | "emailbison"
    model: str = "o3",
    reasoning_effort: str = "medium",  # "low" | "medium" | "high"
    tolerance: float = 0.05,
    tolerance_floor: int = 3,
    max_tool_calls: int = 10,
) -> None:
    """Drive the OpenAI tool-calling loop for one job.
    Updates job status via jobs.update() on each state transition.
    Catches all exceptions and sets job to 'failed' with error message.
    Never raises - caller fire-and-forget."""

def build_system_prompt(
    platform: str,
    skills_dir: Path,
) -> str:
    """Assemble system prompt from hard rules + skill markdown files.
    Reads: SKILL.md, _rules-length.md, _rules-ai-patterns.md,
    _rules-spam-words.md, _format-{platform}.md from skills_dir."""
```

### `app/config.py`

```python
from pydantic_settings import BaseSettings

class Settings(BaseSettings):
    admin_password: str = ""          # ADMIN_PASSWORD env var
    openai_api_key: str = ""          # OPENAI_API_KEY env var
    daily_spend_cap_usd: float = 20.0 # DAILY_SPEND_CAP_USD env var
    default_model: str = "o3"         # DEFAULT_MODEL env var
    default_platform: str = "instantly"  # DEFAULT_PLATFORM env var
    port: int = 8000                  # PORT env var (Render sets this)

    class Config:
        env_file = ".env"

def get_settings() -> Settings:
    """Return cached Settings instance (lru_cache)."""
```

---

## 4. Dependency Graph

```
app/main.py
  └─ imports: app/config.py, app/auth.py
  └─ in Phase 1+: imports app/lint.py, app/qa.py, app/jobs.py, app/spend.py, app/spintax_runner.py

app/spintax_runner.py
  └─ imports: app/lint.py, app/jobs.py, app/config.py

app/qa.py
  └─ imports: app/lint.py   ← only internal dependency

app/jobs.py
  └─ imports: (none - stdlib only: dataclasses, datetime, uuid, threading)

app/spend.py
  └─ imports: app/config.py

app/auth.py
  └─ imports: app/config.py

app/lint.py
  └─ imports: (none - stdlib only: argparse, re, sys, pathlib)

app/config.py
  └─ imports: pydantic-settings (external dep only)
```

**No cycles.** Dependency order (leaf to root):
1. `stdlib` (no imports)
2. `app/lint.py` (imports stdlib)
3. `app/jobs.py` (imports stdlib)
4. `app/config.py` (imports pydantic-settings)
5. `app/qa.py` (imports app.lint)
6. `app/spend.py` (imports app.config)
7. `app/auth.py` (imports app.config)
8. `app/spintax_runner.py` (imports app.lint, app.jobs, app.config)
9. `app/main.py` (imports everything)

**Critical constraint:** `app/lint.py` and `app/qa.py` import NOTHING from within `app/` except `app/lint.py` by `app/qa.py`. This keeps them pure and testable in complete isolation.

---

## 5. Phase Boundaries (per module)

| Module | Phase 0 | Phase 1 | Phase 2 | Phase 3+ |
|--------|---------|---------|---------|----------|
| `app/main.py` | `/health` route only; app + lifespan stub | Mount `/api/lint`, `/api/qa` | Mount `/api/spintax`, `/api/status/{id}`, `/admin/login`; start background runner | Mount `/` (UI shell) |
| `app/config.py` | Full impl (needed for app to start) | No change | Add `openai_api_key` if not done | No change |
| `app/lint.py` | Full copy (from source) - zero TODO | No change | No change | No change |
| `app/qa.py` | Full copy (from source) - zero TODO | No change | No change | No change |
| `app/jobs.py` | Skeleton - docstring + signatures + TODO | No change | Full impl | Add TTL eviction |
| `app/spend.py` | Skeleton | No change | Full impl | No change |
| `app/auth.py` | Skeleton | No change | Full impl | No change |
| `app/spintax_runner.py` | Skeleton | No change | Full impl | No change |
| `app/skills/` | Copy all 6 .md files from source | No change | No change | No change |
| `static/` | Empty | No change | No change | style.css + main.js |
| `templates/` | Empty | No change | No change | index.html |

**Phase 0 is complete when:**
1. `app/lint.py` and `app/qa.py` are full copies (not skeletons) - they have NO TODOs.
2. All other `app/*.py` modules have skeletons (docstring + signatures + `raise NotImplementedError` or `pass` bodies).
3. `GET /health` returns `{"status": "ok"}` with HTTP 200.
4. `uvicorn app.main:app --reload` starts without errors.
5. `pytest tests/test_health.py tests/test_smoke.py` passes with coverage >85%.

---

## 6. Decision Rationale

### Why copy `lint.py` and `qa.py` instead of importing?

The source tools live at `/Users/mihajlo/Desktop/claude-code/tools/prospeqt-automation/scripts/`. This path:
- Will not exist on Render (different machine)
- Is inside a different git repo (the monorepo)
- Cannot be a pip-installable package without publishing or submodule overhead

The copy approach means `pip install -r requirements.txt` + `git clone` is all Render needs. No `sys.path` hackery, no relative imports across repos, no deployment complexity.

**Drift risk mitigation:** The source files are pure Python stdlib, well-tested (34 tests passing). Changes are infrequent and deliberate. When a change is needed, the update policy is: (1) copy new version to `app/lint.py` or `app/qa.py`, (2) run `pytest`, (3) fix any regressions before merge.

### Why `jobs.py` as a single interface?

Phase 0 uses an in-memory dict. Phase 2+ might need Redis persistence (e.g., if multiple Gunicorn workers are used, the in-memory dict is not shared across workers). By hiding the storage behind four functions - `create()`, `update()`, `get()`, `list()` - swapping to Redis means changing ONLY `jobs.py`. Every other module stays identical.

This is not over-engineering for MVP: the interface is trivially simple and the payoff (no shared-state bugs if/when we add workers) is real.

### Why `spend.py` as a separate module?

The spend cap is a cross-cutting concern. It must be checked in the route handler BEFORE kicking off a generation job, and updated by `spintax_runner.py` DURING generation (as cost accumulates). A separate module with a clear interface avoids the spend logic bleeding into both the route handler and the runner.

### Why model and platform as parameters throughout?

`spintax_openai_v3.py` uses `DEFAULT_MODEL = "o3"` as a constant, but the session plan locks in the rule: "model and platform are parameters everywhere, never hard-coded as strings." This means:
- `config.py` has `default_model = "o3"` - one place to change the default
- Route handler reads `model = request_body.model or settings.default_model`
- `spintax_runner.run(model=model, ...)` passes it through
- The OpenAI call uses `model=model` not `model="o3"`

Swapping to `o4-mini` is a single env var change: `DEFAULT_MODEL=o4-mini`.

### Why polling, not sync?

o3 generation runs 100-170 seconds. Render's load balancer cuts requests at ~100s on most plans (confirmed in session breadcrumb from March 28). A sync endpoint would fail intermittently on real inputs. Polling (`POST /api/spintax` → `job_id`, `GET /api/status/{job_id}`) is reliable regardless of generation time. SSE streaming is better UX but deferred to v2 (Phase 6+).

### Why `app/skills/` inside the web app?

`spintax_openai_v3.py` reads skill files via `SKILL_DIR = REPO_ROOT / "skills" / "spintax"`. In the web service, these files must be present at deploy time. Copying them into `app/skills/` makes them a tracked part of the repo - no relative path magic, no missing files on Render.

---

## 7. Test Plan Handoff (for tester agent)

### Phase 0 tests to write (failing first, then builder implements)

**`tests/conftest.py`** - must provide:
```python
import pytest
from fastapi.testclient import TestClient
from app.main import app

@pytest.fixture
def client():
    with TestClient(app) as c:
        yield c
```

Also set env vars in conftest so tests don't need a real `.env`:
```python
import os
os.environ.setdefault("ADMIN_PASSWORD", "test-password")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-000")
```

**`tests/test_health.py`** - tests to write:
1. `test_health_returns_200` - `GET /health` returns HTTP 200
2. `test_health_returns_ok_json` - response body is `{"status": "ok"}`
3. `test_health_content_type` - Content-Type is `application/json`

These tests must FAIL before `app/main.py` exists. They must PASS after builder creates the `/health` route.

**`tests/test_smoke.py`** - tests to write:
1. `test_app_has_openapi` - `GET /openapi.json` returns 200 (FastAPI auto-generates this)
2. `test_app_has_docs` - `GET /docs` returns 200 (FastAPI auto-generates Swagger UI)
3. `test_import_lint_module` - `from app.lint import lint, extract_blocks` succeeds without error
4. `test_import_qa_module` - `from app.qa import qa` succeeds without error
5. `test_import_jobs_module` - `from app.jobs import create, update, get, list` succeeds (skeletons are importable)
6. `test_import_spintax_runner_module` - `from app.spintax_runner import run` succeeds

**Coverage gate:** Run `pytest --cov=app --cov-fail-under=85`. Phase 0 coverage is high because `app/lint.py` and `app/qa.py` are full copies with 100% of functions importable, and `app/main.py` has only one route. The skeleton modules contribute non-zero coverage from imports.

### Fixtures to set up in Phase 0 (for Phase 1-2 use)

Create placeholder files now so Phase 1 tester doesn't need to re-structure:

`tests/fixtures/openai/` directory - create empty stubs:
- `o3_pass_first_try.json` - stub `{}`
- `o3_iterate_once.json` - stub `{}`
- `o3_iterate_max.json` - stub `{}`
- `o3_timeout.json` - stub `{}`
- `o3_quota.json` - stub `{}`
- `o3_malformed.json` - stub `{}`

These get populated with real recorded responses in Phase 2 when `respx` mocking is implemented.

### State machine tests (Phase 2 scope, document now)

Every job state transition listed in the session plan needs a dedicated test:
- `queued → drafting` - test: create job, call runner, first update sets status=drafting
- `drafting → linting` - test: after draft emitted, status transitions to linting
- `linting → qa` - test: lint passes, transitions to qa
- `linting → iterating` - test: lint fails, transitions to iterating
- `iterating → linting` - test: retry loop, transitions back to linting
- `iterating → failed` - test: max_tool_calls hit, transitions to failed
- `qa → done` - test: QA passes, transitions to done with result set
- `qa → failed` - test: QA fails, transitions to failed with error set
- `* → failed` on API error - test: OpenAI raises, job transitions to failed

All these use `respx` to mock OpenAI HTTP calls. No real API calls in tests ever.

---

## 8. Key Gotchas (inherited from session plan)

1. **Jinja + spintax conflict:** Jinja2 evaluates `{{firstName}}` in templates. Spintax output NEVER goes through Jinja. Route handlers return `JSONResponse` directly for API endpoints. UI reads output via JSON polling; JS sets `.textContent` from the JSON, never innerHTML. Template engine is only for static shell pages that contain no user data.

2. **Render bind address:** `main()` must bind to `0.0.0.0` (not `127.0.0.1`) and read port from `$PORT` env var. Use `uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", 8000)))`.

3. **Render deploy cache:** Never rely on the Deploy Hook URL for cache-busting. Always use `clearCache: "clear"` via Render REST API. This is a Phase 5 concern but document now so it's not forgotten.

4. **Single worker vs multi-worker:** Phase 0-4 uses a single Gunicorn worker (in-memory jobs dict is safe). When/if we add workers (Phase 5+), we need Redis for the jobs dict - the `jobs.py` interface swap covers this.

5. **`openai` package not in stdlib:** `requirements.txt` must include `openai>=1.0`. `app/lint.py` and `app/qa.py` must NOT import openai - they are pure stdlib.

6. **Skill files are read at startup:** `build_system_prompt()` reads from `app/skills/`. Missing files = startup crash. Include `app/skills/` in the git repo. Do NOT `.gitignore` them.

---

## 9. Phase 1 API Contract

**Author:** architect agent | **Date:** 2026-04-26 | **Phase:** 1

Phase 1 adds two sync endpoints - `POST /api/lint` and `POST /api/qa` - that wrap the pure
functions already present in `app/lint.py` and `app/qa.py`. Both routes are open (no auth) on
purpose; auth gating lands in Phase 2 alongside the spend cap and the async `/api/spintax`.

---

### 9.1 New files this phase

```
app/
  api_models.py        # Pydantic v2 request + response models for all API endpoints
  routes/
    __init__.py        # Empty - marks routes as a package
    lint.py            # POST /api/lint route (thin shim)
    qa.py              # POST /api/qa route (thin shim)

tests/
  test_routes_lint.py  # Route-level integration tests for /api/lint
  test_routes_qa.py    # Route-level integration tests for /api/qa
  test_lint.py         # Pure function tests ported from upstream (18 tests)
  test_qa.py           # Pure function tests ported from upstream (16 tests)
```

`app/main.py` mounts both routers (one import + one `app.include_router()` call per route).
`pyproject.toml` removes `app/lint.py` and `app/qa.py` from the coverage omit list.

---

### 9.2 POST /api/lint

#### Request body - `LintRequest`

```python
class LintRequest(BaseModel):
    text: str
    platform: Literal["instantly", "emailbison"] = "instantly"
    tolerance: float = Field(default=0.05, ge=0.0, le=1.0)
    tolerance_floor: int = Field(default=3, ge=0, le=50)
```

| Field | Type | Default | Validation |
|---|---|---|---|
| `text` | `str` | required | non-empty (validator raises 422 if blank/whitespace-only) |
| `platform` | `"instantly" \| "emailbison"` | `"instantly"` | literal enum, 422 on unknown value |
| `tolerance` | `float` | `0.05` | 0.0-1.0 inclusive |
| `tolerance_floor` | `int` | `3` | 0-50 inclusive |

**Blank-text validator** (add to `LintRequest`):

```python
@field_validator("text")
@classmethod
def text_not_blank(cls, v: str) -> str:
    if not v.strip():
        raise ValueError("text must not be blank")
    return v
```

#### Response success - HTTP 200 - `LintResponse`

```python
class LintResponse(BaseModel):
    passed: bool
    errors: list[str]
    warnings: list[str]
    meta: LintMeta

class LintMeta(BaseModel):
    error_count: int
    warning_count: int
    platform: str
    tolerance: float
    tolerance_floor: int
```

Example 200 body (linter finds issues):

```json
{
  "passed": false,
  "errors": ["block 1 (line 1): variation 3 length 12 vs base 30 (diff 18 chars = 60.0%, limit 5% or 3 chars floor - effective 3 chars)"],
  "warnings": ["block 2 (line 4): variation 1 contains spam trigger: 'free trial'"],
  "meta": {
    "error_count": 1,
    "warning_count": 1,
    "platform": "instantly",
    "tolerance": 0.05,
    "tolerance_floor": 3
  }
}
```

Example 200 body (linter passes):

```json
{
  "passed": true,
  "errors": [],
  "warnings": [],
  "meta": {
    "error_count": 0,
    "warning_count": 0,
    "platform": "instantly",
    "tolerance": 0.05,
    "tolerance_floor": 3
  }
}
```

**Why `passed` is always present:** The UI and CI scripts need a single boolean to branch on.
`errors == []` is equivalent but less readable in client code. Explicit is better.

**Why meta echoes the request params:** The caller may poll or cache results. Echoing the
effective params avoids ambiguity when defaults are applied server-side.

#### Response errors

| Condition | HTTP | Body |
|---|---|---|
| `text` blank or whitespace-only | 422 | pydantic `ValidationError` JSON (FastAPI default) |
| `platform` not in `["instantly", "emailbison"]` | 422 | pydantic `ValidationError` JSON |
| `tolerance` out of `[0, 1]` | 422 | pydantic `ValidationError` JSON |
| Body is not valid JSON | 422 | FastAPI default JSON parse error |
| `lint()` raises unexpected exception | 500 | `{"detail": "internal error"}` |

FastAPI's 422 envelope (standard, do not override):

```json
{
  "detail": [
    {
      "type": "value_error",
      "loc": ["body", "text"],
      "msg": "Value error, text must not be blank",
      "input": "   "
    }
  ]
}
```

500 envelope (custom, use `HTTPException`):

```json
{
  "detail": "internal error"
}
```

Do NOT expose raw Python tracebacks in 500 responses. Log the exception server-side
(`logging.exception("lint route error")`), return the generic message to the caller.

#### Status code policy

- **200** - lint ran (regardless of whether it passed or failed). Lint errors are domain-level
  results, not HTTP errors. A failed lint is still a successful API call.
- **422** - caller sent invalid input. FastAPI raises this automatically for pydantic errors.
- **500** - unexpected exception inside `lint()`. Should never happen in practice; `lint()` is
  pure Python with no I/O. Add a bare `except Exception` in the route handler, log, and
  raise `HTTPException(status_code=500, detail="internal error")`.
- **400** - NOT used for this route. 422 covers all client input errors.

#### Curl example

```bash
curl -s -X POST http://localhost:8000/api/lint \
  -H "Content-Type: application/json" \
  -d '{
    "text": "{{RANDOM | Hello there friend. | Hello there buddy. | Hello there mate. | Hello there pal. | Hello there dear. }}",
    "platform": "instantly"
  }' | python3 -m json.tool
```

Expected response:

```json
{
  "passed": true,
  "errors": [],
  "warnings": [],
  "meta": {
    "error_count": 0,
    "warning_count": 0,
    "platform": "instantly",
    "tolerance": 0.05,
    "tolerance_floor": 3
  }
}
```

---

### 9.3 POST /api/qa

#### Request body - `QARequest`

```python
class QARequest(BaseModel):
    output_text: str
    input_text: str
    platform: Literal["instantly", "emailbison"] = "instantly"
```

| Field | Type | Default | Validation |
|---|---|---|---|
| `output_text` | `str` | required | non-empty (validator raises 422 if blank/whitespace-only) |
| `input_text` | `str` | required | non-empty (validator raises 422 if blank/whitespace-only) |
| `platform` | `"instantly" \| "emailbison"` | `"instantly"` | literal enum, 422 on unknown value |

**Blank validators** on both text fields - same pattern as `LintRequest.text_not_blank`.

**Why `output_text` / `input_text` naming (not `text`):** `qa()` takes two text arguments with
asymmetric semantics. Using distinct names in the request body prevents caller confusion about
which string is the spintax output and which is the original plain email. The names mirror the
function signature exactly.

#### Response success - HTTP 200 - `QAResponse`

The `qa()` return dict maps cleanly to the response. No transformation needed except adding
`meta` for echo-back of request params.

```python
class QAResponse(BaseModel):
    passed: bool
    errors: list[str]
    warnings: list[str]
    error_count: int
    warning_count: int
    block_count: int
    input_paragraph_count: int
    meta: QAMeta

class QAMeta(BaseModel):
    platform: str
```

**Why are counts top-level AND not in meta?** They come directly from `qa()` and are
meaningful domain data, not request echo. A UI badge showing "3 errors, 2 warnings" reads
`response.error_count` directly without unpacking `meta`. The `platform` echo goes in `meta`
because it is request context, not QA domain data.

Example 200 body (QA fails):

```json
{
  "passed": false,
  "errors": ["V1 fidelity: block 1 variation 1 does not match input paragraph 1"],
  "warnings": ["block 1 variation 2: smart quote in ‘It’s fine’"],
  "error_count": 1,
  "warning_count": 1,
  "block_count": 1,
  "input_paragraph_count": 1,
  "meta": {
    "platform": "instantly"
  }
}
```

Example 200 body (QA passes):

```json
{
  "passed": true,
  "errors": [],
  "warnings": [],
  "error_count": 0,
  "warning_count": 0,
  "block_count": 2,
  "input_paragraph_count": 2,
  "meta": {
    "platform": "instantly"
  }
}
```

#### Response errors

Same policy as `/api/lint`:

| Condition | HTTP | Body |
|---|---|---|
| Either text field blank | 422 | pydantic ValidationError JSON |
| `platform` unknown value | 422 | pydantic ValidationError JSON |
| Body not valid JSON | 422 | FastAPI default |
| `qa()` raises unexpected exception | 500 | `{"detail": "internal error"}` |

#### Status code policy

- **200** - QA ran (regardless of pass/fail). Same reasoning as `/api/lint`.
- **422** - invalid input.
- **500** - unexpected exception.
- **400** - NOT used.

#### Curl example

```bash
curl -s -X POST http://localhost:8000/api/qa \
  -H "Content-Type: application/json" \
  -d '{
    "output_text": "{{RANDOM | Just one prose paragraph here. | Just one clear paragraph here. | Just one brief paragraph here.. | Just one solid paragraph here. | Just one simple paragraph here. }}",
    "input_text": "Just one prose paragraph here.\n",
    "platform": "instantly"
  }' | python3 -m json.tool
```

Expected response:

```json
{
  "passed": true,
  "errors": [],
  "warnings": [],
  "error_count": 0,
  "warning_count": 0,
  "block_count": 1,
  "input_paragraph_count": 1,
  "meta": {
    "platform": "instantly"
  }
}
```

---

### 9.4 Pydantic models - `app/api_models.py`

Models live in their own module, not in `main.py` or in the route files. Rationale in section 9.7.

Top docstring required:

```
"""Pydantic v2 request and response models for all API endpoints.

What this does:
    Defines the wire contract for POST /api/lint, POST /api/qa, and (Phase 2)
    POST /api/spintax. All pydantic models used by route handlers live here.

What it depends on:
    pydantic v2 (ships with fastapi[standard]).

What depends on it:
    app/routes/lint.py - imports LintRequest, LintResponse
    app/routes/qa.py   - imports QARequest, QAResponse
    Phase 2 app/routes/spintax.py will import SpintaxRequest, SpintaxResponse
"""
```

Full model set for Phase 1:

```python
from __future__ import annotations
from typing import Any, Literal
from pydantic import BaseModel, Field, field_validator


class LintMeta(BaseModel):
    error_count: int
    warning_count: int
    platform: str
    tolerance: float
    tolerance_floor: int


class LintRequest(BaseModel):
    text: str
    platform: Literal["instantly", "emailbison"] = "instantly"
    tolerance: float = Field(default=0.05, ge=0.0, le=1.0)
    tolerance_floor: int = Field(default=3, ge=0, le=50)

    @field_validator("text")
    @classmethod
    def text_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("text must not be blank")
        return v


class LintResponse(BaseModel):
    passed: bool
    errors: list[str]
    warnings: list[str]
    meta: LintMeta


class QAMeta(BaseModel):
    platform: str


class QARequest(BaseModel):
    output_text: str
    input_text: str
    platform: Literal["instantly", "emailbison"] = "instantly"

    @field_validator("output_text", "input_text")
    @classmethod
    def field_not_blank(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("field must not be blank")
        return v


class QAResponse(BaseModel):
    passed: bool
    errors: list[str]
    warnings: list[str]
    error_count: int
    warning_count: int
    block_count: int
    input_paragraph_count: int
    meta: QAMeta
```

**Phase 2 note:** When `/api/spintax` lands, add `SpintaxRequest` and `SpintaxResponse` to this
same file. The route file `app/routes/spintax.py` imports from here. Do NOT put models inline
in route files.

---

### 9.5 Route file location - `app/routes/` package

Routes live in `app/routes/`, not appended to `app/main.py`.

**Decision:** Phase 1 brings the total route count to 3 (`/health`, `/api/lint`, `/api/qa`).
Phase 2 adds 3 more (`/api/spintax`, `/api/status/{job_id}`, `/admin/login`). Phase 3 adds
`GET /`. We will have 7+ routes before the UI is complete. Putting them all in `main.py`
violates Rule 3 (navigable, each concern in its own module).

The routes package pattern also makes Phase 2 additive: the builder drops a new file
`app/routes/spintax.py` and adds one line to `main.py` (`app.include_router(spintax_router)`).
No merge conflicts with lint.py or qa.py routes.

**`app/routes/__init__.py`** - empty, just marks the package.

**`app/routes/lint.py`** - top docstring, one router, one handler:

```
"""POST /api/lint route.

What this does:
    Thin shim around app.lint.lint(). Validates the request via LintRequest,
    calls lint(), and returns LintResponse. No business logic here.

What it depends on:
    app.lint.lint - pure function, no I/O
    app.api_models - LintRequest, LintResponse, LintMeta

What depends on it:
    app.main mounts this router at /api/lint.
"""
```

```python
import logging
from fastapi import APIRouter, HTTPException
from app.lint import lint
from app.api_models import LintRequest, LintResponse, LintMeta

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/lint", response_model=LintResponse)
def lint_route(body: LintRequest) -> LintResponse:
    try:
        errors, warnings = lint(
            body.text,
            body.platform,
            body.tolerance,
            body.tolerance_floor,
        )
    except Exception:
        logger.exception("lint route unexpected error")
        raise HTTPException(status_code=500, detail="internal error")

    return LintResponse(
        passed=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        meta=LintMeta(
            error_count=len(errors),
            warning_count=len(warnings),
            platform=body.platform,
            tolerance=body.tolerance,
            tolerance_floor=body.tolerance_floor,
        ),
    )
```

**`app/routes/qa.py`** - same pattern:

```
"""POST /api/qa route.

What this does:
    Thin shim around app.qa.qa(). Validates the request via QARequest,
    calls qa(), and returns QAResponse. No business logic here.

What it depends on:
    app.qa.qa - pure function, no I/O
    app.api_models - QARequest, QAResponse, QAMeta

What depends on it:
    app.main mounts this router at /api/qa.
"""
```

```python
import logging
from fastapi import APIRouter, HTTPException
from app.qa import qa
from app.api_models import QARequest, QAResponse, QAMeta

logger = logging.getLogger(__name__)
router = APIRouter()


@router.post("/api/qa", response_model=QAResponse)
def qa_route(body: QARequest) -> QAResponse:
    try:
        result = qa(body.output_text, body.input_text, body.platform)
    except Exception:
        logger.exception("qa route unexpected error")
        raise HTTPException(status_code=500, detail="internal error")

    return QAResponse(
        passed=result["passed"],
        errors=result["errors"],
        warnings=result["warnings"],
        error_count=result["error_count"],
        warning_count=result["warning_count"],
        block_count=result["block_count"],
        input_paragraph_count=result["input_paragraph_count"],
        meta=QAMeta(platform=body.platform),
    )
```

**`app/main.py` mount additions** (two lines added after the health route):

```python
from app.routes.lint import router as lint_router
from app.routes.qa import router as qa_router

app.include_router(lint_router)
app.include_router(qa_router)
```

---

### 9.6 Test plan handoff (for tester)

#### 9.6.1 Route tests - `tests/test_routes_lint.py`

Tests to write (all use the existing `client` fixture from `conftest.py`):

**Happy path:**
1. `test_lint_route_returns_200` - POST valid text, assert HTTP 200
2. `test_lint_route_pass_body` - valid 5-variation block, assert `passed=True`, `errors=[]`
3. `test_lint_route_fail_body` - text with em-dash, assert `passed=False`, `errors` non-empty
4. `test_lint_route_warns_body` - text with spam trigger, assert `warnings` non-empty
5. `test_lint_route_meta_echoes_defaults` - omit tolerance/floor, assert `meta.tolerance=0.05`, `meta.tolerance_floor=3`
6. `test_lint_route_meta_echoes_custom` - send `tolerance=0.10`, `tolerance_floor=5`, assert echoed in meta
7. `test_lint_route_emailbison_platform` - valid emailbison text, assert `meta.platform="emailbison"`

**Error / validation:**
8. `test_lint_route_blank_text_returns_422` - `{"text": "   ", "platform": "instantly"}` → 422
9. `test_lint_route_empty_text_returns_422` - `{"text": "", "platform": "instantly"}` → 422
10. `test_lint_route_bad_platform_returns_422` - `{"text": "x", "platform": "twitter"}` → 422
11. `test_lint_route_tolerance_out_of_range_returns_422` - `tolerance=1.5` → 422
12. `test_lint_route_missing_text_returns_422` - body without `text` field → 422
13. `test_lint_route_content_type_is_json` - response `Content-Type: application/json`

#### 9.6.2 Route tests - `tests/test_routes_qa.py`

1. `test_qa_route_returns_200` - POST valid output + input, HTTP 200
2. `test_qa_route_pass_body` - correct output, assert `passed=True`
3. `test_qa_route_fail_body` - V1 fidelity mismatch, assert `passed=False`, `errors` non-empty
4. `test_qa_route_response_has_all_keys` - assert all 7 payload keys present plus `meta`
5. `test_qa_route_counts_match_list_lengths` - `error_count == len(errors)`, same for warnings
6. `test_qa_route_block_count_correct` - 1-paragraph input, assert `block_count=1`, `input_paragraph_count=1`
7. `test_qa_route_meta_echoes_platform` - send `platform="emailbison"`, assert in `meta`
8. `test_qa_route_blank_output_returns_422` - `output_text="  "` → 422
9. `test_qa_route_blank_input_returns_422` - `input_text="  "` → 422
10. `test_qa_route_bad_platform_returns_422` - `platform="twitter"` → 422
11. `test_qa_route_missing_field_returns_422` - missing `input_text` → 422

#### 9.6.3 Pure function tests - porting upstream

**`tests/test_lint.py`** - port from `tools/prospeqt-automation/tests/test_spintax_lint.py`

The upstream file has 18 tests. The port requires two changes only:

1. Remove the `sys.path.insert` hack and the `ROOT` path scaffolding (first 5 lines).
2. Change `from spintax_lint import ...` to `from app.lint import ...`.

All 18 test functions are otherwise identical. No test logic changes.

Import line change:
```python
# REMOVE:
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
from spintax_lint import (lint, extract_blocks, _split_variations, ...)

# REPLACE WITH:
from app.lint import (lint, extract_blocks, _split_variations, ...)
```

Also remove `from pathlib import Path` and `import sys` since they are no longer needed after
removing the path hack.

**`tests/test_qa.py`** - port from `tools/prospeqt-automation/tests/test_qa_spintax.py`

The upstream file has 16 tests. Same two changes:

1. Remove `sys.path.insert` scaffolding.
2. Change `from qa_spintax import ...` to `from app.qa import ...`.

Also note: `split_input_paragraphs` is not in the module docstring's "Public API" list but IS
used in the upstream tests. It is a public function (no underscore prefix). Import it from
`app.qa` directly. No change to test logic.

#### 9.6.4 Coverage target

After porting the 34 upstream tests and removing the omit list:
- `app/lint.py` will hit >95% line coverage (18 tests cover all branches)
- `app/qa.py` will hit >90% line coverage (16 tests cover all QA checks)
- Full app package target: >85% (the failing gate from Phase 0 forced by `--cov-fail-under=85`)

Remove from `pyproject.toml` `[tool.coverage.run]` omit:
```toml
# REMOVE these two lines:
    "app/lint.py",
    "app/qa.py",
```

Expected test count after Phase 1: 23 (Phase 0) + 18 (lint) + 16 (qa) + 13 (routes_lint) +
11 (routes_qa) = **81 tests total**.

---

### 9.7 Type hint plan (for builder)

#### 9.7.1 `app/lint.py` public API - add type hints

The verbatim-copy stance is maintained but type hints are NOT part of the lint logic - they
are metadata that the copy can carry without diverging semantically from upstream. If upstream
later adds its own type hints, a diff will show them cleanly.

Exact signatures to add:

```python
def lint(
    text: str,
    platform: str,
    tolerance: float,
    tolerance_floor: int = DEFAULT_TOLERANCE_FLOOR,
) -> tuple[list[str], list[str]]:
    """Run all checks. Return (errors, warnings) as lists of strings."""

def extract_blocks(
    text: str,
    platform: str,
) -> list[tuple[int, str]]:
    """Return list of (char_offset, inner_text) for each spintax block."""

def _split_variations(
    block_inner: str,
    platform: str,
) -> list[str]:
    """Split a block's inner text on top-level pipes only."""

def is_greeting_block(variations: list[str]) -> bool:
    """True if every variation matches an approved greeting pattern."""
```

Do NOT add type hints to private helpers (`_has_top_level_pipe`,
`_extract_instantly_blocks`, `_extract_emailbison_blocks`, `check_length`, `check_em_dashes`,
etc.). Only the four public functions listed here.

After adding hints, update the `Source:` block in the module docstring:
```
Public API type hints added in Phase 1 (not present in upstream source).
All other code is verbatim.
```

#### 9.7.2 `app/qa.py:237` return type

**Decision: use `dict[str, Any]`**, not a TypedDict.

Reasoning:
- `qa()` returns a flat dict with 7 well-known keys (all present, no optional keys). A TypedDict
  would be correct and is the "purist" choice.
- However, `qa()` is a copied function. A TypedDict would require adding `from typing import
  TypedDict` and a new class definition - more divergence from upstream than a simple
  `dict[str, Any]` annotation.
- The `QAResponse` pydantic model in `api_models.py` already provides the full type contract
  at the API boundary. The `dict[str, Any]` annotation on `qa()` is honest (it IS a plain
  dict) and the pydantic model is where type-safety is enforced for callers.
- If `qa()` is ever extracted into a proper package, upgrade the return type to TypedDict then.

Change to make at line 237:

```python
# BEFORE:
def qa(output_text: str, input_text: str, platform: str) -> dict:

# AFTER:
def qa(output_text: str, input_text: str, platform: str) -> dict[str, Any]:
```

Add `from typing import Any` to the imports at the top of `app/qa.py`.

Update the module docstring `Public API:` line:
```
Public API type hint tightened in Phase 1: qa() -> dict[str, Any].
```

---

### 9.8 Decision rationale

#### Why pydantic models in `app/api_models.py`, not inline in main.py or route files?

Three concrete reasons:

1. **Phase 2 reuse.** `/api/spintax` (Phase 2) needs its own `SpintaxRequest` /
   `SpintaxResponse`. Phase 3's UI JavaScript calls these endpoints and reads the response
   shape. Having all models in one file means "where is the contract?" has a single answer:
   `app/api_models.py`. A developer onboarding in Phase 4 reads one file to understand all
   API shapes.

2. **Testing independence.** The tester can write model-level unit tests (field validators,
   defaults, edge cases) without importing a route or spinning up a TestClient. If models are
   inline in `main.py`, you cannot import just the model class without importing all of
   `main.py`'s dependencies.

3. **No circular imports.** `app/routes/lint.py` imports from `app/api_models.py`.
   `app/main.py` imports from `app/routes/lint.py`. If models were in `main.py`, the route
   file would import from `main.py`, creating a cycle.

#### Why this status code policy (200 for all lint/qa results, 422 for bad input, 500 for unexpected errors)?

`lint()` and `qa()` do not fail - they return structured results including error messages. A
linter that finds 10 errors is doing its job correctly; the HTTP layer should say "the call
succeeded" (200). Using 400 for "linter found errors" would conflate "your HTTP request was
malformed" with "your spintax has problems" - two different things. This is the same pattern
used by CI linters (GitHub Actions returns 200 for the lint step even when lint errors are
found; the step is marked failed via the payload, not via HTTP status).

422 is reserved for "you sent me something I cannot interpret as a valid request." FastAPI
handles this automatically via pydantic.

500 is reserved for bugs in our code. The route handler's `try/except Exception` ensures the
caller always gets a structured JSON response, never a raw Python traceback.

#### Why this error envelope shape?

The 500 envelope uses FastAPI's native `HTTPException` which serializes as `{"detail": "..."}`.
This matches what FastAPI emits for all its own internal errors (404, 405, etc.), so the caller
needs to handle only one error shape across all HTTP error codes.

The 422 envelope is FastAPI's standard pydantic validation error format. It is the de-facto
standard for FastAPI apps; overriding it with a custom shape would break tools that know how
to parse FastAPI 422 responses (e.g., the FastAPI `/docs` UI, code generators).

#### How does this contract support Phase 3 UI consumption?

Phase 3's JavaScript will do two things with these endpoints:

1. POST to `/api/lint` after the user pastes text (as a real-time lint preview before
   submitting to `/api/spintax`). It reads `response.passed`, `response.errors`, and
   `response.warnings` to render inline annotations.

2. POST to `/api/qa` after a job completes (to re-run QA on the final output before the user
   copies it). It reads `response.passed`, `response.errors`, `response.block_count`.

The consistent `passed: bool` / `errors: list[str]` / `warnings: list[str]` shape across both
endpoints means the UI can use a single render function for lint and QA results. The `meta`
object is ignored by the UI but available for debugging and for the `/docs` API reference.

The Phase 3 JavaScript must NOT pass the response through Jinja2 (gotcha #1 from section 8).
It reads the JSON directly from `fetch()` and sets DOM content via `.textContent`.

#### Why `routes/` package with separate files per endpoint, not a single `routes.py`?

At Phase 2's end, there will be 6+ routes across 4 concern areas (lint, qa, spintax+status,
admin). A single `routes.py` file would be 200+ lines with no internal navigation. The package
pattern means a developer looking for the `/api/lint` handler opens `app/routes/lint.py`
immediately. Each file is a complete, readable unit (<50 lines in Phase 1).

The Phase 2 builder will add `app/routes/spintax.py` (POST /api/spintax + GET /api/status)
and `app/routes/admin.py` (POST /admin/login). No changes to existing route files.

---

## 10. Phase 2 Architecture

**Author:** p2_architect agent | **Date:** 2026-04-26 | **Phase:** 2

Phase 2 is the largest phase. It ships:
- Real `app/jobs.py` (dict + lock + TTL cleanup)
- Real async `app/spintax_runner.run()` (ports v3 tool-calling loop)
- `POST /api/spintax` (kick off, returns job_id)
- `GET /api/status/{job_id}` (poll, returns state + result)
- `POST /admin/login` (sets signed cookie, gates all `/api/*` routes)
- Spend cap enforcement ($20/day, in-memory, resets midnight UTC)
- 6 OpenAI fixtures in `tests/fixtures/openai/`
- Phase 1 NITs resolved
- New files: `app/routes/spintax.py`, `app/routes/admin.py`, `app/dependencies.py`

---

### 10.1 State Machine

#### Diagram

```
                        [START]
                           |
                    jobs.create() called
                    status = "queued"
                           |
                           v
                 asyncio.create_task(run(...))
                           |
                           v
  +------------------------+------------------------+
  |                                                 |
  |         queued --> drafting                     |
  |         (runner starts, first API call begins)  |
  |                      |                          |
  |                      v                          |
  |         drafting --> linting                    |
  |         (model returned draft, tool call made)  |
  |                      |                          |
  |           +----------+----------+               |
  |           |                     |               |
  |    lint PASS               lint FAIL            |
  |    (0 errors)           (1+ errors,             |
  |           |              tool_calls < max)      |
  |           v                     |               |
  |    linting --> qa        linting --> iterating  |
  |           |                     |               |
  |           |              iterating --> linting  |
  |           |              (next fix attempt)     |
  |           |                     |               |
  |           |              lint FAIL at max       |
  |           |              tool_calls reached     |
  |           |                     |               |
  |           |              iterating --> failed   |
  |           |                                     |
  |    qa check runs                                |
  |    qa.passed == True                            |
  |           |                                     |
  |           v                                     |
  |      qa --> done                                |
  |      (terminal PASS state)                      |
  |                                                 |
  |    qa.passed == False                           |
  |           |                                     |
  |           v                                     |
  |      qa --> done  (NOT failed)                  |
  |      result set, qa.passed=False in result      |
  |      (terminal, UI shows yellow warning)        |
  |                                                 |
  |    Any state --> failed                         |
  |    (OpenAI timeout, quota, network, exception)  |
  +------------------------+------------------------+
```

#### Transition Table

Every transition is a contract. Tester writes one test per row.

| # | From | To | Precondition | Action | Postcondition |
|---|------|----|--------------|--------|---------------|
| T1 | queued | drafting | runner coroutine starts | `jobs.update(status="drafting")` called | `job.status == "drafting"` |
| T2 | drafting | linting | model returns first tool call | `jobs.update(status="linting")` before executing lint | `job.status == "linting"`, `job.tool_calls` incremented |
| T3 | linting | iterating | `lint_result.passed == False` AND `tool_calls_made < max_tool_calls` | `jobs.update(status="iterating")` | `job.status == "iterating"` |
| T4 | linting | qa | `lint_result.passed == True` | `jobs.update(status="qa")` | `job.status == "qa"` |
| T5 | iterating | linting | model emits next tool call (next round) | `jobs.update(status="linting")` | `job.status == "linting"`, `job.tool_calls` incremented again |
| T6 | iterating | failed | `tool_calls_made >= max_tool_calls` AND model did not return final body | `jobs.update(status="failed", error="max tool calls reached")` | `job.status == "failed"`, `job.error` set |
| T7 | qa | done | `qa_result["passed"] == True` | `jobs.update(status="done", result=spintax_body)` | `job.status == "done"`, `job.result` set |
| T8 | qa | done | `qa_result["passed"] == False` | `jobs.update(status="done", result=spintax_body)` - result contains qa failure data | `job.status == "done"`, `job.result` set, `result.qa.passed == False` |
| T9 | any | failed | `openai.RateLimitError`, `httpx.TimeoutException`, or uncaught `Exception` | `jobs.update(status="failed", error=<reason_string>)` inside outer `try/except` | `job.status == "failed"`, `job.error` set |

**Decision on T8 (qa fail):** `qa fail` maps to `done` with `qa.passed: false`, NOT `failed`.

Rationale: "Failed" means the system couldn't produce usable output. A QA fail means spintax was generated but has QA concerns - the output IS usable, the team just needs to review it. The UI renders yellow warning, shows the output, allows copy/download. Using `failed` for QA issues would suppress the result entirely - the wrong tradeoff. This was pre-decided in Session_20260426_133102 breadcrumb: "Lint pass but QA fail -> job `done` with `qa.passed: false`, UI yellow warning + still renders output."

**Decision on T9 error strings (constants):**

```python
ERR_TIMEOUT = "openai_timeout"
ERR_QUOTA = "openai_quota"
ERR_MAX_TOOL_CALLS = "max_tool_calls"
ERR_MALFORMED = "malformed_response"
ERR_UNKNOWN = "internal_error"
```

These are machine-readable error keys. Tests assert exact string values. UI maps keys to human messages.

---

### 10.2 `app/jobs.py` Real Implementation Contract

#### Storage

```python
import threading
import uuid
from datetime import datetime, timezone
from typing import Any

_store: dict[str, Job] = {}
_lock = threading.Lock()
```

- One module-level dict. One module-level lock. Nothing else touches `_store` directly.
- All four public functions acquire `_lock` before reading or writing.
- `_lock` is a `threading.Lock()`, not `RLock` - no recursive acquisition, simpler.

#### TTL Cleanup - decision: sweep on `get()` and `list()`, NOT a background task

Trade-off:
- **Background task** runs on a fixed interval (e.g., every 60s) regardless of traffic. Zero read latency impact. Requires registering a lifespan task in `main.py`. Risk: if the loop crashes silently, TTL never cleans up.
- **Sweep on access** (`get()` and `list()`) checks if the job being fetched is expired and evicts it inline. Also evicts a small batch of oldest jobs on each `list()` call. No separate task to manage. Latency impact is O(1) per `get()` (single key check) and O(N) on `list()` (full scan - acceptable since list is called rarely and N is small at MVP scale). Coverage: only jobs that are _accessed_ after expiry get evicted. Zombie jobs that are never read again stay in memory until server restart.

**Decision: sweep on access** for Phase 2 MVP. Rationale:

1. Single-worker Render free tier - one process, no concurrency between workers. Memory pressure from stale jobs is negligible at MVP scale (team of 2-3, max 20 jobs/day).
2. No lifespan complexity. `main.py` already has a lean lifespan; adding a TTL loop adds failure surface.
3. The Phase 2 plan already defers Redis to a later phase - TTL sweep is a temporary mechanism, not a permanent solution. If it becomes a problem, adding a background cleanup task is a 10-line change.
4. The `list()` endpoint does a full sweep and evicts expired entries, so long-running server instances don't accumulate unbounded stale jobs.

TTL = 1 hour from `created_at`.

```python
TTL_SECONDS = 3600  # 1 hour

def _is_expired(job: Job) -> bool:
    return (datetime.now(timezone.utc) - job.created_at).total_seconds() > TTL_SECONDS

def _sweep_expired() -> None:
    """Evict all expired jobs. Caller must hold _lock."""
    expired = [jid for jid, j in _store.items() if _is_expired(j)]
    for jid in expired:
        del _store[jid]
```

#### `Job` Dataclass - final fields

```python
from dataclasses import dataclass, field
from datetime import datetime, timezone
import time

@dataclass
class Job:
    job_id: str
    status: JobStatus
    created_at: datetime
    updated_at: datetime
    input_text: str
    platform: str           # "instantly" | "emailbison"
    model: str              # e.g. "o3"
    result: "SpintaxJobResult | None"  # set when status == "done"
    error: str | None       # machine-readable error key, set when status == "failed"
    cost_usd: float         # accumulated cost, updated on each API call
    tool_calls: int         # accumulated tool calls
    api_calls: int          # accumulated OpenAI API round-trips
    started_at: float       # time.monotonic() at creation, for elapsed_sec calculation
```

`SpintaxJobResult` is a dataclass (not pydantic) - it lives in `app/jobs.py` alongside `Job`.

```python
@dataclass
class SpintaxJobResult:
    spintax_body: str
    lint_errors: list[str]
    lint_warnings: list[str]
    lint_passed: bool
    qa_errors: list[str]
    qa_warnings: list[str]
    qa_passed: bool
    tool_calls: int
    api_calls: int
    cost_usd: float
```

#### Public API - exact signatures

```python
def create(
    input_text: str,
    platform: str,
    model: str,
) -> Job:
    """Create a new job in 'queued' state. Returns the Job.

    Thread-safe. Generates a UUID job_id internally.
    """

def update(
    job_id: str,
    status: JobStatus | None = None,
    result: "SpintaxJobResult | None" = None,
    error: str | None = None,
    cost_usd_delta: float = 0.0,
    tool_calls_delta: int = 0,
    api_calls_delta: int = 0,
) -> Job:
    """Update an existing job. Raises KeyError if job_id not found.

    Thread-safe. Sets updated_at to utcnow().
    Deltas are added to existing accumulated values.
    Does NOT evict expired jobs - caller sees stale jobs if they exist.
    """

def get(job_id: str) -> Job | None:
    """Return Job by ID, or None if not found or expired.

    Thread-safe. Evicts the job inline if expired before returning None.
    """

def list(limit: int = 50) -> list[Job]:
    """Return most-recent jobs first, up to limit. Sweeps expired entries.

    Thread-safe. Full sweep on every call - acceptable at MVP scale.
    """
```

**Important contract:** `update()` raises `KeyError` if `job_id` is unknown. The runner wraps calls to `update()` inside `try/except KeyError` - if a job was TTL-evicted while the runner was still working (job ran > 1 hour), the runner logs a warning and exits cleanly rather than crashing.

---

### 10.3 `app/spintax_runner.run()` Async Contract

#### Signature

```python
async def run(
    job_id: str,
    plain_body: str,
    platform: str,
    model: str | None = None,
    reasoning_effort: str = "medium",
    tolerance: float = 0.05,
    tolerance_floor: int = 3,
    max_tool_calls: int = 10,
) -> None:
```

Returns `None`. Side effects only: `jobs.update()` calls and `spend` accumulation.
Never raises - all exceptions are caught and mapped to `failed` state.

#### Internal Structure

```python
async def run(...) -> None:
    if model is None:
        model = settings.default_model

    try:
        # --- T1: queued -> drafting ---
        jobs.update(job_id, status="drafting")

        client = _make_openai_client()
        system_prompt = build_system_prompt(platform, _skills_dir())
        messages = _build_initial_messages(plain_body, platform, system_prompt)
        tools = [TOOL_LINT_SPINTAX]
        totals = {"api_calls": 0, "tool_calls": 0, "cost_usd": 0.0}
        tool_calls_made = 0

        for _round in range(max_tool_calls + 2):
            response = await _call_openai(client, model, reasoning_effort, messages, tools)

            cost = _compute_cost(response.usage, model)
            totals["cost_usd"] += cost["total_cost_usd"]
            totals["api_calls"] += 1
            jobs.update(job_id, api_calls_delta=1, cost_usd_delta=cost["total_cost_usd"])

            msg = response.choices[0].message

            if msg.tool_calls:
                # Append assistant message to conversation
                messages.append(_assistant_msg(msg))

                for tc in msg.tool_calls:
                    if tool_calls_made >= max_tool_calls:
                        # Tell model to emit final body now
                        messages.append(_tool_result_msg(tc.id, {
                            "error": f"Max tool calls ({max_tool_calls}) reached. Emit final body now."
                        }))
                        # T6 will trigger next round when model doesn't call tool again
                        continue

                    # --- T2: drafting -> linting OR T5: iterating -> linting ---
                    jobs.update(job_id, status="linting")

                    args = json.loads(tc.function.arguments)
                    body = args.get("spintax_body", "")
                    lint_result = _run_lint_tool(body, platform, tolerance, tolerance_floor)
                    tool_calls_made += 1
                    jobs.update(job_id, tool_calls_delta=1)

                    messages.append(_tool_result_msg(tc.id, lint_result))

                    if lint_result["passed"]:
                        # T4: linting -> qa (handled below when model returns final body)
                        # model will return final body in next round (no more tool calls)
                        pass
                    else:
                        # T3: linting -> iterating
                        jobs.update(job_id, status="iterating")
                        if tool_calls_made >= max_tool_calls:
                            # T6: iterating -> failed
                            jobs.update(job_id, status="failed", error=ERR_MAX_TOOL_CALLS)
                            _add_spend(totals["cost_usd"])
                            return

            else:
                # Model returned final body (no tool calls)
                final_body = _strip_wrapping(msg.content or "")

                if not final_body.strip():
                    jobs.update(job_id, status="failed", error=ERR_MALFORMED)
                    _add_spend(totals["cost_usd"])
                    return

                # --- T4: linting -> qa ---
                jobs.update(job_id, status="qa")

                qa_result = qa(final_body, plain_body, platform)

                result = SpintaxJobResult(
                    spintax_body=final_body,
                    lint_errors=[],
                    lint_warnings=[],
                    lint_passed=True,
                    qa_errors=qa_result["errors"],
                    qa_warnings=qa_result["warnings"],
                    qa_passed=qa_result["passed"],
                    tool_calls=tool_calls_made,
                    api_calls=totals["api_calls"],
                    cost_usd=totals["cost_usd"],
                )

                # T7 or T8: qa -> done (regardless of qa.passed)
                jobs.update(job_id, status="done", result=result)
                _add_spend(totals["cost_usd"])
                return

        # Round budget exhausted without final answer
        jobs.update(job_id, status="failed", error=ERR_MAX_TOOL_CALLS)
        _add_spend(totals["cost_usd"])

    except openai.RateLimitError:
        # T9: any -> failed (quota)
        _safe_fail(job_id, ERR_QUOTA)
    except (httpx.TimeoutException, openai.APITimeoutError):
        # T9: any -> failed (timeout)
        _safe_fail(job_id, ERR_TIMEOUT)
    except KeyError:
        # Job was TTL-evicted during run - log and exit silently
        import logging
        logging.warning("spintax_runner: job %s evicted during run (TTL)", job_id)
    except Exception:
        # T9: any -> failed (unknown)
        import logging
        logging.exception("spintax_runner: unexpected error for job %s", job_id)
        _safe_fail(job_id, ERR_UNKNOWN)
```

#### `_make_openai_client()` - async client

```python
def _make_openai_client() -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(api_key=settings.openai_api_key)
```

Use `openai.AsyncOpenAI`, NOT `openai.OpenAI`. The runner is an async coroutine; a sync client would block the event loop. The v3 CLI used a sync client because it ran in a thread. The web runner runs directly in the asyncio event loop.

#### `_call_openai()` - no retry at the runner level

```python
async def _call_openai(
    client: openai.AsyncOpenAI,
    model: str,
    reasoning_effort: str,
    messages: list[dict],
    tools: list[dict],
) -> Any:
    """Single OpenAI API call. Raises on error - caller handles."""
    is_reasoning = model in REASONING_MODELS
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "tools": tools,
        "tool_choice": "auto",
    }
    if is_reasoning:
        kwargs["reasoning_effort"] = reasoning_effort
    else:
        kwargs["temperature"] = 0.6
    return await client.chat.completions.create(**kwargs)
```

**No retry in the runner.** The v3 CLI had a 3-attempt retry loop with `time.sleep()`. The async runner does NOT retry - reasons:

1. `time.sleep()` in async context blocks the event loop. `asyncio.sleep()` would be correct but adds complexity.
2. Retries here are best handled at the infrastructure level (Render restart policy) or by the user hitting "retry" in the UI.
3. The runner failing fast with `ERR_QUOTA` or `ERR_TIMEOUT` gives the user a clear error state they can act on.

#### `_safe_fail()` helper

```python
def _safe_fail(job_id: str, error: str) -> None:
    """Update job to failed state. Silently ignores KeyError (job evicted)."""
    try:
        jobs.update(job_id, status="failed", error=error)
    except KeyError:
        pass
```

#### `build_system_prompt()` implementation

```python
def build_system_prompt(platform: str, skills_dir: Path) -> str:
    """Assemble system prompt: hard rules + skill markdown files."""
    hard_rules = _build_hard_rules(platform, max_tool_calls=10)
    orchestrator = (skills_dir / "SKILL.md").read_text(encoding="utf-8")
    length = (skills_dir / "_rules-length.md").read_text(encoding="utf-8")
    ai_patterns = (skills_dir / "_rules-ai-patterns.md").read_text(encoding="utf-8")
    spam_words = (skills_dir / "_rules-spam-words.md").read_text(encoding="utf-8")
    fmt = (skills_dir / f"_format-{platform}.md").read_text(encoding="utf-8")

    parts = [
        hard_rules,
        "\n" + "=" * 60,
        "ORCHESTRATOR (pipeline overview)",
        "=" * 60,
        orchestrator,
        "\n" + "=" * 60, "LENGTH RULE", "=" * 60, length,
        "\n" + "=" * 60, "AI-PATTERN RULES", "=" * 60, ai_patterns,
        "\n" + "=" * 60, "SPAM TRIGGER WORDS (warning-level, avoid unless load-bearing)", "=" * 60, spam_words,
        "\n" + "=" * 60, f"PLATFORM FORMAT ({platform.upper()})", "=" * 60, fmt,
    ]
    return "\n".join(parts)

def _skills_dir() -> Path:
    return Path(__file__).resolve().parent / "skills" / "spintax"
```

The hard rules string is extracted verbatim from `spintax_openai_v3.py:build_system_prompt()` lines 113-208. Builder must port it character-for-character (including the `{{{{firstName}}}}` double-brace escaping for Python string formatting).

---

### 10.4 Auth Model

#### Decision: stdlib HMAC instead of `itsdangerous`

`itsdangerous` is NOT in the transitive dependency tree of `fastapi[standard]`. Confirmed:

```
$ .venv/bin/python -c "import itsdangerous"
ModuleNotFoundError: No module named 'itsdangerous'
```

`starlette.middleware.sessions` lists `itsdangerous` as an optional extra, but the base `fastapi[standard]` install does NOT bring it in.

Per Rule 8 ("no new dependencies unless absolutely required"), we use `stdlib hmac + hashlib + secrets` to sign cookies. HMAC-SHA256 provides equivalent security to `itsdangerous` TimestampSigner for this use case. The cookie payload is a simple JSON blob; we do not need `itsdangerous`'s URL-safe base64 + timestamp embedded in the token - we embed the expiry timestamp in the JSON payload ourselves.

If a future phase adds `starlette[full]` or uses `SessionMiddleware`, `itsdangerous` will come in as a transitive dep then. Do NOT add it now.

#### Cookie Signing - `app/auth.py` full implementation contract

```python
"""Cookie-based authentication helpers.

What this does:
    Signs and verifies session cookies using HMAC-SHA256 (stdlib).
    No external dependencies. Admin password compared constant-time.
    All /api/* routes use require_auth dependency (app/dependencies.py).

What it depends on:
    Python stdlib: hashlib, hmac, json, base64, secrets, datetime.
    app.config for ADMIN_PASSWORD and SESSION_SECRET env vars.

What depends on it:
    app/routes/admin.py (set_session_cookie)
    app/dependencies.py (is_authenticated)
"""
```

Cookie format:

```
session=<base64url(json_payload)>.<hex_hmac>
```

Where:
- `json_payload = {"login_at": "<iso8601_utc>", "expires_at": "<iso8601_utc>"}`
- `hex_hmac = hmac.new(SESSION_SECRET.encode(), json_payload_bytes, sha256).hexdigest()`

Verification steps:
1. Split cookie value on `.` - must have exactly 2 parts
2. Decode base64url payload
3. Parse JSON
4. Compute HMAC of raw payload bytes using SESSION_SECRET
5. Compare computed HMAC to cookie HMAC using `hmac.compare_digest()` (constant-time)
6. Check `expires_at` > now

```python
SESSION_DURATION_DAYS = 7

def sign_cookie(login_at: datetime) -> str:
    """Return signed cookie value string (everything after 'session=')."""

def verify_cookie(value: str) -> bool:
    """Return True if cookie value is valid, unexpired, and HMAC matches."""

def set_session_cookie(response: Response) -> None:
    """Set the session cookie on a FastAPI response object."""

def is_authenticated(request: Request) -> bool:
    """Return True if the request carries a valid session cookie."""

def verify_password(candidate: str) -> bool:
    """Constant-time compare candidate against ADMIN_PASSWORD setting."""
```

#### New env vars (both required in Phase 2)

| Env var | Purpose | Default | Notes |
|---------|---------|---------|-------|
| `ADMIN_PASSWORD` | Login password | `""` (server won't accept any login if blank) | Already in config.py skeleton |
| `SESSION_SECRET` | HMAC signing key for cookies | `""` (crash on startup if blank in prod) | New - add to `app/config.py` |

Add to `app/config.py`:

```python
session_secret: str = Field(default="", validation_alias="SESSION_SECRET")
```

On startup, if `session_secret == ""` and not in test mode, log a critical warning. Do NOT crash - this allows local dev without setting the env var. But in tests, `SESSION_SECRET=test-secret-32chars-minimum` must be set in `conftest.py`.

#### Route gating

All `/api/*` routes require auth. Public routes:

| Route | Auth required? |
|-------|---------------|
| `GET /health` | No |
| `POST /admin/login` | No |
| `GET /` | No (Phase 3) |
| `GET /docs` | No (Phase 6) |
| `GET /llms.txt` | No (Phase 6) |
| `GET /openapi.json` | No |
| `POST /api/lint` | **Yes** |
| `POST /api/qa` | **Yes** |
| `POST /api/spintax` | **Yes** |
| `GET /api/status/{job_id}` | **Yes** |

**Note:** `/api/lint` and `/api/qa` were open in Phase 1 (by design - auth ships in Phase 2 as a single gate). Phase 2 gates them retroactively. This is expected and was planned from Phase 1 architecture: "both routes are open (no auth) on purpose; auth gating lands in Phase 2."

---

### 10.5 Spend Cap Algorithm

#### Storage - in-process singleton in `app/spend.py`

```python
_state = {"date": "", "usd_spent": 0.0}
_lock = threading.Lock()
```

`date` is a UTC date string: `"2026-04-26"`. If `date != _today_utc()`, reset `usd_spent = 0.0` and update `date`.

#### Check + enforcement sequence

```
POST /api/spintax handler:
  1. Authenticate (require_auth dep)
  2. Check spend: if spend.today_total() >= settings.daily_spend_cap_usd:
       return 429 + ErrorEnvelope(error="daily_cap_hit", ...)
  3. Create job (jobs.create())
  4. asyncio.create_task(run(job_id, ...))
  5. Return SpintaxResponse(job_id=job_id)

spintax_runner.run() - at end (success or failure):
  6. spend.add(cost_usd) - unconditional
```

**Why check BEFORE creating the job, not AFTER?** We want to reject the request before creating an orphaned job in the store. A job created but immediately cancelled is confusing. The cost has not been incurred yet - checking at request time is correct.

**Why add cost AFTER run(), not incrementally?** Two options:

- Incremental: add per-API-call cost inside the loop. More accurate. But if the runner crashes before adding the final batch, the counter under-counts.
- End-of-run: add total cost once at the very end. Slightly under-counts if server crashes mid-run, but cleaner to implement and the difference is <$0.02 per run in practice.

**Decision: end-of-run add** via `_add_spend()` helper called just before each `return` in `run()`. Note: `_add_spend()` is called even on `failed` runs - the API tokens were consumed regardless.

#### 429 response shape

```json
{
  "error": "daily_cap_hit",
  "message": "Daily spend cap of $20.00 reached. Try again after midnight UTC.",
  "details": {
    "cap_usd": 20.0,
    "spent_usd": 19.83,
    "resets_at": "2026-04-27T00:00:00Z"
  }
}
```

Use `ErrorEnvelope` model (defined in section 10.6).

#### Spend cap state key - UTC date string

**Decision: UTC date string** (`"2026-04-26"`) not a Unix midnight timestamp.

Rationale: Date string is human-readable in logs. Comparison is a string equality check (`==`). Reset condition is trivially clear. No arithmetic needed. Both approaches are O(1) and thread-safe with the lock - date string wins on readability.

#### Rate limiting per IP - deferred from Phase 2

The session plan mentioned "10/hour per IP" as a consideration. This is NOT included in Phase 2.

Rationale: The team uses a single shared password, meaning all users share the same auth identity. Per-IP rate limiting on top of the daily spend cap adds complexity (IP extraction from headers, X-Forwarded-For parsing, Render's proxy setup) with minimal benefit - the spend cap already protects against runaway usage, and the tool is used by 2-3 team members who are not adversarial. Defer to v2 if abuse becomes a real concern.

---

### 10.6 New Pydantic Models (`app/api_models.py` additions)

Add to the existing file. Do NOT create a new file.

```python
# Phase 2 additions - append after existing QAResponse class

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
        description="Reasoning effort for o-series models. Ignored for non-reasoning models.",
    )

    @field_validator("platform")
    @classmethod
    def platform_must_be_valid(cls, v: str) -> str:
        if v not in VALID_PLATFORMS:
            raise ValueError(
                f"platform must be one of {sorted(VALID_PLATFORMS)!r}, got {v!r}"
            )
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
    """Shape of the result field in a completed job. Only present when status == 'done'."""

    spintax_body: str
    lint: LintResultEmbed
    qa: QAResultEmbed
    tool_calls: int
    api_calls: int
    cost_usd: float


class JobStatusResponse(BaseModel):
    """Response body for GET /api/status/{job_id}."""

    job_id: str
    status: str                             # JobStatus literal
    progress: dict[str, Any] | None = None  # reserved for Phase 3 UI
    result: SpintaxJobResult | None = None  # only when status == "done"
    error: str | None = None               # only when status == "failed"
    cost_usd: float
    elapsed_sec: float


class LoginRequest(BaseModel):
    """Request body for POST /admin/login."""

    password: str = Field(description="Admin password (ADMIN_PASSWORD env var).")


class LoginResponse(BaseModel):
    """Response body for POST /admin/login. Cookie is set via Set-Cookie header."""

    success: bool


class ErrorEnvelope(BaseModel):
    """Consistent shape for non-422 error responses (429, 401, 500)."""

    error: str = Field(description="Machine-readable error key.")
    message: str = Field(description="Human-readable error message.")
    details: dict[str, Any] | None = Field(default=None, description="Extra context.")
```

**`Literal` import note:** Add `Literal` to the existing `from typing import` statement at the top of `api_models.py`.

---

### 10.7 Routes Layout

#### New files

```
app/
  routes/
    spintax.py       # POST /api/spintax + GET /api/status/{job_id}
    admin.py         # POST /admin/login
  dependencies.py    # require_auth FastAPI dependency + spend cap check helper
```

#### `app/routes/spintax.py`

```python
"""POST /api/spintax and GET /api/status/{job_id} routes.

What this does:
    POST /api/spintax: validates request, checks auth (via require_auth dep),
    checks spend cap, creates a job, fires asyncio.create_task(run(...)),
    returns job_id immediately.

    GET /api/status/{job_id}: returns current job state. 404 if job not found.
    No auth on status? No - status IS auth-gated too (see auth table in 10.4).

What it depends on:
    app.jobs.create, app.jobs.get
    app.spintax_runner.run
    app.api_models.SpintaxRequest, SpintaxResponse, JobStatusResponse, ErrorEnvelope
    app.dependencies.require_auth
    app.spend (check daily cap)
    app.config.get_settings

What depends on it:
    app.main mounts this router.
"""
```

Route handlers:

```python
@router.post(
    "/api/spintax",
    response_model=SpintaxResponse,
    dependencies=[Depends(require_auth)],
)
async def create_spintax_job(
    body: SpintaxRequest,
    settings: Annotated[Settings, Depends(get_settings)],
) -> SpintaxResponse:
    # Check spend cap before creating job
    if spend.today_total() >= settings.daily_spend_cap_usd:
        raise HTTPException(
            status_code=429,
            detail=ErrorEnvelope(
                error="daily_cap_hit",
                message=f"Daily spend cap of ${settings.daily_spend_cap_usd:.2f} reached.",
                details={
                    "cap_usd": settings.daily_spend_cap_usd,
                    "spent_usd": spend.today_total(),
                    "resets_at": spend.reset_at().isoformat(),
                },
            ).model_dump(),
        )

    resolved_model = body.model or settings.default_model
    job = jobs.create(
        input_text=body.text,
        platform=body.platform,
        model=resolved_model,
    )
    asyncio.create_task(
        spintax_runner.run(
            job_id=job.job_id,
            plain_body=body.text,
            platform=body.platform,
            model=resolved_model,
            reasoning_effort=body.reasoning_effort,
        )
    )
    return SpintaxResponse(job_id=job.job_id)


@router.get(
    "/api/status/{job_id}",
    dependencies=[Depends(require_auth)],
)
async def get_job_status(job_id: str) -> JobStatusResponse:
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")

    result: SpintaxJobResult | None = None
    if job.result is not None and job.status == "done":
        r = job.result
        result = SpintaxJobResult(
            spintax_body=r.spintax_body,
            lint=LintResultEmbed(
                passed=r.lint_passed, errors=r.lint_errors, warnings=r.lint_warnings
            ),
            qa=QAResultEmbed(
                passed=r.qa_passed, errors=r.qa_errors, warnings=r.qa_warnings
            ),
            tool_calls=r.tool_calls,
            api_calls=r.api_calls,
            cost_usd=r.cost_usd,
        )

    elapsed = time.monotonic() - job.started_at
    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        result=result,
        error=job.error,
        cost_usd=job.cost_usd,
        elapsed_sec=round(elapsed, 1),
    )
```

**`asyncio.create_task` vs `BackgroundTasks`:**

Decision: `asyncio.create_task`.

Rationale:
- `BackgroundTasks` runs the task AFTER the response is sent, within the SAME request lifetime. If the Gunicorn worker is killed mid-request, the background task dies with it. More importantly, `BackgroundTasks` was designed for quick post-response cleanup (e.g., sending emails after a form submit), not 100-second AI jobs.
- `asyncio.create_task` schedules the coroutine on the event loop immediately. It survives after the response is returned. The worker can accept new requests while the task runs. This is the correct pattern for long-running async jobs in a single-worker FastAPI app.
- `asyncio.create_task` requires the handler to be `async def` (it is). No extra dependencies.

The trade-off: if the process dies, `create_task` jobs die too. This is acceptable for MVP - the user sees a `failed` job or gets a 500 on status poll, and retries. Redis-backed queues (Celery, RQ) are the Phase 5+ answer if persistence is needed.

#### `app/routes/admin.py`

```python
"""POST /admin/login route.

What this does:
    Validates the admin password and sets a signed session cookie.
    Returns LoginResponse. Cookie carries all session state - no server-side session.

What it depends on:
    app.auth.verify_password, app.auth.set_session_cookie
    app.api_models.LoginRequest, LoginResponse

What depends on it:
    app.main mounts this router.
"""
```

```python
@router.post("/admin/login", response_model=LoginResponse)
async def admin_login(
    body: LoginRequest,
    response: Response,
) -> LoginResponse:
    if not auth.verify_password(body.password):
        raise HTTPException(status_code=401, detail="invalid password")
    auth.set_session_cookie(response)
    return LoginResponse(success=True)
```

HTTP 401 on wrong password. No rate limiting in Phase 2 (team-only tool, shared password). 401 is correct (not 403) because the credentials are wrong, not forbidden.

#### `app/dependencies.py`

```python
"""FastAPI dependency functions for auth gating and spend checks.

What this does:
    require_auth: FastAPI Depends() function that raises 401 if the
    request does not carry a valid signed session cookie.

What it depends on:
    app.auth.is_authenticated

What depends on it:
    app/routes/spintax.py (all routes)
    app/routes/lint.py (Phase 2 adds auth gate retroactively)
    app/routes/qa.py (Phase 2 adds auth gate retroactively)
"""

from fastapi import Depends, HTTPException, Request


def require_auth(request: Request) -> None:
    """FastAPI dependency. Raises 401 if session cookie is missing or invalid."""
    from app.auth import is_authenticated
    if not is_authenticated(request):
        raise HTTPException(status_code=401, detail="authentication required")
```

The import of `is_authenticated` is inside the function body to avoid circular imports at module load time (`auth.py` -> `config.py` -> no issue, but defensive habit).

#### `app/main.py` changes

Add to lifespan and router mounts:

```python
from app.routes.spintax import router as spintax_router
from app.routes.admin import router as admin_router

app.include_router(spintax_router)
app.include_router(admin_router)
```

Also add `Depends(require_auth)` to the existing lint and qa routers. The cleanest way is to pass `dependencies=[Depends(require_auth)]` in `include_router()`:

```python
from app.dependencies import require_auth

app.include_router(lint_router, dependencies=[Depends(require_auth)])
app.include_router(qa_router, dependencies=[Depends(require_auth)])
app.include_router(spintax_router)  # auth is inside the router already
app.include_router(admin_router)    # public
```

This keeps Phase 1 route files unchanged and adds auth at the mount level.

---

### 10.8 OpenAI Fixtures

All 6 fixtures at `tests/fixtures/openai/`. Each is a Python dict serialized to JSON that respx intercepts when `openai.AsyncOpenAI.chat.completions.create` is called.

The fixture shape matches the `openai.types.chat.ChatCompletion` response object. All fixtures use a minimal, exact structure that the runner actually accesses.

#### `o3_pass_first_try.json`

Two API calls total: first call returns a tool call, second call returns the final body after lint passes.

```json
[
  {
    "id": "chatcmpl-fixture-pass-1",
    "object": "chat.completion",
    "model": "o3",
    "choices": [{
      "index": 0,
      "message": {
        "role": "assistant",
        "content": null,
        "tool_calls": [{
          "id": "call_fixture_001",
          "type": "function",
          "function": {
            "name": "lint_spintax",
            "arguments": "{\"spintax_body\": \"{{RANDOM | Hi there, | Hello there,}}\\n\\nBody line.\"}"
          }
        }]
      },
      "finish_reason": "tool_calls"
    }],
    "usage": {"prompt_tokens": 500, "completion_tokens": 50, "total_tokens": 550}
  },
  {
    "id": "chatcmpl-fixture-pass-2",
    "object": "chat.completion",
    "model": "o3",
    "choices": [{
      "index": 0,
      "message": {
        "role": "assistant",
        "content": "{{RANDOM | Hi there, | Hello there,}}\n\nBody line.",
        "tool_calls": null
      },
      "finish_reason": "stop"
    }],
    "usage": {"prompt_tokens": 600, "completion_tokens": 60, "total_tokens": 660}
  }
]
```

The fixture is a JSON array. Each element is one API call response. `respx` mock returns them in sequence.

#### `o3_iterate_once.json`

Three API calls: first tool call returns lint errors, second tool call returns lint pass, third call returns final body.

Array of 3 response objects. Second response includes tool_calls with lint body that passes. Third response has no tool_calls and a content string.

#### `o3_iterate_max.json`

11 API calls (max_tool_calls=10 + 1 final "emit now" round). All tool calls. No final body returned within budget. Fixture is array of 12 response objects - the last one has tool_calls: null but content is empty string (simulating model confusion after max hit).

#### `o3_timeout.json`

Not a response object. This fixture is a sentinel file that signals the mock to raise `httpx.TimeoutException` on the first call.

```json
{"__fixture_type": "timeout"}
```

The test's respx setup reads this sentinel and raises the exception instead of returning a response.

#### `o3_quota.json`

```json
{"__fixture_type": "rate_limit_error", "status_code": 429}
```

Signals respx to raise `openai.RateLimitError`.

#### `o3_malformed.json`

Response object where `choices[0].message` has neither `content` nor `tool_calls`:

```json
[{
  "id": "chatcmpl-fixture-malformed",
  "object": "chat.completion",
  "model": "o3",
  "choices": [{
    "index": 0,
    "message": {
      "role": "assistant",
      "content": "",
      "tool_calls": null
    },
    "finish_reason": "stop"
  }],
  "usage": {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105}
}]
```

Empty `content` with no tool_calls triggers the malformed-response branch in the runner (empty final body after `_strip_wrapping()`).

**Builder note on respx setup:** The runner uses `openai.AsyncOpenAI`. To mock with respx, intercept at the HTTP level:

```python
import respx
import httpx

@respx.mock
async def test_something():
    respx.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=fixture_data[0])
    )
    # ...
```

Or use `respx.MockRouter` as a context manager. The key: respx intercepts `httpx` transport, which `openai.AsyncOpenAI` uses under the hood.

---

### 10.9 Test Plan Handoff (for tester)

All tests go in existing or new files per the Phase 0 structure. New files for Phase 2:
- `tests/test_jobs.py`
- `tests/test_spend.py`
- `tests/test_routes_spintax.py`
- `tests/test_auth.py`

Plus additions to `tests/conftest.py`.

#### `conftest.py` additions

```python
# Add to os.environ.setdefault calls:
os.environ.setdefault("SESSION_SECRET", "test-secret-32-characters-minimum!!")

# Add fixtures:
@pytest.fixture
def authed_client(client):
    """TestClient pre-authenticated with a valid session cookie."""
    resp = client.post("/admin/login", json={"password": "test-password"})
    assert resp.status_code == 200
    return client  # cookies are stored on the TestClient session
```

#### State machine tests - `tests/test_jobs.py`

```python
# T1: queued -> drafting
def test_t1_queued_to_drafting():
    job = jobs.create("text", "instantly", "o3")
    assert job.status == "queued"
    updated = jobs.update(job.job_id, status="drafting")
    assert updated.status == "drafting"

# T2: drafting -> linting
def test_t2_drafting_to_linting():
    job = jobs.create("text", "instantly", "o3")
    jobs.update(job.job_id, status="drafting")
    updated = jobs.update(job.job_id, status="linting", tool_calls_delta=1)
    assert updated.status == "linting"
    assert updated.tool_calls == 1

# T3: linting -> iterating
def test_t3_linting_to_iterating():
    job = jobs.create("text", "instantly", "o3")
    jobs.update(job.job_id, status="linting")
    updated = jobs.update(job.job_id, status="iterating")
    assert updated.status == "iterating"

# T4: linting -> qa
def test_t4_linting_to_qa():
    job = jobs.create("text", "instantly", "o3")
    jobs.update(job.job_id, status="linting")
    updated = jobs.update(job.job_id, status="qa")
    assert updated.status == "qa"

# T5: iterating -> linting
def test_t5_iterating_to_linting():
    job = jobs.create("text", "instantly", "o3")
    jobs.update(job.job_id, status="iterating")
    updated = jobs.update(job.job_id, status="linting", tool_calls_delta=1)
    assert updated.status == "linting"

# T6: iterating -> failed (max tool calls)
def test_t6_iterating_to_failed_max():
    job = jobs.create("text", "instantly", "o3")
    jobs.update(job.job_id, status="iterating")
    updated = jobs.update(job.job_id, status="failed", error=ERR_MAX_TOOL_CALLS)
    assert updated.status == "failed"
    assert updated.error == ERR_MAX_TOOL_CALLS

# T7: qa -> done (QA pass)
def test_t7_qa_to_done_pass():
    job = jobs.create("text", "instantly", "o3")
    jobs.update(job.job_id, status="qa")
    result = SpintaxJobResult(  # dataclass from jobs.py
        spintax_body="...", lint_errors=[], lint_warnings=[], lint_passed=True,
        qa_errors=[], qa_warnings=[], qa_passed=True,
        tool_calls=1, api_calls=2, cost_usd=0.01,
    )
    updated = jobs.update(job.job_id, status="done", result=result)
    assert updated.status == "done"
    assert updated.result.qa_passed is True

# T8: qa -> done (QA fail - still done, not failed)
def test_t8_qa_to_done_qa_fail():
    job = jobs.create("text", "instantly", "o3")
    jobs.update(job.job_id, status="qa")
    result = SpintaxJobResult(
        spintax_body="...", lint_errors=[], lint_warnings=[], lint_passed=True,
        qa_errors=["V1 fidelity mismatch"], qa_warnings=[], qa_passed=False,
        tool_calls=1, api_calls=2, cost_usd=0.01,
    )
    updated = jobs.update(job.job_id, status="done", result=result)
    assert updated.status == "done"       # NOT "failed"
    assert updated.result.qa_passed is False
    assert updated.error is None

# T9: any -> failed (exception in runner)
def test_t9_any_to_failed_exception():
    job = jobs.create("text", "instantly", "o3")
    jobs.update(job.job_id, status="drafting")
    updated = jobs.update(job.job_id, status="failed", error=ERR_UNKNOWN)
    assert updated.status == "failed"
    assert updated.error == ERR_UNKNOWN
```

Additional jobs tests:
- `test_get_returns_none_for_unknown_id` - `get("bad-id")` returns None
- `test_update_raises_keyerror_for_unknown_id` - `update("bad-id")` raises KeyError
- `test_cost_delta_accumulates` - two updates with `cost_usd_delta=0.05` results in `cost_usd == 0.10`
- `test_ttl_eviction_on_get` - create job, mock `created_at` to T-2h, call `get()`, returns None
- `test_ttl_not_evicted_before_expiry` - create job, mock `created_at` to T-30min, call `get()`, returns job
- `test_list_sweeps_expired` - create 3 jobs (2 expired, 1 fresh), call `list()`, returns only 1

#### Concurrency test - `tests/test_jobs.py`

```python
def test_concurrent_updates_no_race():
    """100 parallel threads each update cost_usd_delta=0.01. Final cost == 1.00."""
    import concurrent.futures
    job = jobs.create("text", "instantly", "o3")
    with concurrent.futures.ThreadPoolExecutor(max_workers=20) as pool:
        futures = [pool.submit(jobs.update, job.job_id, cost_usd_delta=0.01)
                   for _ in range(100)]
        concurrent.futures.wait(futures)
    final = jobs.get(job.job_id)
    assert abs(final.cost_usd - 1.00) < 1e-9
```

#### Spend cap tests - `tests/test_spend.py`

```python
def test_under_cap_returns_false():
    spend.reset()  # test helper - clears state
    spend.add(5.0)
    assert spend.today_total() == 5.0

def test_at_cap_blocks_api_spintax(authed_client):
    spend.reset()
    spend.add(20.0)  # hit exactly at cap
    resp = authed_client.post("/api/spintax", json={...})
    assert resp.status_code == 429
    body = resp.json()
    assert body["error"] == "daily_cap_hit"
    assert body["details"]["spent_usd"] == 20.0

def test_midnight_utc_reset():
    """After reset, today_total is 0.0."""
    spend.reset()
    spend.add(19.0)
    spend._force_date("2000-01-01")  # force stale date
    # Next call resets because date != today
    assert spend.today_total() == 0.0

def test_cap_response_shape():
    """429 body matches ErrorEnvelope schema."""
    spend.reset()
    spend.add(20.0)
    resp = authed_client.post("/api/spintax", json={...})
    body = resp.json()
    assert "error" in body
    assert "message" in body
    assert "details" in body
    assert "resets_at" in body["details"]
```

`spend.reset()` and `spend._force_date()` are test helpers added to `app/spend.py` as module-level functions, prefixed with underscore to signal non-public use. They're necessary for deterministic testing of the date-reset logic.

#### Auth tests - `tests/test_auth.py`

```python
def test_login_success_returns_200(client):
    resp = client.post("/admin/login", json={"password": "test-password"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert "session" in resp.cookies

def test_login_wrong_password_returns_401(client):
    resp = client.post("/admin/login", json={"password": "wrong"})
    assert resp.status_code == 401

def test_gated_route_without_cookie_returns_401(client):
    resp = client.post("/api/lint", json={"text": "x", "platform": "instantly"})
    assert resp.status_code == 401

def test_gated_route_with_valid_cookie_returns_200(authed_client):
    resp = authed_client.post("/api/lint", json={
        "text": "{{RANDOM | Hi | Hello}}", "platform": "instantly"
    })
    assert resp.status_code == 200

def test_expired_cookie_returns_401(client):
    """Manually craft a cookie with expired timestamp."""
    import json, base64, hmac, hashlib
    from app.config import get_settings
    from datetime import datetime, timedelta, timezone
    settings = get_settings()
    # Build an expired payload
    expired = datetime.now(timezone.utc) - timedelta(days=8)
    payload = json.dumps({
        "login_at": expired.isoformat(),
        "expires_at": (expired + timedelta(days=7)).isoformat(),
    }).encode()
    sig = hmac.new(settings.session_secret.encode(), payload, hashlib.sha256).hexdigest()
    cookie_val = base64.urlsafe_b64encode(payload).decode() + "." + sig
    client.cookies.set("session", cookie_val)
    resp = client.post("/api/lint", json={"text": "x", "platform": "instantly"})
    assert resp.status_code == 401

def test_tampered_cookie_returns_401(client):
    """Cookie with valid structure but wrong HMAC."""
    import json, base64
    payload = json.dumps({"login_at": "2026-01-01T00:00:00Z", "expires_at": "2030-01-01T00:00:00Z"}).encode()
    cookie_val = base64.urlsafe_b64encode(payload).decode() + ".deadbeef" * 8
    client.cookies.set("session", cookie_val)
    resp = client.post("/api/lint", json={"text": "x", "platform": "instantly"})
    assert resp.status_code == 401
```

#### Route integration tests - `tests/test_routes_spintax.py`

These use respx to mock OpenAI. All run offline.

```python
# Happy path - job created, returns job_id
def test_post_spintax_returns_job_id(authed_client, respx_mock):
    # Mock OpenAI to return o3_pass_first_try fixture
    respx_mock.post("https://api.openai.com/v1/chat/completions").mock(
        return_value=httpx.Response(200, json=fixture_pass_first_try[0])
    )
    resp = authed_client.post("/api/spintax", json={
        "text": "Hey there,\n\nBody text here.", "platform": "instantly"
    })
    assert resp.status_code == 200
    body = resp.json()
    assert "job_id" in body
    assert isinstance(body["job_id"], str)

# Status 404 for unknown job
def test_get_status_404_for_unknown(authed_client):
    resp = authed_client.get("/api/status/nonexistent-job-id")
    assert resp.status_code == 404

# Full happy path (queued -> done via mock)
async def test_full_happy_path(authed_client, respx_mock):
    # POST /api/spintax
    # Poll /api/status until status == "done"
    # Assert result.spintax_body is non-empty
    # Assert result.lint.passed is True
    # Assert result.qa.passed is True or False (either is acceptable for "done")
    ...

# Failure modes
def test_timeout_maps_to_failed_job(authed_client, respx_mock):
    respx_mock.post("https://api.openai.com/v1/chat/completions").mock(
        side_effect=httpx.TimeoutException("timeout")
    )
    resp = authed_client.post("/api/spintax", json={...})
    job_id = resp.json()["job_id"]
    # Wait for task to complete (asyncio)
    status = authed_client.get(f"/api/status/{job_id}").json()
    assert status["status"] == "failed"
    assert status["error"] == "openai_timeout"

def test_quota_maps_to_failed_job(authed_client, respx_mock): ...
def test_job_not_found_returns_404(authed_client): ...
def test_malformed_response_maps_to_failed_job(authed_client, respx_mock): ...
def test_qa_fail_still_returns_done(authed_client, respx_mock): ...
```

**Test note on `asyncio.create_task`:** Tests with `TestClient` (sync ASGI test client) will need `anyio` or `asyncio.run()` to drain the event loop after `POST /api/spintax` before asserting the final status. Use `pytest-anyio` or set `asyncio_mode = "auto"` in `pyproject.toml` (already required for Phase 2 async tests per the plan). Alternatively, use `asyncio.get_event_loop().run_until_complete()` sparingly in test helpers.

---

### 10.10 Phase 1 NITs - Phase 2 resolution

#### NIT 1: Unreachable defensive branches in `app/lint.py:351` and `app/qa.py:94`

**Decision: `# pragma: no cover` with justification.**

Rationale: These are defensive "should never happen" branches (likely `else: raise` or `except Exception` guards). Writing tests that trigger them requires either mocking stdlib or inserting artificial preconditions that break the "pure function, no I/O" invariant. `# pragma: no cover` is the correct tool here - it signals to future readers that the branch is intentionally uncovered because it protects against impossible states.

Builder adds on the specific lines:

```python
# pragma: no cover  # defensive guard, unreachable under normal input
```

Before adding pragma, builder must verify the branches ARE actually unreachable by reading the code. If they ARE reachable, write the test instead.

#### NIT 2: `raise NotImplementedError` exclusion in pyproject.toml

The current pyproject.toml excludes `raise NotImplementedError` from coverage:

```toml
[tool.coverage.report]
exclude_lines = [
    "raise NotImplementedError",
    ...
]
```

Once Phase 2 fills in `jobs.py` and `spintax_runner.py`, these exclusions become irrelevant (the bodies are no longer `raise NotImplementedError`). The exclusion in pyproject.toml stays - it does no harm and guards any future skeleton added in Phase 3+. No change needed.

#### NIT 3: `app/qa.py` CLI imports

`app/qa.py` imports `argparse`, `json`, `sys`, `pathlib` for its CLI `main()` function. These bloat the module's import surface. The `# pragma: no cover` on `main()` already excludes the CLI path from coverage.

**Decision: defer to Phase 4+.** Splitting the CLI out of `app/qa.py` requires extracting `app/qa.py` into a proper package or a `__main__.py` shim - not worth the complexity in Phase 2. The imports are stdlib-only and add ~0ms to import time.

---

### 10.11 Updated Module Dependency Graph

```
app/main.py
  imports: app/config.py, app/routes/lint.py, app/routes/qa.py,
           app/routes/spintax.py, app/routes/admin.py,
           app/dependencies.py

app/routes/spintax.py
  imports: app/jobs.py, app/spintax_runner.py, app/api_models.py,
           app/dependencies.py, app/spend.py, app/config.py

app/routes/admin.py
  imports: app/auth.py, app/api_models.py

app/routes/lint.py
  imports: app/lint.py, app/api_models.py   (unchanged from Phase 1)

app/routes/qa.py
  imports: app/qa.py, app/api_models.py     (unchanged from Phase 1)

app/dependencies.py
  imports: app/auth.py

app/spintax_runner.py
  imports: app/lint.py, app/jobs.py, app/config.py, app/spend.py, openai, httpx

app/auth.py
  imports: app/config.py   (+ stdlib: hmac, hashlib, json, base64, secrets, datetime)

app/spend.py
  imports: app/config.py

app/jobs.py
  imports: stdlib only

app/lint.py
  imports: stdlib only

app/qa.py
  imports: app/lint.py
```

**No cycles.** Dependency order (leaf to root):
1. stdlib
2. `app/lint.py`, `app/jobs.py`
3. `app/config.py`
4. `app/qa.py`, `app/spend.py`, `app/auth.py`
5. `app/spintax_runner.py`, `app/dependencies.py`, `app/api_models.py`
6. `app/routes/*.py`
7. `app/main.py`

---

### 10.12 Decision Rationale

#### `asyncio.create_task` vs `BackgroundTasks`

Chosen: `asyncio.create_task`. See 10.7 route section for full rationale. Short version: `BackgroundTasks` has request-lifetime semantics, unsuitable for 100-second jobs. `create_task` is fire-and-forget on the event loop.

#### TTL sweep on access vs background task

Chosen: sweep on access. See 10.2 for full rationale. Short version: MVP scale (20 jobs/day) makes a background sweep thread pure overhead. Sweep-on-access keeps `main.py` lifespan simple.

#### `qa fail` = `done` with `qa.passed: false`, not `failed`

Chosen: `done` with `qa.passed: false`. See T8 in state machine rationale. Short version: output IS usable, just needs review. `failed` suppresses output entirely - wrong tradeoff.

#### stdlib HMAC instead of `itsdangerous`

Chosen: stdlib HMAC. See 10.4 auth section for full rationale. Short version: `itsdangerous` is not in `fastapi[standard]`'s transitive deps. HMAC-SHA256 provides equivalent security. Zero new dependencies.

#### Spend cap key: UTC date string

Chosen: date string (`"2026-04-26"`). See 10.5 for rationale. Short version: human-readable, trivial comparison, no arithmetic.

#### Per-IP rate limiting: deferred from Phase 2

Deferred to v2. See 10.5 for rationale. Short version: shared password = team-only access, spend cap already provides the main protection, IP parsing complexity not justified at MVP scale.

---

### 10.13 Phase 2 Completion Gates

Phase 2 is done when ALL four gates pass:

**Gate 1 - Code Gate (reviewer PASS):**
- `app/jobs.py` has real implementation, all four public functions callable
- `app/spintax_runner.py.run()` has real implementation, state machine complete
- `app/routes/spintax.py` exists with POST and GET handlers
- `app/routes/admin.py` exists with POST /admin/login
- `app/dependencies.py` exists with `require_auth`
- Auth gates all `/api/*` routes
- All Phase 1 NITs addressed
- No emdashes in new code/docs
- No model name hard-coded (Rule 3 compliance)

**Gate 2 - Test Gate:**
- `pytest` exit code 0
- All 9 state machine transitions have dedicated tests
- All 7 failure modes have dedicated tests (timeout, quota, daily cap, auth missing, lint pass + QA fail, job not found, malformed response)
- Auth tests: login success, wrong password, gated 401, gated + cookie 200, expired cookie 401
- Concurrency test: 100 parallel updates, final cost correct
- TTL tests: expired evicted, fresh not evicted
- Coverage >= 85% on full `app` package
- All tests offline (<5s total, no real OpenAI calls)

**Gate 3 - UX Gate:** N/A (no UI in Phase 2)

**Gate 4 - Verification Gate:**
```bash
# Run the proof commands:
cd /Users/mihajlo/Desktop/prospeqt-spintax-web
.venv/bin/python -m pytest -v 2>&1 | tail -20
# Expect: all tests pass, >= 85% coverage

# Live smoke test (server + curl):
.venv/bin/python -m uvicorn app.main:app --port 8084 &
sleep 2
curl -s http://localhost:8084/health
# Expect: {"status":"ok"}
curl -s -X POST http://localhost:8084/api/spintax \
  -H "Content-Type: application/json" \
  -d '{"text":"test","platform":"instantly"}'
# Expect: 401 (auth gate working)
curl -s -X POST http://localhost:8084/admin/login \
  -H "Content-Type: application/json" \
  -d '{"password":"test-password"}'
# Expect: 200 + Set-Cookie header
pkill -f "uvicorn app.main:app"
```
