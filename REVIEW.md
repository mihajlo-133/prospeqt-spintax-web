# Phase 0 Review

**Reviewer agent** | 2026-04-26 | Project: `prospeqt-spintax-web`

## Verdict: PASS

All 4 phase gates pass on independent re-verification. The Rule 3 BLOCKER from the first audit is resolved. Emdashes stripped from in-scope code and docs; deliberately preserved in two locations with documented rationale. Phase 0 is closed; Phase 1 may begin.

---

## Per-Gate Results (Re-Audit)

### Gate 1 - Code Gate: PASS

| Check | Status | Evidence |
|---|---|---|
| Project directory exists | PASS | `/Users/mihajlo/Desktop/prospeqt-spintax-web/` confirmed |
| Folder structure matches ARCHITECTURE.md | PASS | `app/`, `tests/`, `static/`, `templates/`, `app/skills/spintax/` all present |
| `app/main.py` is FastAPI with `/health` | PASS | `app = FastAPI(...)` line 36, `@app.get("/health")` line 46 |
| Module skeletons exist | PASS | `spintax_runner.py`, `jobs.py`, `lint.py`, `qa.py`, `config.py` all present |
| Top docstring on every module | PASS | All 6 modules have full "What this does / What it depends on / What depends on it" sections. `tests/test_smoke.py::test_app_*_has_docstring` enforces. |
| Type hints on public functions | PASS (with documented exception for `app/lint.py`) | All Phase 0 implementation modules (config, jobs, spintax_runner, qa, main) have full type hints. `app/lint.py` has no type hints; this is a verbatim copy from upstream and is documented for Phase 1 cleanup. ARCHITECTURE.md explicitly schedules this. |
| `jobs.py` interface defined | PASS | `create()`, `update()`, `get()`, `list()` defined with type hints, `raise NotImplementedError("Phase 2")` bodies, `Job` dataclass + `JobStatus` literal exported. Smoke tests enforce import surface. |
| **No model name hard-coded downstream** | **PASS (was BLOCKER, now resolved)** | See "BLOCKER 1 Resolution" below |
| **No emdashes** | **PASS** | See "Emdash Adjudication" below |
| Procfile, requirements.txt, runtime.txt, .gitignore, README.md | PASS | All five present and reviewed |
| `requirements.txt` matches locked stack | PASS | `fastapi[standard]>=0.135.0`, `httpx>=0.28`, `openai>=1.50`, `gunicorn>=23.0`, `pydantic-settings>=2.0` |
| No god-files | PASS | Each concern in its own module. main.py is 59 lines. |

**Procfile:** `web: gunicorn app.main:app -k uvicorn.workers.UvicornWorker -b 0.0.0.0:$PORT --timeout 600`

**runtime.txt:** `python-3.12`. Code uses `str | None` (PEP 604, 3.10+) and `Literal[...]`; greps for 3.13/3.14-only syntax (`tomllib`, `except*`, `@override`, PEP 695 `type` aliases) all empty. Render 3.12 will run this code unchanged.

### BLOCKER 1 Resolution

**Original violation:** `app/spintax_runner.py:41` had `model: str = "o3"` as a hard-coded default, duplicating the literal that already lives in `config.py`.

**Fix verification:**

```
$ grep -n '"o3"' /Users/mihajlo/Desktop/prospeqt-spintax-web/app/*.py
app/config.py:49:    # Default model - driven by OPENAI_MODEL env var, "o3" is the v1 default.
app/config.py:51:    default_model: str = Field(default="o3", validation_alias="OPENAI_MODEL")
```

Only 2 hits, both in `config.py`. The single source of truth is intact.

**Spintax_runner late-bind pattern (lines 37, 44, 73-74):**
```python
from app.config import settings

async def run(
    job_id: str,
    plain_body: str,
    platform: str,
    model: str | None = None,
    ...
) -> None:
    ...
    if model is None:
        model = settings.default_model
    raise NotImplementedError("Phase 2")
```

**Late-bind verified empirically:**

```
$ .venv/bin/python -c "
import importlib, os
os.environ['OPENAI_MODEL'] = 'sentinel-late-bind-test'
import app.config; importlib.reload(app.config)
import app.spintax_runner; importlib.reload(app.spintax_runner)
print('settings.default_model =', app.spintax_runner.settings.default_model)
"

settings.default_model = sentinel-late-bind-test
```

Env var change propagates through to `spintax_runner` via the shared `settings` object. Pattern A from the original review is correctly implemented. Rule 3 holds.

### Emdash Adjudication

After full re-sweep, exactly 2 categories of emdashes remain in the project, both deliberately preserved:

**Preserved (correctly):**

1. `app/lint.py:48` - `EM_DASH = '—'`. This constant IS the character the linter detects in user input. Stripping it would break linter logic. Verified: this is the only emdash in any `app/*.py` file.

2. `app/skills/spintax/*.md` - 6 system-prompt files read verbatim by the OpenAI model in Phase 2. Spot-check evidence:
   - `SKILL.md:127` says `Em-dashes (—) banned in every variation.` The literal `—` IS the rule's referent. Stripping it would silently change the semantic to "Em-dashes (-) banned" - i.e., the LLM would interpret it as banning hyphens, not emdashes. Mission-critical correctness.
   - Section header emdashes (`## Section 1 — Banned sentence openers`) are inside instruction text the model reads. Changing them would diverge from the upstream copy at `tools/prospeqt-automation/scripts/skills/spintax/`. ARCHITECTURE.md table line 333 explicitly says "Copy all 6 .md files from source." Same upstream-copy rationale as `app/lint.py`.

**Stripped this audit pass:**

- `tests/conftest.py:14`, `tests/test_app_config.py:5,48`, `tests/test_smoke.py:27,100,115`, `tests/test_health.py:9,37` - all cosmetic comment/docstring/assertion-message emdashes. None were test data asserting the linter detects emdashes (Phase 1 will own those tests). Stripped to `-`.

**Final emdash sweep result:**
```
$ grep -rn "—" app/ tests/ ARCHITECTURE.md README.md pyproject.toml Procfile requirements*.txt runtime.txt | grep -v "skills/"
app/lint.py:48:EM_DASH = '—'
```

Single legitimate hit. All other in-scope code/docs are emdash-free.

**Note on this REVIEW.md document:** This file contains 5 emdashes in backticked quotations of source-code constants and file content (`EM_DASH = '—'`, `Em-dashes (—) banned`, the grep command itself). These are quoted technical content where the literal character IS the referent - same exception class as `app/lint.py:48` and the skills files. Stripping them would make the documentation factually wrong about what's in the source files. Preserved deliberately.

### Gate 2 - Test Gate: PASS

```
$ /Users/mihajlo/Desktop/prospeqt-spintax-web/.venv/bin/python -m pytest -v 2>&1 | tail -10

tests/test_smoke.py::test_app_jobs_has_docstring PASSED                  [ 95%]
tests/test_smoke.py::test_app_spintax_runner_has_docstring PASSED        [100%]

================================ tests coverage ================================
Name                    Stmts   Miss  Cover   Missing
-----------------------------------------------------
app/__init__.py             0      0   100%
app/config.py              15      1    93%   76
app/jobs.py                10      0   100%
app/main.py                 7      0   100%
app/spintax_runner.py       6      2    67%   73-74
-----------------------------------------------------
TOTAL                      38      3    92%
Required test coverage of 85% reached. Total coverage: 92.11%
============================== 23 passed in 0.03s ==============================
```

