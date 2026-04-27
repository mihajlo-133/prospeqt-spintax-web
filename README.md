# Prospeqt Spintax Web

Web service that wraps the Prospeqt spintax tooling. Paste plain email copy
in, get spintax-formatted output back. Runs the deterministic linter and
the OpenAI reasoning-model generator behind a FastAPI surface.

Deployed to Render Frankfurt. Repo: `mihajlo-133/prospeqt-spintax-web`.

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

