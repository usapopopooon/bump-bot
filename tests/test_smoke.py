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


def test_database_url_normalizes_coolify_quoted_value() -> None:
    from src.config import Settings

    settings = Settings(
        discord_token="test_token",
        database_url=' "postgres://user:pass@localhost/test_db" ',
    )

    assert (
        settings.async_database_url
        == "postgresql+asyncpg://user:pass@localhost/test_db"
    )


def test_database_url_rejects_unknown_scheme() -> None:
    from src.config import Settings

    settings = Settings(discord_token="test_token", database_url="not-a-url")

    try:
        _ = settings.async_database_url
    except ValueError as exc:
        assert "DATABASE_URL must start with" in str(exc)
    else:
        raise AssertionError("Expected invalid DATABASE_URL to raise ValueError")