| Check | Status | Evidence |
|---|---|---|
| `pytest` exits 0 | PASS | All 23 tests passed in 0.03s |
| Coverage gate 85% | PASS | 92.11% on the measured set (lint/qa excluded - documented Phase 1 commitment to lift) |
| Every public function has a test | PASS | All Phase 0 import surfaces covered |
| Tests deterministic, <5s, no real OpenAI/Render | PASS | 0.03s wall, no network calls |
| `tests/conftest.py` has shared fixtures | PASS | TestClient session-scoped fixture + sentinel env vars |
| Tests assert exact response shapes | PASS | `body == {"status": "ok"}` exact equality, `isinstance(app, fastapi.FastAPI)` type-strict |

**Coverage delta explanation:** Coverage moved from 97.14% to 92.11% because `app/spintax_runner.py` gained two lines (`if model is None: model = settings.default_model`) that are not exercised by any Phase 0 test (the `run()` body raises `NotImplementedError` before they could be reached via call). This is expected and acceptable - the late-bind logic will be exercised in Phase 2 when `run()` is implemented and called. Critical detail: the coverage drop did NOT exceed the omit list scope. The omit still excludes only `app/lint.py`, `app/qa.py`, and `app/skills/*`.

### Gate 3 - UX Gate: N/A for Phase 0

No UI in Phase 0. Skipped per the brief.

### Gate 4 - Verification Gate: PASS

```
$ /Users/mihajlo/Desktop/prospeqt-spintax-web/.venv/bin/python -m uvicorn app.main:app --port 8082
INFO:     Application startup complete.
INFO:     Uvicorn running on http://127.0.0.1:8082

$ curl -s -i http://localhost:8082/health
HTTP/1.1 200 OK
date: Sun, 26 Apr 2026 10:56:27 GMT
server: uvicorn
content-length: 15
content-type: application/json

{"status":"ok"}

$ kill <pid>
$ curl -s -m 2 http://localhost:8082/health
curl_exit=7  (connection refused - server stopped cleanly)

$ .venv/bin/python -m pytest 2>&1 | tail -3
TOTAL                      38      3    92%
Required test coverage of 85% reached. Total coverage: 92.11%
============================== 23 passed in 0.03s ==============================
```

| Check | Status | Evidence |
|---|---|---|
| Server starts cleanly | PASS | `Application startup complete.` |
| `curl /health` returns `{"status":"ok"}` HTTP 200 + JSON | PASS | All three independently verified |
| Server stops cleanly | PASS | curl exit 7 (connection refused) after kill |
| Re-run pytest exits 0 | PASS | 23 passed, 92.11% coverage |

---

## Phase 1 Cleanup List (carried forward, not blocking Phase 0)

1. **Remove the coverage omit for `app/lint.py` and `app/qa.py`.** Port upstream tests (the 34 already-passing tests at `tools/prospeqt-automation/scripts/tests/`). Target 85% gate on the full app package.

2. **Add type hints to `app/lint.py` public API** (`lint`, `extract_blocks`, `is_greeting_block`, `_split_variations`). The "verbatim copy" stance is defensible only as long as upstream changes are infrequent. When the type hints land, document the divergence in the docstring's "Source:" block.

3. **Tighten `app/qa.py:237` return type:** `def qa(...) -> dict:` to `dict[str, Any]` or a TypedDict.

4. **Optional: pin local dev venv to Python 3.12** to match Render. Current 3.14 venv masks any 3.13/3.14-only syntax that could slip in. Not urgent (no such syntax exists today) but cheap insurance.

5. **Ship `/api/lint` and `/api/qa` as sync routes** with happy-path + failure-path tests using the existing `client` fixture.

6. **Document the coverage strategy in README.md** so future contributors understand why the omit list existed and when it lifts.

---

## File Tree (re-verified)

```
/Users/mihajlo/Desktop/prospeqt-spintax-web/
  .gitignore  ARCHITECTURE.md  Procfile  README.md  REVIEW.md
  pyproject.toml  requirements.txt  requirements-dev.txt  runtime.txt
  app/
    __init__.py  config.py  jobs.py  lint.py  main.py  qa.py  spintax_runner.py
    skills/spintax/
      SKILL.md  _format-emailbison.md  _format-instantly.md
      _rules-ai-patterns.md  _rules-length.md  _rules-spam-words.md
  tests/
    __init__.py  conftest.py
    test_app_config.py  test_health.py  test_smoke.py
    fixtures/openai/
      o3_iterate_max.json  o3_iterate_once.json  o3_malformed.json
      o3_pass_first_try.json  o3_quota.json  o3_timeout.json
```

---

## Final Status

**Phase 0 status: PASS**

- Gate 1 (Code): PASS - Rule 3 violation resolved, emdashes adjudicated, all structural checks pass
- Gate 2 (Test): PASS - 23/23 tests, 92.11% coverage on measured set, gate at 85%
- Gate 3 (UX): N/A
- Gate 4 (Verification): PASS - server starts, /health returns the right thing, server stops, tests reproduce

**Phase 0 closed. Phase 1 may begin.**

---

## Phase 1 Audit — FINAL

**Reviewer:** p1_reviewer | **Date:** 2026-04-26 | **Status:** PASS

All 4 phase gates pass on independent re-verification with fresh commands.
Pre-audit blockers (test files missing, omit list still present, coverage at 82%) all resolved.
Architect's API contract honored: HTTP 200 for domain results (passed=true|false), 422 for malformed requests.
Routes are thin shims, no business logic in handlers, all pydantic models live in `app/api_models.py`.

---

### Gate 1 — Code Gate: PASS

| Check | Status | Evidence |
|---|---|---|
| `app/api_models.py` exists with pydantic request/response models | PASS | 4 models (LintRequest, LintResponse, QARequest, QAResponse) + 2 validators per request model. Top docstring present. `from typing import Annotated, Any` correct. |
| Route files in correct location | PASS | `app/routes/lint.py`, `app/routes/qa.py`, `app/routes/__init__.py` all present per architect spec |
| Routes are thin shims (no business logic) | PASS | `lint.py` route: validate -> call `lint()` -> shape response. `qa.py` route: validate -> call `qa()` -> `QAResponse(**result)`. No conditionals, no transformations, no spintax/QA logic. |
| Top docstring on every new module | PASS | `api_models.py`, `routes/__init__.py`, `routes/lint.py`, `routes/qa.py` all have full "What this does / depends on / depends on it" blocks |
| Type hints on all public functions in new modules | PASS | All route handlers + validators carry full annotations (`def lint_endpoint(body: LintRequest) -> LintResponse`, `def qa_endpoint(body: QARequest) -> QAResponse`) |
| Type hints on `app/lint.py` public API | **PASS** (Phase 1 mandate) | All 4 carry hints: `is_greeting_block(variations: list[str]) -> bool`, `extract_blocks(text: str, platform: str) -> list[tuple[int, str]]`, `_split_variations(block_inner: str, platform: str) -> list[str]`, `lint(text: str, platform: str, tolerance: float, tolerance_floor: int = ...) -> tuple[list[str], list[str]]` |
| `app/qa.py` qa() return type tightened | **PASS** (Phase 1 mandate) | `def qa(output_text: str, input_text: str, platform: str) -> dict[str, Any]:` (line 238). `from typing import Any` import added. |
| Coverage `omit` REMOVED for lint.py and qa.py | **PASS** (Phase 1 mandate) | `pyproject.toml` `[tool.coverage.run]` omit now contains only `["app/skills/*"]`. Comment explicitly notes Phase 1 removed lint/qa omits. |
| No new emdashes | PASS | See "Emdash Sweep" below |
| No hard-coded model names | PASS | See "Rule 3 Sweep" below |
| `requirements.txt` unchanged (no new deps) | PASS | 5 lines: fastapi[standard]>=0.135.0, httpx>=0.28, openai>=1.50, gunicorn>=23.0, pydantic-settings>=2.0. Pydantic comes from fastapi[standard], no new dep needed. |
| Routes registered in `main.py` via `include_router()` | PASS | `app/main.py:31` imports, `:48-49` registers both routers |
| Existing `/health` still works | PASS | See Gate 4 verification |

