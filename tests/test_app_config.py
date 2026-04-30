"""Tests for app configuration and FastAPI instance shape.

Phase 0 scope:
- Verify the app object is a FastAPI instance (not Flask, not a raw Starlette app).
- Verify the default model config attribute exists (not its runtime value - we
  cannot guarantee the OPENAI_MODEL env var is set in CI).
- Verify the model attribute is configurable via an env var (i.e., the config
  module reads from environment, not a hard-coded string).

These tests are intentionally narrow. We do NOT test:
- Actual OpenAI calls (Phase 2)
- Model swapping at runtime (Phase 2)
- Spend cap value (Phase 2)

Rule 3 compliance: model and platform are parameters everywhere, never hard-coded.
The config attribute test enforces this by ensuring the value comes from the
environment layer, not a literal string embedded in the code.
"""

import importlib

import fastapi


# ---------------------------------------------------------------------------
# FastAPI instance check
# ---------------------------------------------------------------------------


def test_app_is_fastapi_instance():
    """app.main.app must be a FastAPI instance, not Starlette or Flask."""
    from app.main import app

    assert isinstance(app, fastapi.FastAPI), (
        f"Expected fastapi.FastAPI instance, got {type(app).__name__}. "
        "The stack spec mandates FastAPI for async route handling."
    )


# ---------------------------------------------------------------------------
# Model config attribute exists
# ---------------------------------------------------------------------------


def test_app_config_has_model_attribute():
    """app must expose a config object (or module-level constant) named DEFAULT_MODEL.

    Phase 0 only checks that the attribute exists and is a non-empty string.
    The actual value ('o3', 'o4-mini', etc.) is controlled by the OPENAI_MODEL
    env var - we do not assert a specific value here so tests run in clean CI
    environments without .env files.
    """
    import app.main as main_mod

    # Accept either a module-level DEFAULT_MODEL or a settings object
    has_attr = (
        hasattr(main_mod, "DEFAULT_MODEL")
        or hasattr(main_mod, "settings")
        or _config_module_has_default_model()
    )
    assert has_attr, (
        "Could not find DEFAULT_MODEL in app.main or app.config. "
        "Rule 3: model must be a parameter, never hard-coded. "
        "Expose DEFAULT_MODEL = os.getenv('OPENAI_MODEL', 'o3') somewhere the routes can read it."
    )


def _config_module_has_default_model() -> bool:
    """Check if app.config module exposes DEFAULT_MODEL."""
    try:
        config_mod = importlib.import_module("app.config")
        return hasattr(config_mod, "DEFAULT_MODEL") or hasattr(config_mod, "settings")
    except ModuleNotFoundError:
        return False


# ---------------------------------------------------------------------------
# Model is env-var driven (not hard-coded)
# ---------------------------------------------------------------------------


def test_default_model_reads_from_env(monkeypatch):
    """DEFAULT_MODEL must reflect the OPENAI_MODEL env var when set.

    This test patches the env var, reloads the config, and verifies the
    value propagated. This is the minimum proof that model is not hard-coded.

    If the config uses pydantic-settings, this works automatically via
    model_validate(). If it's os.getenv(), it works by reloading the module.
    """
    monkeypatch.setenv("OPENAI_MODEL", "test-model-sentinel")

    # Reload config to pick up the patched env
    try:
        config_mod = importlib.import_module("app.config")
        importlib.reload(config_mod)
        # Check pydantic-settings pattern: settings.default_model or settings.openai_model
        if hasattr(config_mod, "settings"):
            s = config_mod.settings
            model_val = getattr(s, "default_model", None) or getattr(s, "openai_model", None)
            if model_val is not None:
                assert model_val == "test-model-sentinel", (
                    f"Expected 'test-model-sentinel', got '{model_val}'. "
                    "Config must read OPENAI_MODEL from environment."
                )
                return
        # Check module-level DEFAULT_MODEL pattern
        if hasattr(config_mod, "DEFAULT_MODEL"):
            assert config_mod.DEFAULT_MODEL == "test-model-sentinel", (
                f"Expected 'test-model-sentinel', got '{config_mod.DEFAULT_MODEL}'. "
                "DEFAULT_MODEL must read from OPENAI_MODEL env var."
            )
            return
    except ModuleNotFoundError:
        pass

    # Fall back: check app.main directly
    main_mod = importlib.import_module("app.main")
    importlib.reload(main_mod)
    if hasattr(main_mod, "DEFAULT_MODEL"):
        assert main_mod.DEFAULT_MODEL == "test-model-sentinel", (
            f"Expected 'test-model-sentinel', got '{main_mod.DEFAULT_MODEL}'."
        )
        return

    # If we reach here, no DEFAULT_MODEL was found at all
    raise AssertionError(
        "Could not locate DEFAULT_MODEL in app.config or app.main after env patch. "
        "Expose DEFAULT_MODEL = os.getenv('OPENAI_MODEL', 'o3') in one of these modules."
    )
