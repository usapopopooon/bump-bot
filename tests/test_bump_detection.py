from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from unittest.mock import MagicMock

import discord
from discord.ext import commands

os.environ.setdefault("DISCORD_TOKEN", "test_token")
os.environ.setdefault(
    "DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/test_db"
)

from src.cogs.bump import (
    BUMP_SERVICES,
    DISBOARD_BOT_ID,
    DISBOARD_SUCCESS_KEYWORD,
    DISSOKU_BOT_ID,
    DISSOKU_SUCCESS_KEYWORD,
    TARGET_ROLE_NAME,
    BumpCog,
    _get_reminder_hours,
)
from src.database.models import BumpReminder


def _make_cog() -> BumpCog:
    bot = MagicMock(spec=commands.Bot)
    bot.wait_until_ready = MagicMock()
    bot.get_channel = MagicMock(return_value=None)
    bot.add_view = MagicMock()
    return BumpCog(bot)


def _make_message(
    *,
    author_id: int,
    embed_description: str | None = None,
    embed_title: str | None = None,
    content: str | None = None,
) -> MagicMock:
    message = MagicMock(spec=discord.Message)
    message.author = MagicMock()
    message.author.id = author_id
    message.content = content

    if embed_description is not None or embed_title is not None:
        embed = MagicMock(spec=discord.Embed)
        embed.description = embed_description
        embed.title = embed_title
        embed.fields = []
        message.embeds = [embed]
    else:
        message.embeds = []

    message.interaction_metadata = None
    message.guild = MagicMock()
    message.guild.get_member = MagicMock(return_value=None)
    return message


def _make_member(*, has_target_role: bool = True) -> MagicMock:
    member = MagicMock(spec=discord.Member)
    if has_target_role:
        role = MagicMock()
        role.name = TARGET_ROLE_NAME
        member.roles = [role]
    else:
        member.roles = []
    return member


def test_detects_disboard_success() -> None:
    cog = _make_cog()
    message = _make_message(
        author_id=DISBOARD_BOT_ID,
        embed_description=f"サーバーの{DISBOARD_SUCCESS_KEYWORD}しました！",
    )
    assert cog._detect_bump_success(message) == "DISBOARD"


def test_detects_dissoku_success_in_title() -> None:
    cog = _make_cog()
    message = _make_message(
        author_id=DISSOKU_BOT_ID,
        embed_title=f"サーバー名 を{DISSOKU_SUCCESS_KEYWORD}したよ!",
    )
    assert cog._detect_bump_success(message) == "ディス速報"


def test_detects_dissoku_success_in_content() -> None:
    cog = _make_cog()
    message = _make_message(
        author_id=DISSOKU_BOT_ID,
        content=f"🍭CHILLカフェ を{DISSOKU_SUCCESS_KEYWORD}したよ!",
    )
    assert cog._detect_bump_success(message) == "ディス速報"


def test_detects_dissoku_success_in_fields() -> None:
    cog = _make_cog()
    message = _make_message(
        author_id=DISSOKU_BOT_ID,
        embed_description="<@12345>\nコマンド: `/up`",
    )
    field = MagicMock()
    field.name = f"{DISSOKU_SUCCESS_KEYWORD}しました!"
    field.value = "1時間後にまたupできます"
    message.embeds[0].fields = [field]
    assert cog._detect_bump_success(message) == "ディス速報"


def test_does_not_detect_failure_message() -> None:
    cog = _make_cog()
    message = _make_message(
        author_id=DISSOKU_BOT_ID,
        embed_description="<@12345>\nコマンド: `/up`",
    )
    field = MagicMock()
    field.name = "失敗しました..."
    field.value = "間隔をあけてください(76分)"
    message.embeds[0].fields = [field]
    assert cog._detect_bump_success(message) is None


def test_get_bump_user_returns_member() -> None:
    cog = _make_cog()
    member = _make_member()
    message = _make_message(author_id=DISBOARD_BOT_ID)
    message.interaction_metadata = MagicMock()
    message.interaction_metadata.user = member
    assert cog._get_bump_user(message) == member