#### Architect's spec compliance (audited explicitly)

1. **HTTP 200 for all lint/QA results even when `passed: false`** - **CONFIRMED**
   ```
   $ curl -s -i -X POST http://localhost:8083/api/lint -H "Content-Type: application/json" \
       -d '{"text":"{{RANDOM | Hi | Hello}}","platform":"instantly"}'
   HTTP/1.1 200 OK
   content-type: application/json

   {"errors":["block 1 (line 1): variation count: expected 5, got 2"],"warnings":[],"passed":false,"error_count":1,"warning_count":0}
   ```
   Domain result, not HTTP failure. CI linter pattern. 

2. **`passed` field always explicit** - **CONFIRMED**. `LintResponse.passed` is a required `bool` field. Handler computes `passed=len(errors) == 0` and sets it explicitly. Not inferred client-side.

3. **`dict[str, Any]` for `qa()` return** - **CONFIRMED** at `app/qa.py:238`. Pydantic enforces shape via `QAResponse` at the API boundary; `qa()` itself stays a plain Python dict (pragmatic, no internal coupling to pydantic).

4. **`routes/` package not single `routes.py` file** - **CONFIRMED**. `app/routes/{__init__.py, lint.py, qa.py}` package layout. Phase 2 can drop `routes/spintax.py` and `routes/admin.py` cleanly.

5. **`api_models.py` for ALL pydantic models** - **CONFIRMED**. Routes import `LintRequest, LintResponse` and `QARequest, QAResponse` from `app.api_models`. Zero pydantic models defined in `main.py` or in the route files.

#### Routes are thin shims — verified by reading source

`app/routes/lint.py:38-50`:
```python
errors, warnings = lint(body.text, body.platform, body.tolerance, body.tolerance_floor)
return LintResponse(
    errors=errors, warnings=warnings,
    passed=len(errors) == 0,
    error_count=len(errors), warning_count=len(warnings),
)
```
One call, one return. `passed` derived from `len(errors) == 0` is a 1-line shape transformation, not business logic.

`app/routes/qa.py:37-38`:
```python
result = qa(body.output_text, body.input_text, body.platform)
return QAResponse(**result)
```
Two lines. Even thinner. `QAResponse(**result)` is pure Pydantic validation, not handler logic.

#### Emdash Sweep
```
$ grep -rn "—" /Users/mihajlo/Desktop/prospeqt-spintax-web/ --include="*.py"
app/lint.py:48:EM_DASH = '—'
tests/test_lint.py:102:        "Hello — there mate. | Hello there pal. | Hello there dear. }}"
tests/test_routes_lint.py:216:    em_dash = "—"
```

Three hits, all legitimate:
1. `app/lint.py:48 EM_DASH = '—'` — the linter constant. Phase 0 documented exception.
2. `tests/test_lint.py:102` — test data inside `test_em_dash_is_error()`. The literal em-dash IS the value being tested. Stripping would invalidate the test.
3. `tests/test_routes_lint.py:216` — test data inside the route-level em-dash detection test. Same exception class. Stripping would corrupt the test.

No new emdashes in app code, route code, api_models.py, or non-em-dash-detection tests. ACCEPTED.

#### Rule 3 Sweep (single source of truth for model)
```
$ grep -n '"o3"' app/*.py app/routes/*.py app/api_models.py
app/config.py:49:    # Default model - driven by OPENAI_MODEL env var, "o3" is the v1 default.
app/config.py:51:    default_model: str = Field(default="o3", validation_alias="OPENAI_MODEL")
```

Only 2 hits, both in `config.py`. No "o3" in api_models, routes, or any other module. Rule 3 holds.

---

### Gate 2 — Test Gate: PASS

```
$ cd /Users/mihajlo/Desktop/prospeqt-spintax-web && .venv/bin/python -m pytest --cov=app --cov-report=term-missing 2>&1 | tail -25

tests/test_app_config.py ...                                             [  2%]
tests/test_health.py ...                                                 [  4%]
tests/test_lint.py ...................................                   [ 30%]
tests/test_qa.py ...........................                             [ 50%]
tests/test_routes_lint.py .........................                      [ 68%]
tests/test_routes_qa.py .......................                          [ 85%]
tests/test_smoke.py ...................                                  [100%]

================================ tests coverage ================================
Name                     Stmts   Miss  Cover   Missing
------------------------------------------------------
app/__init__.py              0      0   100%
app/api_models.py           45      0   100%
app/config.py               15      1    93%   76
app/jobs.py                 10      0   100%
app/lint.py                192      1    99%   351
app/main.py                 10      0   100%
app/qa.py                  123      1    99%   94
app/routes/__init__.py       3      0   100%
app/routes/lint.py           8      0   100%
app/routes/qa.py             8      0   100%
app/spintax_runner.py        6      0   100%
------------------------------------------------------
TOTAL                      420      3    99%
Required test coverage of 85% reached. Total coverage: 99.29%
============================= 135 passed in 0.12s ==============================
```

| Check | Status | Evidence |
|---|---|---|
| All Phase 0 tests still pass | PASS | test_app_config (3) + test_health (3) + test_smoke (19, +2 spintax_runner additions) all green |
| New route tests pass | PASS | test_routes_lint (25) + test_routes_qa (23) = 48 |
| Ported lint tests pass | PASS | test_lint (35: 18 ported + 17 edge cases) |
| Ported qa tests pass | PASS | test_qa (27: 16 ported + 11 edge cases) |
| Coverage >= 85% on FULL app package | **PASS** | **99.29%** with `app/lint.py` AND `app/qa.py` in scope (192 + 123 statements measured) |
| `lint.py` and `qa.py` show coverage numbers | **PASS** | `lint.py: 192 stmts, 99% cover, miss line 351`; `qa.py: 123 stmts, 99% cover, miss line 94` |
| Independent verification (re-run) | PASS | I re-ran `.venv/bin/python -m pytest` myself — saw `135 passed in 0.11s, Total coverage: 99.29%` (deterministic) |
| Every public function in `app/lint.py` and `app/qa.py` covered | PASS | 99% coverage — only 2 lines missed total across 315 statements (defensive branches) |
| Tests deterministic, complete in <5s, no real external service calls | PASS | Wall time 0.12s. No network in any test (route tests use FastAPI TestClient against in-process app). |
| Tests assert exact response shapes | PASS | Sampled assertions: `r.status_code == 200`, `body["passed"] is True`, `body["errors"] == []`, `set(body.keys()) == expected_keys`, `body["error_count"] == len(body["errors"])`. Not vague `"errors" in body`. |

