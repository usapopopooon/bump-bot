from __future__ import annotations

import os
from unittest.mock import MagicMock

from discord.ext import commands

os.environ.setdefault("DISCORD_TOKEN", "test_token")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/test_db"
)

from src.cogs.bump import (
    DISBOARD_BOT_ID,
    DISBOARD_SUCCESS_KEYWORD,
    DISSOKU_BOT_ID,
    DISSOKU_SUCCESS_KEYWORD,
    BumpCog,
)


def _make_message(author_id: int, description: str = "", title: str = "") -> MagicMock:
    msg = MagicMock()
    msg.author = MagicMock()
    msg.author.id = author_id
    embed = MagicMock()
    embed.description = description
    embed.title = title
    embed.fields = []
    msg.embeds = [embed]
    msg.content = ""
    return msg


def test_detect_disboard_success() -> None:
    bot = MagicMock(spec=commands.Bot)
    cog = BumpCog(bot)
    msg = _make_message(
        DISBOARD_BOT_ID,
        description=f"サーバーの{DISBOARD_SUCCESS_KEYWORD}しました",
    )
    assert cog._detect_bump_success(msg) == "DISBOARD"


def test_detect_dissoku_success() -> None:
    bot = MagicMock(spec=commands.Bot)
    cog = BumpCog(bot)
    msg = _make_message(
        DISSOKU_BOT_ID,
        title=f"サーバーを{DISSOKU_SUCCESS_KEYWORD}したよ",
    )
    assert cog._detect_bump_success(msg) == "ディス速報"