def test_get_bump_user_fallback_to_guild_get_member() -> None:
    cog = _make_cog()
    message = _make_message(author_id=DISBOARD_BOT_ID)
    user = MagicMock(spec=discord.User)
    user.id = 123
    message.interaction_metadata = MagicMock()
    message.interaction_metadata.user = user

    fetched = _make_member()
    message.guild.get_member.return_value = fetched
    assert cog._get_bump_user(message) == fetched


def test_has_target_role() -> None:
    cog = _make_cog()
    assert cog._has_target_role(_make_member(has_target_role=True)) is True
    assert cog._has_target_role(_make_member(has_target_role=False)) is False


def test_service_registry_helpers() -> None:
    cog = _make_cog()
    names = cog._get_monitored_service_names()
    assert names == [service.name for service in BUMP_SERVICES]
    for service in BUMP_SERVICES:
        assert cog._get_service_by_bot_id(service.bot_id) == service


def test_reminder_hours_by_service() -> None:
    assert _get_reminder_hours("DISBOARD") == 5
    assert _get_reminder_hours("ディス速報") == 2


def test_sync_from_history_returns_no_result_when_not_found() -> None:
    cog = _make_cog()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 10
    cog.bot.get_channel.return_value = channel

    async def _run() -> None:
        from unittest.mock import AsyncMock, patch

        with patch.object(cog, "_find_recent_bumps", AsyncMock(return_value={})):
            ok, msg = await cog._sync_next_reminder_from_history(guild, "10")
            assert ok is False
            assert "見つけられませんでした" in msg

    import asyncio

    asyncio.run(_run())


def test_sync_from_history_sets_both_services() -> None:
    cog = _make_cog()
    guild = MagicMock(spec=discord.Guild)
    guild.id = 1

    channel = MagicMock(spec=discord.TextChannel)
    channel.id = 10
    cog.bot.get_channel.return_value = channel

    async def _run() -> None:
        from unittest.mock import AsyncMock, patch

        now = datetime.now(UTC)
        recent = {
            service.name: now - timedelta(minutes=30 - idx * 5)
            for idx, service in enumerate(BUMP_SERVICES)
        }

        fake_reminder = MagicMock()
        fake_reminder.is_enabled = True

        with (
            patch.object(cog, "_find_recent_bumps", AsyncMock(return_value=recent)),
            patch(
                "src.cogs.bump.upsert_bump_reminder",
                AsyncMock(return_value=fake_reminder),
            ) as upsert_mock,
        ):
            ok, msg = await cog._sync_next_reminder_from_history(guild, "10")
            assert ok is True
            for service in BUMP_SERVICES:
                assert service.name in msg
            assert upsert_mock.await_count == len(BUMP_SERVICES)

    import asyncio

    asyncio.run(_run())


def test_format_service_status_with_future_reminder() -> None:
    cog = _make_cog()
    guild = MagicMock(spec=discord.Guild)
    role = MagicMock(spec=discord.Role)
    role.name = "Bump通知"
    guild.get_role.return_value = role

    reminder = BumpReminder(
        guild_id="1",
        channel_id="2",
        service_name="DISBOARD",
        remind_at=datetime.now(UTC) + timedelta(minutes=30),
        is_enabled=True,
        role_id="123",
    )

    status = cog._format_service_status(guild, "DISBOARD", reminder)
    assert "通知: **有効**" in status
    assert "通知ロール: `@Bump通知`" in status
    assert "次回bump可能時刻: <t:" in status


def test_format_service_status_without_reminder() -> None:
    cog = _make_cog()
    guild = MagicMock(spec=discord.Guild)
    status = cog._format_service_status(guild, "ディス速報", None)
    assert "通知: **有効 (デフォルト)**" in status
    assert f"通知ロール: `@{TARGET_ROLE_NAME}` (デフォルト)" in status
    assert "次回bump可能時刻: 未判定" in status