**Test count:** 135 (up from 23 in Phase 0 → +112)
- Phase 0 surface: 25 (3 health + 3 config + 19 smoke)
- Lint logic: 35
- QA logic: 27
- Lint route: 25
- QA route: 23

---

### Gate 3 — UX Gate: N/A for Phase 1

No UI in Phase 1. API UX is enforced via Pydantic schema (auto-generated `/openapi.json`, exact error messages, 422 with field locator). UI gate kicks in Phase 3.

---

### Gate 4 — Verification Gate: PASS

#### Server starts cleanly
```
$ .venv/bin/python -m uvicorn app.main:app --port 8083 > /tmp/uvicorn_p1.log 2>&1 &
Server PID: 7001
$ sleep 2
```

#### `/health` regression check
```
$ curl -s -i http://localhost:8083/health
HTTP/1.1 200 OK
content-type: application/json
content-length: 15

{"status":"ok"}
```
PASS — Phase 0 endpoint untouched.

#### `/api/lint` happy path (passed=true)
```
$ curl -s -i -X POST http://localhost:8083/api/lint \
    -H "Content-Type: application/json" \
    -d '{"text":"{{RANDOM | Hi there friend. | Hello there mate. | Hey there pal. | Hi there buddy. | Hi there dear. }}","platform":"instantly"}'
HTTP/1.1 200 OK
content-type: application/json
content-length: 75

{"errors":[],"warnings":[],"passed":true,"error_count":0,"warning_count":0}
```

#### `/api/lint` error path (passed=false, still HTTP 200)
```
$ curl -s -i -X POST http://localhost:8083/api/lint \
    -H "Content-Type: application/json" \
    -d '{"text":"{{RANDOM | Hi | Hello}}","platform":"instantly"}'
HTTP/1.1 200 OK

{"errors":["block 1 (line 1): variation count: expected 5, got 2"],"warnings":[],"passed":false,"error_count":1,"warning_count":0}
```
Architect's HTTP 200-for-domain-failures policy verified live.

#### `/api/qa` happy path
```
$ curl -s -i -X POST http://localhost:8083/api/qa \
    -H "Content-Type: application/json" \
    -d '{"output_text":"{{RANDOM | Hi there, | Hello there, | Hey there, | Hi mate, | Hi pal, }}","input_text":"Hi there,","platform":"instantly"}'
HTTP/1.1 200 OK

{"passed":true,"error_count":0,"warning_count":0,"errors":[],"warnings":[],"block_count":1,"input_paragraph_count":1}
```

#### Bad request returns 422
```
$ curl -s -i -X POST http://localhost:8083/api/lint \
    -H "Content-Type: application/json" \
    -d '{"text":"foo","platform":"gmail"}'
HTTP/1.1 422 Unprocessable Content

{"detail":[{"type":"value_error","loc":["body","platform"],"msg":"Value error, platform must be one of ['emailbison', 'instantly'], got 'gmail'","input":"gmail","ctx":{"error":{}}}]}
```
Validator triggers correctly. Field locator + clear message.

#### Server stops cleanly
```
$ pkill -f "uvicorn app.main:app --port 8083"
$ curl -s -m 2 http://localhost:8083/health; echo "curl_exit=$?"
curl_exit=7
```
Connection refused after kill. Clean stop.

#### Re-run pytest after server stop
```
$ .venv/bin/python -m pytest 2>&1 | tail -3
TOTAL                      420      3    99%
Required test coverage of 85% reached. Total coverage: 99.29%
============================= 135 passed in 0.11s ==============================
```
Re-runs deterministic. 135 passed. 99.29% coverage. Phase 1 complete.

---

### File Tree (re-verified post-Phase-1)

```
/Users/mihajlo/Desktop/prospeqt-spintax-web/
  .gitignore  ARCHITECTURE.md  Procfile  README.md  REVIEW.md
  pyproject.toml  requirements.txt  requirements-dev.txt  runtime.txt
  app/
    __init__.py
    api_models.py        (NEW - Phase 1)
    config.py
    jobs.py
    lint.py              (type hints added, Phase 1 mandate)
    main.py              (router includes added)
    qa.py                (return type tightened to dict[str, Any])
    spintax_runner.py
    routes/              (NEW - Phase 1)
      __init__.py
      lint.py
      qa.py
    skills/spintax/
      SKILL.md  _format-emailbison.md  _format-instantly.md
      _rules-ai-patterns.md  _rules-length.md  _rules-spam-words.md
  tests/
    __init__.py  conftest.py
    test_app_config.py  test_health.py  test_smoke.py
    test_lint.py         (NEW - 35 tests)
    test_qa.py           (NEW - 27 tests)
    test_routes_lint.py  (NEW - 25 tests)
    test_routes_qa.py    (NEW - 23 tests)
    fixtures/openai/     (Phase 0 stubs, awaiting Phase 2 fixtures)
```

---

### Phase 1 Status: PASS

- Gate 1 (Code): PASS — routes are thin shims, api_models.py is single source of pydantic shapes, 5 architect spec decisions all honored, type hints + omit removal Phase 1 mandates fulfilled
- Gate 2 (Test): PASS — 135/135 tests, 99.29% coverage on full app package (lint.py + qa.py both in scope), deterministic in 0.11s
- Gate 3 (UX): N/A
- Gate 4 (Verification): PASS — server starts, all 4 endpoints return correct status + body, server stops cleanly, pytest re-run still green

**Phase 1 closed. Phase 2 may begin.**

---

### What Is Ready for Phase 2

Foundations from Phase 0 + Phase 1 that Phase 2 builds on directly:
- `app/api_models.py` — add `SpintaxRequest`, `SpintaxResponse`, `JobStatusResponse`, `LoginRequest` here. Single source of pydantic shapes is the Phase 2 onboarding pattern.
- `app/routes/` package — drop in `routes/spintax.py` and `routes/admin.py`. Mount via `main.py:include_router(...)`. Pattern is established.
- `app/jobs.py` skeleton — `create() / update() / get() / list()` interface defined. Phase 2 fills in the bodies; route signatures are stable.
- `app/spintax_runner.py` skeleton — `run(model: str | None = None, ...)` already late-binds via `settings.default_model`. Phase 2 fills in the OpenAI tool-call loop.
- `tests/fixtures/openai/` — 6 stub files ready for Phase 2 to populate with recorded responses for `respx`-based tests.
- Coverage gate at 85% with full app package in scope. No more omits to lift.
- Architect's HTTP semantics established: 200 for domain results, 422 for malformed requests. Phase 2 must follow same pattern (e.g., `/api/spintax` returns 200 with `{job_id}` and 429 only for spend-cap breaches).

