"""Smoke tests for CI."""

import os


def test_env_defaults_present() -> None:
    os.environ.setdefault("DISCORD_TOKEN", "test_token")
    os.environ.setdefault(
        "DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/test_db"
    )

    from src.config import settings

    assert settings.discord_token
    assert settings.async_database_url.startswith("postgresql+asyncpg://")
