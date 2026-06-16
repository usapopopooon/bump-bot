"""Discord bot class for bump feature."""

import logging

import discord
from discord.ext import commands

from src.database.engine import init_db

logger = logging.getLogger(__name__)


class BumpBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.guilds = True
        # Privileged Intent を必須にしない。
        # (Portal 側で未有効でも起動を継続できるようにする)
        intents.members = True
        # bump 検知では bot メッセージ本文を参照するため有効化する。
        intents.message_content = True
        super().__init__(command_prefix="!", intents=intents)

    async def setup_hook(self) -> None:
        await init_db()
        await self.load_extension("src.cogs.bump")
        synced = await self.tree.sync()
        logger.info("Synced %d slash commands", len(synced))
