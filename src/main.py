"""Entrypoint for bump bot."""

import asyncio
import logging
import os

from src.bot import BumpBot
from src.config import settings
from src.database.engine import check_database_connection_with_retry


def _setup_logging() -> None:
    level_name = os.environ.get("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )


async def main() -> None:
    _setup_logging()
    if not await check_database_connection_with_retry():
        raise RuntimeError("Database connection failed")

    bot = BumpBot()
    async with bot:
        await bot.start(settings.discord_token)


if __name__ == "__main__":
    asyncio.run(main())