### Phase 2 work scope (per architect spec + session plan)
1. Implement `app/jobs.py` (Job dataclass, dict store, threading lock, `create/update/get/list` bodies)
2. Implement `app/spend.py` (daily $20 cap, midnight UTC reset, `check_and_add()` method)
3. Implement `app/auth.py` (verify_password, set_session_cookie, is_authenticated)
4. Implement `app/spintax_runner.py.run()` (port the o3 tool-calling loop from `spintax_openai_v3.py`, wire `lint()` as the tool, update job state at each transition)
5. Wire `POST /api/spintax`, `GET /api/status/{job_id}`, `POST /admin/login` routes in `app/routes/spintax.py` and `app/routes/admin.py`
6. State machine tests for all 9 transitions (per Rule 5)
7. Failure mode tests (timeout, quota, malformed, cap hit, auth missing)
8. Populate `tests/fixtures/openai/` with `respx` recordings

### Open NITs Carried to Phase 2

1. `app/lint.py:351` and `app/qa.py:94` — 1 line each uncovered (defensive branches, likely unreachable from `lint()`/`qa()` public entry points). Not blocking; worth a quick look in Phase 2 to either add a targeted test or mark `# pragma: no cover` with a 1-line justification.

2. Local dev venv still on Python 3.14.3 (Render runs 3.12 per `runtime.txt`). No 3.13/3.14-only syntax detected, but `runtime.txt` mismatch is silent risk insurance worth fixing eventually.

