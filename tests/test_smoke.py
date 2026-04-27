"""Smoke tests: verify app package and all Phase 0 modules import cleanly.

These tests must fail with ImportError / ModuleNotFoundError until the builder
creates the scaffold. Once the scaffold exists, they must all pass with zero
warnings.

Rule 3 (Clean Code): every module has a top docstring. We assert __doc__ is
non-empty here so the reviewer can enforce the rule automatically.

Architecture alignment: covers the 6 tests listed in ARCHITECTURE.md Section 7.
"""

import importlib


# ---------------------------------------------------------------------------
# App package import
# ---------------------------------------------------------------------------

def test_app_package_imports():
    """app/__init__.py must exist and import without error."""
    mod = importlib.import_module("app")
    assert mod is not None


# ---------------------------------------------------------------------------
# Module imports - each module in Phase 0 scaffold
# ---------------------------------------------------------------------------

def test_app_main_imports():
    """app/main.py must import cleanly."""
    mod = importlib.import_module("app.main")
    assert mod is not None


def test_app_lint_imports():
    """app/lint.py must import cleanly (copied/adapted from spintax_lint.py)."""
    mod = importlib.import_module("app.lint")
    assert mod is not None


def test_app_qa_imports():
    """app/qa.py must import cleanly (copied/adapted from qa_spintax.py)."""
    mod = importlib.import_module("app.qa")
    assert mod is not None


def test_app_jobs_imports():
    """app/jobs.py must import cleanly."""
    mod = importlib.import_module("app.jobs")
    assert mod is not None


def test_app_spintax_runner_imports():
    """app/spintax_runner.py must import cleanly."""
    mod = importlib.import_module("app.spintax_runner")
    assert mod is not None


# ---------------------------------------------------------------------------
# Specific function imports (from ARCHITECTURE.md Section 7)
# ---------------------------------------------------------------------------

def test_import_lint_public_functions():
    """from app.lint import lint, extract_blocks must succeed (full copy, not skeleton)."""
    from app.lint import lint, extract_blocks  # noqa: F401
    assert callable(lint)
    assert callable(extract_blocks)


def test_import_qa_public_function():
    """from app.qa import qa must succeed (full copy, not skeleton)."""
    from app.qa import qa  # noqa: F401
    assert callable(qa)


def test_import_jobs_public_functions():
    """from app.jobs import create, update, get must succeed (skeletons are importable)."""
    from app.jobs import create, update, get  # noqa: F401
    assert callable(create)
    assert callable(update)
    assert callable(get)


def test_import_spintax_runner_run():
    """from app.spintax_runner import run must succeed."""
    from app.spintax_runner import run  # noqa: F401
    assert callable(run)


# ---------------------------------------------------------------------------
# FastAPI auto-generated routes (from ARCHITECTURE.md Section 7)
# ---------------------------------------------------------------------------

def test_app_has_openapi(client):
    """GET /openapi.json must return 200 (FastAPI auto-generates this)."""
    response = client.get("/openapi.json")
    assert response.status_code == 200, (
        f"Expected 200, got {response.status_code}. "
        "FastAPI generates /openapi.json automatically - if this fails, "
        "the app object is not a proper FastAPI instance."
    )


def test_app_has_swagger_docs(client):
    """GET /docs must return 200 (FastAPI auto-generates Swagger UI)."""
    response = client.get("/docs")
    assert response.status_code == 200, (
        f"Expected 200, got {response.status_code}. "
        "FastAPI generates /docs automatically."
    )


# ---------------------------------------------------------------------------
# Top docstrings - Rule 3: every module has a module-level docstring
# ---------------------------------------------------------------------------

def test_app_main_has_docstring():
    """app/main.py must have a non-empty module docstring."""
    mod = importlib.import_module("app.main")
    assert mod.__doc__ and mod.__doc__.strip(), (
        "app/main.py is missing a module docstring. "
        "Rule 3: every module documents what it does, what it depends on, and what depends on it."
    )


def test_app_lint_has_docstring():
    """app/lint.py must have a non-empty module docstring."""
    mod = importlib.import_module("app.lint")
    assert mod.__doc__ and mod.__doc__.strip(), (
        "app/lint.py is missing a module docstring."
    )


def test_app_qa_has_docstring():
    """app/qa.py must have a non-empty module docstring."""
    mod = importlib.import_module("app.qa")
    assert mod.__doc__ and mod.__doc__.strip(), (
        "app/qa.py is missing a module docstring."
    )


def test_app_jobs_has_docstring():
    """app/jobs.py must have a non-empty module docstring."""
    mod = importlib.import_module("app.jobs")
    assert mod.__doc__ and mod.__doc__.strip(), (
        "app/jobs.py is missing a module docstring."
    )


def test_app_spintax_runner_has_docstring():
    """app/spintax_runner.py must have a non-empty module docstring."""
    mod = importlib.import_module("app.spintax_runner")
    assert mod.__doc__ and mod.__doc__.strip(), (
        "app/spintax_runner.py is missing a module docstring."
    )


# ---------------------------------------------------------------------------
# Phase 0 skeleton tests removed in Phase 2.
#
# The original test_spintax_runner_run_raises_not_implemented and
# test_spintax_runner_run_resolves_default_model asserted the Phase 0
# skeleton bodies. Phase 2 ships the real implementation, so those tests
# would always fail. Runner coverage is now provided by:
#   - tests/test_spintax_runner.py
#   - tests/test_state_machine.py
#   - tests/test_failure_modes.py
# ---------------------------------------------------------------------------
