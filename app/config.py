"""Central configuration, loaded from environment variables.

All values are read once at import time. Nothing here is secret in itself, but
the DISCORD_TOKEN / DISCORD_WEBHOOK_URL values ARE secrets and must only ever be
supplied through environment variables (never hard-coded, never logged, and
never exposed to executed user code).

For local development a `.env` file is loaded automatically if `python-dotenv`
is installed. In production (Render) the environment variables are injected by
the platform, so python-dotenv is optional.
"""

from __future__ import annotations

import os

# Optional local .env support. Never required in production.
try:  # pragma: no cover - trivial optional import
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover
    pass


def _get_str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip()


def _get_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on", "y"}


class Settings:
    """Immutable-ish view over environment configuration."""

    # --- Secrets (never log these) ---
    DISCORD_TOKEN: str = _get_str("DISCORD_TOKEN")
    DISCORD_WEBHOOK_URL: str = _get_str("DISCORD_WEBHOOK_URL")
    GROQ_API_KEY: str = _get_str("GROQ_API_KEY")

    # --- Groq ---
    GROQ_MODEL: str = _get_str("GROQ_MODEL", "llama3-8b-8192")

    # --- Web server (Render provides PORT) ---
    PORT: int = _get_int("PORT", 10000)
    HOST: str = _get_str("HOST", "0.0.0.0")

    # --- Detection behavior ---
    # When True (default) only explicit ```python / ```py blocks are executed.
    # When False, unmarked ``` ``` fenced blocks are also treated as Python.
    REQUIRE_PYTHON_CODE_BLOCK: bool = _get_bool("REQUIRE_PYTHON_CODE_BLOCK", True)
    # When True, and a message has several Python blocks, all are joined and run.
    # Default False -> only the FIRST Python block runs (blocks are never
    # concatenated automatically).
    EXECUTE_ALL_BLOCKS: bool = _get_bool("EXECUTE_ALL_BLOCKS", False)

    # --- Limits ---
    MAX_CODE_LENGTH: int = _get_int("MAX_CODE_LENGTH", 5000)
    MAX_EXECUTION_TIME: int = _get_int("MAX_EXECUTION_TIME", 5)
    MAX_OUTPUT_LENGTH: int = _get_int("MAX_OUTPUT_LENGTH", 8000)
    # Virtual-memory ceiling for each execution subprocess (defense against
    # runaway allocations). Generous enough for the interpreter to start.
    MAX_MEMORY_MB: int = _get_int("MAX_MEMORY_MB", 256)

    # --- Concurrency ---
    MAX_CONCURRENT_EXECUTIONS: int = _get_int("MAX_CONCURRENT_EXECUTIONS", 1)
    # Tiny wait-queue. 0 => reject immediately when busy.
    MAX_QUEUE_SIZE: int = _get_int("MAX_QUEUE_SIZE", 2)

    # --- Rate limiting ---
    MAX_EXECUTIONS_PER_USER_PER_MINUTE: int = _get_int(
        "MAX_EXECUTIONS_PER_USER_PER_MINUTE", 5
    )
    MAX_GLOBAL_EXECUTIONS_PER_MINUTE: int = _get_int(
        "MAX_GLOBAL_EXECUTIONS_PER_MINUTE", 15
    )
    RATE_LIMIT_WINDOW_SECONDS: int = _get_int("RATE_LIMIT_WINDOW_SECONDS", 60)

    # --- Output delivery ---
    # One of: "bot", "webhook", "both".
    OUTPUT_MODE: str = _get_str("OUTPUT_MODE", "bot").lower() or "bot"

    # --- Logging ---
    LOG_LEVEL: str = _get_str("LOG_LEVEL", "INFO").upper() or "INFO"

    @classmethod
    def allow_unmarked_blocks(cls) -> bool:
        """Whether bare ``` ``` blocks (no language) should be executed."""
        return not cls.REQUIRE_PYTHON_CODE_BLOCK

    @classmethod
    def validate(cls) -> list[str]:
        """Return a list of human-readable configuration problems (may be empty)."""
        problems: list[str] = []
        if not cls.DISCORD_TOKEN:
            problems.append("DISCORD_TOKEN is not set.")
        if cls.OUTPUT_MODE not in {"bot", "webhook", "both"}:
            problems.append(
                f"OUTPUT_MODE must be one of bot/webhook/both, got {cls.OUTPUT_MODE!r}."
            )
        if cls.OUTPUT_MODE in {"webhook", "both"} and not cls.DISCORD_WEBHOOK_URL:
            problems.append(
                "OUTPUT_MODE requires a webhook but DISCORD_WEBHOOK_URL is not set."
            )
        if cls.MAX_CONCURRENT_EXECUTIONS < 1:
            problems.append("MAX_CONCURRENT_EXECUTIONS must be >= 1.")
        if cls.MAX_EXECUTION_TIME < 1:
            problems.append("MAX_EXECUTION_TIME must be >= 1.")
        return problems


settings = Settings()