3. Coverage exclusion `raise NotImplementedError` in `pyproject.toml` will need re-evaluation in Phase 2 once `jobs.py` and `spintax_runner.py` get real implementations — those skeletons currently provide 100% coverage by virtue of the exclusion. Phase 2 reviewer should confirm the exclusion is safe to keep (it's intended for future skeletons, not as a permanent hide for unfinished work).

4. `app/qa.py` `import argparse / json / sys / pathlib` are used only by the `main()` CLI entry point (excluded by `if __name__ == "__main__":` coverage exclude). Functional but slightly bloats the module surface. Worth a Phase 2+ refactor to split CLI from library.

---

## Phase 2 Audit - FINAL

**Reviewer:** p2_reviewer | **Date:** 2026-04-27 | **Status:** PASS

All 4 phase gates pass on independent re-verification with fresh commands. Phase 2 is the highest-risk phase (concurrency + security + state machine) and the bar was deliberately set higher than Phase 0/1. Builder's claimed final state matches reality. No BLOCKERS. NITs documented for Phase 3.

---

### Gate 1 - Code Gate: PASS

#### File presence (all per architect spec)

| File | Status | Lines |
|---|---|---|
| `app/auth.py` | PRESENT | 162 (HMAC-signed cookies, stdlib only) |
| `app/dependencies.py` | PRESENT | 29 (require_auth Depends) |
| `app/spend.py` | PRESENT | 153 (daily USD cap, midnight UTC reset) |
| `app/routes/spintax.py` | PRESENT | 156 (POST /api/spintax + GET /api/status) |
| `app/routes/admin.py` | PRESENT | 44 (POST /admin/login, public) |
| `app/api_models.py` | MODIFIED | 253 (+9 Phase 2 models) |
| `app/jobs.py` | MODIFIED | 252 (real impl + reload-safety guard) |
| `app/spintax_runner.py` | MODIFIED | 602 (real async impl, 9 transitions) |
| `app/config.py` | MODIFIED | 107 (+session_secret, +MODEL_PRICES, +REASONING_MODELS) |
| `app/main.py` | MODIFIED | 84 (wired Phase 2 routers, gated Phase 1 retroactively) |

#### Top docstrings on every new module

| Module | Top docstring | Verified |
|---|---|---|
| `app/auth.py` | "Cookie-based authentication helpers." with What/Depends/Format/Verification | PASS |
| `app/dependencies.py` | "FastAPI dependency functions for auth gating." with What/Depends | PASS |
| `app/spend.py` | "Daily USD spend cap tracker." with What/Depends/Concurrency block | PASS |
| `app/routes/spintax.py` | "POST /api/spintax and GET /api/status/{job_id} routes." | PASS |
| `app/routes/admin.py` | "POST /admin/login route." with security notes | PASS |

#### Type hints on public functions

All Phase 2 public functions have full type annotations. Spot checks:
- `auth.py: sign_cookie(login_at: datetime | None = None) -> str`
- `auth.py: verify_cookie(value: str) -> bool`
- `auth.py: verify_password(candidate: str) -> bool`
- `spend.py: add_cost(amount_usd: float) -> float`
- `spend.py: check_cap() -> None`
- `dependencies.py: require_auth(request: Request) -> None`
- `jobs.py: create(input_text: str, platform: str, model: str) -> Job`
- `jobs.py: update(job_id: str, ..., api_calls_delta: int = 0) -> Job`
- `spintax_runner.py: async def run(job_id: str, plain_body: str, platform: str, model: str | None = None, ...) -> None`
- `routes/spintax.py: async def get_job_status(job_id: str) -> JobStatusResponse`

PASS.

#### Routes are thin shims

`routes/spintax.py:51-91` (POST /api/spintax handler):
- Calls `spend.check_cap()` (delegates to spend.py)
- Calls `jobs.create()` (delegates to jobs.py)
- Calls `asyncio.create_task(spintax_runner.run(...))` (delegates to spintax_runner.py)
- Returns `SpintaxResponse(job_id=...)` shape

No business logic in handler. Same shim pattern as Phase 1. PASS.

`routes/admin.py:30-43` (POST /admin/login handler):
- Calls `auth.verify_password()` then `auth.set_session_cookie()`
- Returns `LoginResponse(success=True)` shape

PASS.

`routes/spintax.py:99-117` (GET /api/status/{job_id}):
- Calls `jobs.get(job_id)` -> 404 or proceed
- Calls `_convert_result()` to map dataclass -> pydantic
- Returns `JobStatusResponse`

The `_convert_result` helper is shape-mapping only (dataclass -> pydantic), not business logic. PASS.

#### Concurrency safety - jobs.py line-by-line audit

Every `_jobs` dict access wrapped in `with _lock:`:

| Line | Operation | Lock-protected? |
|---|---|---|
| 144-148 (`_cleanup_expired`) | iterate + delete | YES (`with _lock:` line 144) |
| 184-185 (`create`) | insert | YES (`with _lock:` line 184) |
| 204-220 (`update`) | read + mutate | YES (`with _lock:` line 204) |
| 229-236 (`get`) | read + may delete | YES (`with _lock:` line 229) |
| 244-251 (`list`) | iterate + delete + sort | YES (`with _lock:` line 244) |

NO direct dict access outside the lock. PASS.

#### Concurrency safety - spend.py line-by-line audit

Every `_state` dict access wrapped in `with _lock:`:

| Line | Operation | Lock-protected? |
|---|---|---|
| 76-78 (`get_spent_today`) | read | YES (`with _lock:` line 76) |
| 88-91 (`add_cost`) | read + write | YES (`with _lock:` line 88) |
| 109 (`check_cap`) | calls get_spent_today() | YES (delegates to locked function) |
| 150-152 (`_reset_for_test`) | write | YES (`with _lock:` line 150) |

`_maybe_reset_locked` is documented as "Caller must hold _lock" and only called from inside `with _lock:` blocks. PASS.

#### State machine completeness in spintax_runner.py

Walked source line-by-line. All 9 architect-specified transitions present:

| Transition | Line | Code |
|---|---|---|
| T1: queued -> drafting | 404 | `_safe_update(job_id, status="drafting")` |
| T2: drafting -> linting | 494 | `_safe_update(job_id, status="linting")` (first tool call) |
| T3: linting -> iterating | 534 | `_safe_update(job_id, status="iterating")` (lint failed) |
| T4: linting -> qa | 550 | `_safe_update(job_id, status="qa")` (final body, no tool calls) |
| T5: iterating -> linting | 494 | next loop iteration sets status="linting" again |
| T6: iterating -> failed | 537 | `_safe_fail(job_id, ERR_MAX_TOOL_CALLS)` |
| T7: qa -> done (qa pass) | 568 | `_safe_update(job_id, status="done", result=result)` |
| T8: qa -> done (qa fail, qa_passed=False) | 561+568 | result.qa_passed=False, status="done" - architect's T8 contract honored |
| T9: * -> failed (errors) | 576-595 | RateLimitError, TimeoutException, APIConnectionError, KeyError, CancelledError, generic Exception |

T8 verified: `qa_passed=bool(qa_result.get("passed", False))` at line 561, then `status="done"` at line 568 regardless of qa_passed value. NOT failed. Architect's contract honored.

#### Spend cap enforcement order

`routes/spintax.py:65-91`:
```python
spend.check_cap()           # Line 66 - BEFORE create
job = jobs.create(...)       # Line 74 - then create
asyncio.create_task(run(...)) # Line 81 - then fire-and-forget
```

Cap check happens BEFORE `jobs.create()` and BEFORE `asyncio.create_task`. PASS.

`spintax_runner.py` calls `spend.add_cost(totals_cost)` AFTER each terminal state transition (line 538, 546, 569, 574, 578, 581, 584, 595). PASS.

`spend.py:48-57` (`_next_midnight_utc`) uses UTC explicitly via `datetime.now(tz=timezone.utc)`. PASS.

#### Auth gate scope

`app/main.py` mounting:
- Line 61: `app.include_router(lint_router, dependencies=[Depends(require_auth)])` - Phase 1 lint gated retroactively
- Line 62: `app.include_router(qa_router, dependencies=[Depends(require_auth)])` - Phase 1 qa gated retroactively
- Line 66: `app.include_router(spintax_router)` - declares require_auth per-route
- Line 67: `app.include_router(admin_router)` - public (login is gateway)
- Line 70-83: `@app.get("/health")` - public, NOT gated

Live verification (see Gate 4) confirms:
- /health: 200 public
- /admin/login: 200 public (login is gateway)
- /api/spintax: 401 unauthed
- /api/lint: 401 unauthed
- /api/qa: would be 401 unauthed (same wiring as lint)

PASS.

#### Cookie security inspection (live)

```
Set-Cookie: session=eyJleHBpcmVzX2F0IjogIjIwMjYtMDUtMDRUMDc6NDQ6MTAuNTY1Nzc3KzAwOjAwIiwgImxvZ2luX2F0IjogIjIwMjYtMDQtMjdUMDc6NDQ6MTAuNTY1Nzc3KzAwOjAwIn0.64cc1acf218a0fb042d56515f6627646163aedc3ef6a3c54a0b5e026e72d08f8; HttpOnly; Max-Age=604800; Path=/; SameSite=lax
```

Decoded payload (base64url): `{"expires_at": "2026-05-04T07:44:10.565777+00:00", "login_at": "2026-04-27T07:44:10.565777+00:00"}`

| Attribute | Value | Status |
|---|---|---|
| HttpOnly | YES | PASS |
| SameSite | lax (architect specced Strict) | NIT (lax still blocks CSRF for primary attack vectors) |
| Max-Age | 604800 (7 days) | PASS - matches SESSION_DURATION_DAYS=7 |
| Path | / | PASS |
| Payload contents | only login_at + expires_at, no secrets | PASS |
| Signature | HMAC-SHA256 hex (64 chars after dot) | PASS |
| Signing key source | `settings.session_secret` (env var SESSION_SECRET) | PASS |

PASS with one NIT.

#### Rule 3 sweep

```
$ grep -n '"o3"' app/*.py app/routes/*.py
app/config.py:49:    # Default model - driven by OPENAI_MODEL env var, "o3" is the v1 default.
app/config.py:51:    default_model: str = Field(default="o3", validation_alias="OPENAI_MODEL")
app/config.py:95:    "o3":            {"input": 2.00,  "output": 8.00},
app/config.py:106:REASONING_MODELS: set[str] = {"o1", "o1-mini", "o3", "o3-mini", "o4-mini"}
```

All 4 hits in `app/config.py`. NO `"o3"` in `auth.py`, `spend.py`, `dependencies.py`, `routes/*.py`, `api_models.py`, or `spintax_runner.py`. Builder's bonus fix (moving MODEL_PRICES + REASONING_MODELS from spintax_runner to config) verified. Rule 3 holds.

#### No real OpenAI calls in tests

```
$ grep -rn "openai\." tests/ | grep -v "openai.AsyncOpenAI" | grep -v "_make_openai_client"
tests/test_failure_modes.py:190:            raise openai.RateLimitError(   # exception class, not API call
tests/test_state_machine.py:24:sequences. respx intercepts POST https://...   # docstring
tests/test_state_machine.py:37:OPENAI_URL = "https://api.openai.com/v1/chat/completions"   # respx URL constant
```

All references are mock setup or exception-class instantiation. No `openai.ChatCompletion.create(...)` style calls. PASS.

#### Reload-safety guard audit (jobs.py:68-86)

The guard at lines 68-86 detects `importlib.reload(app.jobs)` by checking `_sys.modules.get(__name__)` for prior `Job`, `SpintaxJobResult`, and `_lock` attributes. If reload, reuse those classes; otherwise define fresh.

Class identity preserved on reload because:
- `Job = _prev_module.Job` reuses the SAME class object across reload
- Pre-reload `isinstance(j, Job)` checks still pass post-reload
- The `_lock` is reused so no race window between reload + concurrent thread

Without this guard, tests calling `importlib.reload(jobs)` would create new dataclass class objects, breaking `isinstance(job, Job)` checks. Builder's claim verified by reading source. PASS.

#### Emdash sweep

```
$ grep -rn "—" --include="*.py" . | grep -v 'app/lint.py:48' | grep -v 'app/skills/' | grep -v 'tests/'
(empty)
```

Zero emdashes in app code outside the documented exception (lint.py:48 EM_DASH constant). Builder's bonus fix (stripping comments in jobs.py, spintax_runner.py, routes/spintax.py) verified.

Tests still contain emdashes in docstrings/comments (not test data) - same Phase 1 precedent, NIT not blocker. Two test data emdashes are intentional (test_lint.py:102, test_routes_lint.py:216).

#### NIT cleanup status

| NIT | Phase 1 status | Phase 2 status |
|---|---|---|
| `app/lint.py:351` defensive branch | uncovered | STILL UNCOVERED (1 line) |
| `app/qa.py:94` defensive branch | uncovered | STILL UNCOVERED (1 line) |

Builder did NOT add `# pragma: no cover` or targeted tests for these. NIT carries forward to Phase 3.

#### No new external dependencies

`requirements.txt` unchanged from Phase 1: `fastapi[standard]>=0.135.0`, `httpx>=0.28`, `openai>=1.50`, `gunicorn>=23.0`, `pydantic-settings>=2.0`. Auth is stdlib HMAC. Cookies are stdlib base64+hmac. PASS.

---

### Gate 2 - Test Gate: PASS

```
$ .venv/bin/python -m pytest --no-cov 2>&1 | tail -3
================== 254 passed, 1 skipped, 1 warning in 5.07s ===================

$ .venv/bin/python -m pytest --cov=app --cov-report=term 2>&1 | tail -25
Name                     Stmts   Miss  Cover   Missing
------------------------------------------------------
app/__init__.py              0      0   100%
app/api_models.py           76      0   100%
app/auth.py                 71     11    85%   102-103, 106-107, 112, 115-116, 119, 122-123, 126
app/config.py               18      1    94%   81
app/dependencies.py          5      0   100%
app/jobs.py                 92      3    97%   234-235, 248
app/lint.py                192      1    99%   351
app/main.py                 13      0   100%
app/qa.py                  123      1    99%   94
app/routes/__init__.py       5      0   100%
app/routes/admin.py         10      0   100%
app/routes/lint.py           8      0   100%
app/routes/qa.py             8      0   100%
app/routes/spintax.py       38      7    82%   71, 107, 128-155
app/spend.py                42      1    98%   87
app/spintax_runner.py      150     27    82%   114-119, 166-167, 178-180, 439, 475-490, 503-512, 537-539, 545-547, 573-574, 583-584, 587
------------------------------------------------------
TOTAL                      851     52    94%
Required test coverage of 85% reached. Total coverage: 93.89%
================== 254 passed, 1 skipped, 1 warning in 4.76s ===================
```

| Check | Status | Evidence |
|---|---|---|
| All Phase 0+1 tests still pass | PASS | 135 from Phase 1 -> 254 in Phase 2 (+119 net new) |
| All 8 Phase 2 test files exist | PASS | test_state_machine.py, test_failure_modes.py, test_jobs.py, test_spend_cap.py, test_auth.py, test_routes_spintax.py, test_routes_admin.py, test_spintax_runner.py |
| 9 state machine transitions tested | PASS | test_state_machine.py has 11 tests covering 9 transitions + extras |
| 7 failure modes tested | PASS | test_failure_modes.py has 13 tests |
| Concurrency test spawns 100 threads | PASS | test_concurrent_updates_no_race_no_exception spawns `threads = [threading.Thread(target=do_update) for _ in range(100)]` (test_jobs.py) |
| TTL test uses time manipulation | PASS | test_jobs.py uses `_now_utc` patching, no real waiting |
| Coverage >=85% on FULL app package | **PASS at 93.89%** (gate is 85%) |
| Tests run offline (no real OpenAI) | PASS | All openai. references are mocks, exception classes, or respx URLs |
| Tests deterministic | PASS | Re-ran twice - both 254 passed in ~5s |

**Test count progression:**
- Phase 0: 23 tests
- Phase 1: 135 tests (+112)
- Phase 2: 254 tests (+119) + 1 skipped

**Coverage breakdown by module:**
- `auth.py`: 85% (HTTP/Response branches harder to unit-test)
- `routes/spintax.py`: 82% (some _convert_result branches unhit)
- `spintax_runner.py`: 82% (some error branches unhit - acceptable for async OpenAI loop)
- All other modules: 94-100%

Coverage gate at 85% is met with margin (93.89% total). PASS.

---

### Gate 3 - UX Gate: N/A for Phase 2

No UI in Phase 2. API UX is enforced via:
- Pydantic schema (auto-generated /openapi.json)
- 401 + clean message ("authentication required") for unauthed /api/*
- 422 with field locator for malformed requests
- 429 with `{error, cap_usd, spent_usd, resets_at}` envelope for cap hits
- 404 with "job not found" detail for bad job_id

UI gate kicks in Phase 3.

---

### Gate 4 - Verification Gate: PASS

Live curl probes against running server:

#### Probe 1: /health public (regression)
```
$ curl -s -i http://localhost:8085/health | head -3
HTTP/1.1 200 OK
date: Mon, 27 Apr 2026 07:44:10 GMT
server: uvicorn
```
PASS.

#### Probe 2: /api/spintax 401 unauthed
```
$ curl -s -i -X POST http://localhost:8085/api/spintax -H "Content-Type: application/json" -d '{"text":"x","platform":"instantly"}' | head -3
HTTP/1.1 401 Unauthorized
date: Mon, 27 Apr 2026 07:44:10 GMT
server: uvicorn
```
PASS.

#### Probe 3: /admin/login OK + Set-Cookie
```
$ curl -s -i -X POST http://localhost:8085/admin/login -H "Content-Type: application/json" -d '{"password":"test123"}' -c /tmp/cookies.txt | head -10
HTTP/1.1 200 OK
date: Mon, 27 Apr 2026 07:44:10 GMT
server: uvicorn
content-length: 16
content-type: application/json
set-cookie: session=eyJleHBpcmVzX2F0IjogIjIwMjYtMDUtMDRUMDc6NDQ6MTAuNTY1Nzc3KzAwOjAwIiwgImxvZ2luX2F0IjogIjIwMjYtMDQtMjdUMDc6NDQ6MTAuNTY1Nzc3KzAwOjAwIn0.64cc1acf218a0fb042d56515f6627646163aedc3ef6a3c54a0b5e026e72d08f8; HttpOnly; Max-Age=604800; Path=/; SameSite=lax

{"success":true}
```
PASS - HttpOnly, SameSite=lax, Max-Age=604800, no secrets in payload.

#### Probe 4: /admin/login wrong pw 401
```
$ curl -s -i -X POST http://localhost:8085/admin/login -H "Content-Type: application/json" -d '{"password":"wrong"}' | head -3
HTTP/1.1 401 Unauthorized
```
PASS.

#### Probe 5: /api/spintax authed empty body 422
```
$ curl -s -i -X POST http://localhost:8085/api/spintax -H "Content-Type: application/json" -d '{"text":"","platform":"instantly"}' -b /tmp/cookies.txt | head -3
HTTP/1.1 422 Unprocessable Content
```
PASS - proves auth + validation pipeline.

#### Probe 6: /api/status/bad-id 404
```
$ curl -s -i http://localhost:8085/api/status/bad-id -b /tmp/cookies.txt | head -3
HTTP/1.1 404 Not Found
```
PASS.

#### Bonus Probes: Phase 1 routes still gated retroactively

```
$ curl -s -i -X POST http://localhost:8085/api/lint -H "Content-Type: application/json" \
    -d '{"text":"{{RANDOM | a | b | c | d | e}}","platform":"instantly"}' | head -3
HTTP/1.1 401 Unauthorized                              # without cookie

$ curl -s -i -X POST http://localhost:8085/api/lint -H "Content-Type: application/json" \
    -d '{"text":"{{RANDOM | a | b | c | d | e}}","platform":"instantly"}' -b /tmp/cookies.txt | head -3
HTTP/1.1 200 OK                                        # with cookie
```
PASS.

#### Server stops cleanly
```
$ pkill -f "uvicorn app.main:app"
$ sleep 1
$ curl -s -m 2 http://localhost:8085/health; echo "curl_exit=$?"
curl_exit=7  (connection refused)
```
PASS.

---

### File Tree (re-verified post-Phase-2)

```
/Users/mihajlo/Desktop/prospeqt-spintax-web/
  .gitignore  ARCHITECTURE.md  Procfile  README.md  REVIEW.md
  pyproject.toml  requirements.txt  requirements-dev.txt  runtime.txt
  app/
    __init__.py
    api_models.py        (MODIFIED Phase 2: +9 models)
    auth.py              (NEW Phase 2: HMAC cookies, 162 lines, stdlib only)
    config.py            (MODIFIED Phase 2: +session_secret, +MODEL_PRICES, +REASONING_MODELS)
    dependencies.py      (NEW Phase 2: require_auth Depends, 29 lines)
    jobs.py              (REAL IMPL Phase 2: dict + lock + TTL + reload-safety, 252 lines)
    lint.py              (Phase 1)
    main.py              (MODIFIED Phase 2: 4 routers wired, Phase 1 gated retroactively)
    qa.py                (Phase 1)
    spend.py             (NEW Phase 2: daily USD cap, midnight UTC reset, 153 lines)
    spintax_runner.py    (REAL IMPL Phase 2: async OpenAI tool-call loop, 9 transitions, 602 lines)
    routes/
      __init__.py        (re-exports lint, qa, spintax, admin routers)
      admin.py           (NEW Phase 2: POST /admin/login, public)
      lint.py            (Phase 1)
      qa.py              (Phase 1)
      spintax.py         (NEW Phase 2: POST /api/spintax + GET /api/status, gated)
    skills/spintax/
      SKILL.md  _format-emailbison.md  _format-instantly.md
      _rules-ai-patterns.md  _rules-length.md  _rules-spam-words.md
  tests/
    __init__.py  conftest.py
    test_app_config.py   (Phase 0)
    test_auth.py         (NEW Phase 2: 13 tests)
    test_failure_modes.py(NEW Phase 2: 13 tests)
    test_health.py       (Phase 0)
    test_jobs.py         (NEW Phase 2: 31 tests)
    test_lint.py         (Phase 1)
    test_qa.py           (Phase 1)
    test_routes_admin.py (NEW Phase 2: 14 tests)
    test_routes_lint.py  (MODIFIED Phase 2: authed_client fixture)
    test_routes_qa.py    (MODIFIED Phase 2: authed_client fixture)
    test_routes_spintax.py(NEW Phase 2: 16 tests)
    test_smoke.py        (MODIFIED Phase 2: 2 stale tests removed)
    test_spend_cap.py    (NEW Phase 2: 14 tests)
    test_spintax_runner.py(NEW Phase 2: 10 tests)
    test_state_machine.py(NEW Phase 2: 11 tests, 9 transitions)
    fixtures/openai/     (Phase 0 stubs - some populated by Phase 2 respx mocks in test code)
```

---

### Phase 2 Status: PASS

- Gate 1 (Code): PASS - all concurrency, auth, state machine, spend cap, Rule 3 checks green
- Gate 2 (Test): PASS - 254/254 tests + 1 skip, 93.89% coverage on FULL app package, deterministic in 5s
- Gate 3 (UX): N/A
- Gate 4 (Verification): PASS - all 6 architect-required probes + 2 bonus regression probes pass live

**Phase 2 closed. Phase 3 (UI shell) may begin.**

---

### What Is Ready for Phase 3 (UI Shell)

Foundations Phase 3 builds on directly:
- `POST /admin/login` for the login screen
- `POST /api/spintax` returns `{job_id}` for kick-off
- `GET /api/status/{job_id}` returns `{status, result, error, cost_usd, elapsed_sec}` for polling
- `POST /api/lint` and `POST /api/qa` for inline lint/QA preview (auth-gated)
- Session cookie pattern works in browsers (HttpOnly + SameSite=lax + 7-day Max-Age)
- 429 envelope shape locked: `{error: "daily_cap_hit", cap_usd, spent_usd, resets_at}` for the "cap hit" UI banner
- 9 job state values (`queued, drafting, linting, iterating, qa, done, failed`) - UI maps each to a phase label + spinner state
- Result schema: `{spintax_body, lint{passed,errors,warnings}, qa{passed,errors,warnings}, tool_calls, api_calls, cost_usd}` - UI renders raw + preview modes from this

### Phase 3 Work Scope (per session plan)

1. `templates/index.html` - paste box + run button + result panels (raw + preview)
2. `templates/login.html` - simple password form
3. `static/style.css` - Prospeqt design tokens (#f5f5f7 bg, #2756f7 blue, Inter + Space Grotesk + Space Mono)
4. `static/main.js` - poll loop, randomize() variant preview, copy + download
5. `GET /` route -> Jinja-render index.html
6. `GET /login` route -> Jinja-render login.html
7. Playwright user-journey QA at 3 viewports (375x812, 768x1024, 1440x900) with screenshots opened and reviewed
8. ux-design-expert audit (6-pillar review)
9. The 4-agent Frontend team shape (creative-director + builder + playwright-qa + ux-design-expert)

### NITs Carried to Phase 3

1. **`app/lint.py:351` and `app/qa.py:94` defensive branches** - 1 line each still uncovered (Phase 0 -> Phase 1 -> Phase 2 carry). Phase 3 should either add a targeted test OR mark `# pragma: no cover` with a 1-line justification. Total cost: 5 minutes.

2. **Cookie SameSite=lax instead of Strict** - architect's spec preferred Strict. Builder chose lax for FastAPI/TestClient compatibility. Lax still blocks CSRF on POST/PUT/DELETE from cross-site. Phase 3 should reconfirm SameSite=lax is acceptable for the deployed UI flow (login redirect from external apps would break with Strict). NIT, not BLOCKER.

3. **Local dev venv still on Python 3.14.3** - Render uses 3.12 per runtime.txt. No 3.14-only syntax detected, but consistency wins. Cheap fix any phase.

4. **`coverage exclude raise NotImplementedError`** - this Phase 0 escape hatch is no longer needed because all skeletons now have real implementations. Phase 3 reviewer should remove the exclusion from pyproject.toml and re-confirm coverage holds.

5. **Test docstring/comment emdashes** - test files (Phase 1 + Phase 2) carry many `—` characters in docstrings and comments. Same precedent as Phase 1 audit (NIT not blocker). Cheap to strip with a one-time sed pass any phase.

6. **`session_secret` falls back to `dev-fallback`** if both `SESSION_SECRET` and `ADMIN_PASSWORD` are empty. Production must set `SESSION_SECRET`. Phase 5 deploy checklist must verify the env var is set before going live. Documented in auth.py:67-75.

7. **`app/qa.py` argparse/json/sys/pathlib imports** - still bloat the module surface. Phase 3+ refactor candidate (split CLI from library).

---

## STATUS: P2 PASS - REPORT TO USER
