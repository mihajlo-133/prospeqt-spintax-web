"""Configuration via pydantic-settings.

What this does:
    Reads environment variables into a typed Settings object. Provides a
    single source of truth for configuration values used across the app.

What it depends on:
    - pydantic-settings (external dependency)
    - environment variables (or a local .env file in development)

What depends on it:
    - app/main.py reads `settings.default_model` to expose DEFAULT_MODEL
    - app/spintax_runner.py (Phase 2) will read settings.openai_api_key
    - app/spend.py (Phase 2) will read settings.daily_spend_cap_usd
    - app/auth.py (Phase 2) will read settings.admin_password

Rule 3 compliance:
    Model and platform are parameters, never hard-coded literals downstream.
    The default value lives ONLY here. Swapping models is an env var change.
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings sourced from environment variables.

    All fields have safe defaults so the app can boot in test environments
    without a .env file. Production sets these via Render's env var dashboard.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    # Auth (Phase 2 use, defined now so config is stable across phases)
    admin_password: str = Field(default="", validation_alias="ADMIN_PASSWORD")

    # OpenAI (Phase 2 use)
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")

    # Default model - driven by OPENAI_MODEL env var, "o3" is the v1 default.
    # Rule 3: this is the ONLY place the default model literal appears.
    default_model: str = Field(default="o3", validation_alias="OPENAI_MODEL")

    # Default platform for spintax generation when not specified by caller.
    default_platform: str = Field(default="instantly", validation_alias="DEFAULT_PLATFORM")

    # Daily USD cap for OpenAI spend.
    # Bumped to 50 in Phase 4 (batch API) to fit a full HeyReach run (~$35).
    daily_spend_cap_usd: float = Field(default=50.0, validation_alias="DAILY_SPEND_CAP_USD")

    # Session cookie HMAC signing key (Phase 2 use).
    # Empty default permits local dev / tests to boot without setting it.
    # Production must set a >=32-byte secret via the Render env dashboard.
    session_secret: str = Field(default="", validation_alias="SESSION_SECRET")

    # Bearer token for headless API access (Claude Code / curl). When set,
    # any request bearing `Authorization: Bearer <this value>` bypasses the
    # session-cookie check on /api/* routes. Empty disables bearer auth
    # (only session cookies work, e.g., local dev).
    batch_api_key: str = Field(default="", validation_alias="BATCH_API_KEY")

    # HTTP port - Render sets PORT, local dev defaults to 8000.
    port: int = Field(default=8000, validation_alias="PORT")


# Module-level singleton, instantiated at import time so importlib.reload()
# re-reads the current environment. Tests rely on this behavior.
settings: Settings = Settings()


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the cached Settings instance.

    Cached for performance - Settings parsing is cheap but called from
    request handlers. If you need fresh values mid-process (e.g., tests
    that patch env vars), call importlib.reload(app.config) instead.
    """
    return settings


# ---------------------------------------------------------------------------
# Model capability tables - single source of truth.
#
# These live here (not in spintax_runner) so Rule 3's "model literals only
# in config.py" check is satisfied. The runner imports them as plain
# dictionaries and looks up the active model by name at runtime.
# ---------------------------------------------------------------------------

# USD price per 1M tokens, by model name. Ported verbatim from
# tools/prospeqt-automation/scripts/spintax_openai_v3.py.
MODEL_PRICES: dict[str, dict[str, float]] = {
    "o3":            {"input": 2.00,  "output": 8.00},
    "o3-mini":       {"input": 1.10,  "output": 4.40},
    "o3-pro":        {"input": 20.00, "output": 80.00},
    "o4-mini":       {"input": 1.10,  "output": 4.40},
    "o1":            {"input": 15.00, "output": 60.00},
    "o1-mini":       {"input": 1.10,  "output": 4.40},
    "gpt-4.1":       {"input": 2.00,  "output": 8.00},
    "gpt-4.1-mini":  {"input": 0.40,  "output": 1.60},
}

# Set of OpenAI reasoning models. The runner passes 'reasoning_effort' to
# these and 'temperature' to all others.
REASONING_MODELS: set[str] = {"o1", "o1-mini", "o3", "o3-mini", "o3-pro", "o4-mini"}
