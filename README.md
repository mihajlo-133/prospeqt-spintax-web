# Prospeqt Spintax Web

Web service that wraps the Prospeqt spintax tooling. Paste plain email copy
in, get spintax-formatted output back. Runs the deterministic linter and
the OpenAI reasoning-model generator behind a FastAPI surface.

Deployed to Render Frankfurt. Repo: `mihajlo-133/prospeqt-spintax-web`.

## API

This service exposes a JSON HTTP API for converting plain email copy into spintax, batch-spinning whole markdown sequence files, and running standalone lint / QA checks.

**Base URL (production):** https://prospeqt-spintax.onrender.com

### Doc surfaces

| Surface | Path | Audience |
|---|---|---|
| HTML reference | [`/docs`](https://prospeqt-spintax.onrender.com/docs) | Humans onboarding to the API |
| LLM-optimized markdown | [`/llms.txt`](https://prospeqt-spintax.onrender.com/llms.txt) | AI agents (Claude Code, GPT-based agents) |
| OpenAPI 3.1 spec | [`/openapi.json`](https://prospeqt-spintax.onrender.com/openapi.json) | Auto-generated clients, OpenAPI tooling |

All three doc surfaces are PUBLIC (no auth). The actual `/api/*` endpoints require a bearer token.

### Authentication

Set `Authorization: Bearer <BATCH_API_KEY>` on every `/api/*` request. The token is stored in ClickUp - ask Mihajlo in chat for the current value.

### Endpoints at a glance

| Method | Path | Purpose |
|---|---|---|
| POST | `/api/spintax` | Convert one plain email body to spintax. Async; returns `job_id`. |
| GET | `/api/status/{job_id}` | Poll a single job. |
| POST | `/api/spintax/batch` | Spin a whole markdown sequence file. Async; returns `batch_id`. |
| GET | `/api/spintax/batch/{batch_id}` | Poll a batch. |
| POST | `/api/spintax/batch/{batch_id}/cancel` | Cancel a running batch. |
| GET | `/api/spintax/batch/{batch_id}/download` | Download the result zip when done. |
| POST | `/api/lint` | Deterministic lint on already-spun copy. Sync. |
| POST | `/api/qa` | Deterministic QA against the original plain input. Sync. |

For full request/response shapes, error codes, polling guidance, and worked examples see [`/docs`](https://prospeqt-spintax.onrender.com/docs) or [`/llms.txt`](https://prospeqt-spintax.onrender.com/llms.txt).

### How to onboard a teammate (or an AI agent)

1. Send them this README (or just the link to the deployed service).
2. For AI agents: point the agent at `https://prospeqt-spintax.onrender.com/llms.txt` - that page is self-contained and explains every endpoint, error code, the drift-revision loop, the model selection rationale, and recommended retry behavior.
3. Ask Mihajlo in chat for the API key. Store it as `BATCH_API_KEY` in their environment.
4. First call should be a tiny single body or a `dry_run: true` batch to confirm auth and connectivity before running real work.
5. The daily spend cap is $50 across all callers - coordinate before kicking off large batches.

## Local development

```bash
# 1. Create the virtualenv (already present at .venv/ on dev machines)
python3.12 -m venv .venv
source .venv/bin/activate

# 2. Install runtime + dev dependencies
pip install -r requirements-dev.txt

# 3. Run tests
pytest -v

# 4. Start the dev server
uvicorn app.main:app --reload --port 8080

# 5. Hit the health endpoint
curl http://localhost:8080/health
# -> {"status":"ok"}
```

## Layout

```
app/
  __init__.py              # package marker
  main.py                  # FastAPI app + GET /health
  config.py                # pydantic-settings (env-var driven)
  lint.py                  # deterministic spintax linter (full copy)
  qa.py                    # QA checks (full copy, imports app.lint)
  jobs.py                  # in-memory job store interface (Phase 2)
  spintax_runner.py        # OpenAI tool-call loop (Phase 2)
  skills/spintax/          # system-prompt markdown source
tests/                     # pytest suite (run with --cov=app)
Procfile                   # gunicorn + uvicorn worker for Render
requirements.txt           # runtime deps
requirements-dev.txt       # adds pytest, respx, ruff
runtime.txt                # python-3.12
```

## Configuration

All configuration is environment variables (or a local `.env` file). Defaults
live in `app/config.py`:

| Variable | Default | Notes |
|---|---|---|
| `OPENAI_MODEL` | `o3` | Default model. Swap to `o4-mini`, `gpt-4.1`, etc. |
| `OPENAI_API_KEY` | (empty) | Required in Phase 2. |
| `ADMIN_PASSWORD` | (empty) | Required in Phase 2 for admin routes. |
| `DAILY_SPEND_CAP_USD` | `20.0` | Daily OpenAI spend cap. |
| `DEFAULT_PLATFORM` | `instantly` | `instantly` or `emailbison`. |
| `PORT` | `8000` | Render sets this automatically. |

