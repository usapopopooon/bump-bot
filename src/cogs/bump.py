"""Bump reminder cog for DISBOARD and ディス速報.

DISBOARD/ディス速報の bump 成功を検知し、サービス別の待機時間後にリマインドする。

仕組み:
  - on_message で DISBOARD/ディス速報 Bot のメッセージを監視
  - bump 成功 Embed を検知したら DB にリマインダーを保存
  - 30秒ごとのループタスクで送信予定時刻を過ぎたリマインダーをチェック
  - Server Bumper ロールにメンションして通知
  - 通知の有効/無効をボタンで切り替え可能

注意:
  - Bot 再起動後もリマインダーは DB に保存されているため継続して動作する
  - bump_channel_id が 0 の場合は機能が無効化される
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import discord
from discord import app_commands
from discord.ext import commands, tasks

from src.constants import DEFAULT_EMBED_COLOR
from src.database.engine import async_session
from src.services.bump_service import (
    claim_bump_detection,
    clear_bump_reminder,
    delete_bump_config,
    delete_bump_reminders_by_guild,
    get_bump_config,
    get_bump_reminder,
    get_due_bump_reminders,
    toggle_bump_reminder,
    update_bump_reminder_role,
    upsert_bump_config,
    upsert_bump_reminder,
)
from src.utils import get_resource_lock

logger = logging.getLogger(__name__)

# DISBOARD Bot の ID
DISBOARD_BOT_ID = 302050872383242240

# ディス速報 Bot の ID
DISSOKU_BOT_ID = 761562078095867916

# デバッグ用: テストユーザー ID
DEBUG_USER_ID = 1058651415289012295

# bump 成功を判定するキーワード
DISBOARD_SUCCESS_KEYWORD = "表示順をアップ"
DISSOKU_SUCCESS_KEYWORD = "アップ"


@dataclass(frozen=True)
class BumpServiceDefinition:
    name: str
    bot_id: int
    success_keywords: tuple[str, ...]
    check_title: bool = False
    check_description: bool = True
    check_fields: bool = False
    check_content: bool = False


BUMP_SERVICES: tuple[BumpServiceDefinition, ...] = (
    BumpServiceDefinition(
        name="DISBOARD",
        bot_id=DISBOARD_BOT_ID,
        success_keywords=(DISBOARD_SUCCESS_KEYWORD,),
        check_description=True,
    ),
    BumpServiceDefinition(
        name="ディス速報",
        bot_id=DISSOKU_BOT_ID,
        success_keywords=(DISSOKU_SUCCESS_KEYWORD,),
        check_title=True,
        check_description=True,
        check_fields=True,
        check_content=True,
    ),
)

# デフォルトのリマインダー送信間隔 (bump から何時間後か)
REMINDER_HOURS = 2

# DISBOARD のリマインダー送信間隔 (bump から何時間後か)
DISBOARD_REMINDER_HOURS = 5

# リマインダーチェック間隔 (秒)
REMINDER_CHECK_INTERVAL_SECONDS = 30

# リマインド対象のロール名
TARGET_ROLE_NAME = "Server Bumper"

# =============================================================================
# Bump 通知設定クールダウン (連打対策)
# =============================================================================

# Bump 通知設定操作のクールダウン時間 (秒)
BUMP_NOTIFICATION_COOLDOWN_SECONDS = 3

# ユーザーごとの最終操作時刻を記録
# key: (user_id, guild_id, service_name), value: timestamp (float)
_bump_notification_cooldown_cache: dict[tuple[int, str, str], float] = {}

# キャッシュクリーンアップ間隔
_BUMP_CLEANUP_INTERVAL = 300  # 5分
_bump_last_cleanup_time = float("-inf")


def _cleanup_bump_notification_cooldown_cache() -> None:
    """古いBump通知設定クールダウンエントリを削除する."""
    global _bump_last_cleanup_time
    now = time.monotonic()

    # 5分ごとにクリーンアップ
    if (
        _bump_last_cleanup_time > 0
        and now - _bump_last_cleanup_time < _BUMP_CLEANUP_INTERVAL
    ):
        return

    _bump_last_cleanup_time = now

    # 1パス削除: キーのスナップショットから期限切れをその場で削除
    for key in list(_bump_notification_cooldown_cache):
        if now - _bump_notification_cooldown_cache[key] > _BUMP_CLEANUP_INTERVAL:
            del _bump_notification_cooldown_cache[key]


def is_bump_notification_on_cooldown(
    user_id: int, guild_id: str, service_name: str
) -> bool:
    """ユーザーがBump通知設定操作のクールダウン中かどうかを確認する.

    Args:
        user_id: Discord ユーザー ID
        guild_id: ギルド ID
        service_name: サービス名 ("DISBOARD" or "ディス速報")

    Returns:
        クールダウン中なら True
    """
    _cleanup_bump_notification_cooldown_cache()

    key = (user_id, guild_id, service_name)
    now = time.monotonic()

    last_time = _bump_notification_cooldown_cache.get(key)
    if last_time is not None and now - last_time < BUMP_NOTIFICATION_COOLDOWN_SECONDS:
        return True

    # クールダウンを記録/更新
    _bump_notification_cooldown_cache[key] = now
    return False


def clear_bump_notification_cooldown_cache() -> None:
    """Bump通知設定クールダウンキャッシュをクリアする (テスト用)."""
    global _bump_last_cleanup_time
    _bump_notification_cooldown_cache.clear()
    _bump_last_cleanup_time = float("-inf")


def _get_reminder_hours(service_name: str) -> int:
    """サービスごとのリマインダー送信間隔を返す。"""
    if service_name == "DISBOARD":
        return DISBOARD_REMINDER_HOURS
    return REMINDER_HOURS


# =============================================================================
# 通知設定用 View
# =============================================================================


class BumpRoleSelectMenu(discord.ui.RoleSelect["BumpRoleSelectView"]):
    """通知先ロールを選択するセレクトメニュー。"""

    def __init__(
        self,
        guild_id: str,
        service_name: str,
        current_role_id: str | None = None,
    ) -> None:
        # 現在のロールがある場合はデフォルト値として設定
        default_values: list[discord.SelectDefaultValue] = []
        if current_role_id:
            default_values = [
                discord.SelectDefaultValue(
                    id=int(current_role_id),
                    type=discord.SelectDefaultValueType.role,
                )
            ]

        super().__init__(
            placeholder="通知先ロールを選択...",
            min_values=1,
            max_values=1,
            default_values=default_values,
        )
        self.guild_id = guild_id
        self.service_name = service_name

    async def callback(self, interaction: discord.Interaction) -> None:
        """ロール選択時のコールバック。"""
        if not self.values:
            return

        selected_role = self.values[0]
        try:
            await interaction.response.defer()
        except (discord.HTTPException, discord.InteractionResponded):
            logger.warning(
                "Role select defer failed: guild=%s user=%s service=%s",
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
                self.service_name,
                exc_info=True,
            )
            return

        # ギルド・サービスごとのロックで並行リクエストをシリアライズ
        async with get_resource_lock(
            f"bump_notification:{self.guild_id}:{self.service_name}"
        ):
            async with async_session() as session:
                updated = await update_bump_reminder_role(
                    session, self.guild_id, self.service_name, str(selected_role.id)
                )

            if not updated:
                await interaction.followup.send(
                    "通知先ロールの変更に失敗しました。"
                    "先に `/bump setup` を実行してから再度お試しください。",
                    ephemeral=True,
                )
                logger.warning(
                    "Role select update skipped (no reminder row): "
                    "guild=%s service=%s role=%s",
                    self.guild_id,
                    self.service_name,
                    selected_role.id,
                )
                return

            if interaction.message:
                await interaction.followup.edit_message(
                    interaction.message.id,
                    content=f"通知先ロールを **{selected_role.name}** に変更しました。",
                    view=None,
                )
            else:
                await interaction.followup.send(
                    f"通知先ロールを **{selected_role.name}** に変更しました。",
                    ephemeral=True,
                )
            logger.info(
                "Bump notification role changed: guild=%s service=%s role=%s",
                self.guild_id,
                self.service_name,
                selected_role.name,
            )


class BumpRoleSelectView(discord.ui.View):
    """ロール選択メニューを含む View。"""

    def __init__(
        self,
        guild_id: str,
        service_name: str,
        current_role_id: str | None = None,
    ) -> None:
        super().__init__(timeout=60)
        self.add_item(BumpRoleSelectMenu(guild_id, service_name, current_role_id))

    @discord.ui.button(label="デフォルトに戻す", style=discord.ButtonStyle.secondary)
    async def reset_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[BumpRoleSelectView],
    ) -> None:
        """ロールをデフォルト (Server Bumper) に戻す。"""
        try:
            await interaction.response.defer()
        except (discord.HTTPException, discord.InteractionResponded):
            logger.warning(
                "Role reset defer failed: guild=%s user=%s",
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
                exc_info=True,
            )
            return

        guild_id = str(interaction.guild_id) if interaction.guild_id else ""
        # service_name はメニューから取得 (順序は実装依存なので型で探す)
        menu = None
        for child in self.children:
            if isinstance(child, BumpRoleSelectMenu):
                menu = child
                break
        if menu is None:
            return
        service_name = menu.service_name

        # ギルド・サービスごとのロックで並行リクエストをシリアライズ
        async with get_resource_lock(f"bump_notification:{guild_id}:{service_name}"):
            async with async_session() as session:
                updated = await update_bump_reminder_role(
                    session, guild_id, service_name, None
                )

            if not updated:
                await interaction.followup.send(
                    "通知先ロールの変更に失敗しました。"
                    "先に `/bump setup` を実行してから再度お試しください。",
                    ephemeral=True,
                )
                logger.warning(
                    "Role reset update skipped (no reminder row): guild=%s service=%s",
                    guild_id,
                    service_name,
                )
                return

            msg = f"通知先ロールを **{TARGET_ROLE_NAME}** (デフォルト) に戻しました。"
            if interaction.message:
                await interaction.followup.edit_message(
                    interaction.message.id, content=msg, view=None
                )
            else:
                await interaction.followup.send(msg, ephemeral=True)
            logger.info(
                "Bump notification role reset to default: guild=%s service=%s",
                guild_id,
                service_name,
            )


class BumpNotificationView(discord.ui.View):
    """bump 通知の設定を変更するボタンを持つ View。

    Bot 再起動後もボタンが動作するよう、timeout=None で永続化する。
    """

    def __init__(self, guild_id: str, service_name: str, is_enabled: bool) -> None:
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.service_name = service_name
        self._update_toggle_button(is_enabled)
        self._update_role_button()

    def _update_toggle_button(self, is_enabled: bool) -> None:
        """トグルボタンの表示を現在の状態に合わせて更新する。"""
        self.toggle_button.label = (
            "通知を無効にする" if is_enabled else "通知を有効にする"
        )
        self.toggle_button.style = (
            discord.ButtonStyle.secondary if is_enabled else discord.ButtonStyle.success
        )
        # custom_id を状態に関係なく固定 (guild_id と service_name で識別)
        self.toggle_button.custom_id = (
            f"bump_toggle:{self.guild_id}:{self.service_name}"
        )

    def _update_role_button(self) -> None:
        """ロール変更ボタンの custom_id を設定する。"""
        self.role_button.custom_id = f"bump_role:{self.guild_id}:{self.service_name}"

    @discord.ui.button(label="通知を無効にする", style=discord.ButtonStyle.secondary)
    async def toggle_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[BumpNotificationView],
    ) -> None:
        """通知の有効/無効を切り替える。"""
        # クールダウンチェック (連打対策)
        if is_bump_notification_on_cooldown(
            interaction.user.id, self.guild_id, self.service_name
        ):
            await interaction.response.send_message(
                "操作が早すぎます。少し待ってから再度お試しください。",
                ephemeral=True,
            )
            return

        # インタラクションを即座に確認 (複数インスタンス実行時の重複防止)
        try:
            await interaction.response.defer()
        except (discord.HTTPException, discord.InteractionResponded):
            return

        # ギルド・サービスごとのロックで並行リクエストをシリアライズ
        async with get_resource_lock(
            f"bump_notification:{self.guild_id}:{self.service_name}"
        ):
            async with async_session() as session:
                new_state = await toggle_bump_reminder(
                    session, self.guild_id, self.service_name
                )

            self._update_toggle_button(new_state)

            status = "有効" if new_state else "無効"
            if interaction.message:
                await interaction.message.edit(view=self)
            await interaction.followup.send(
                f"**{self.service_name}** の通知を **{status}** にしました。",
                ephemeral=True,
            )
            logger.info(
                "Bump notification toggled: guild=%s service=%s enabled=%s",
                self.guild_id,
                self.service_name,
                new_state,
            )

    @discord.ui.button(label="通知ロールを変更", style=discord.ButtonStyle.primary)
    async def role_button(
        self,
        interaction: discord.Interaction,
        _button: discord.ui.Button[BumpNotificationView],
    ) -> None:
        """通知先ロールの変更メニューを表示する。"""
        # クールダウンチェック (連打対策)
        if is_bump_notification_on_cooldown(
            interaction.user.id, self.guild_id, self.service_name
        ):
            await interaction.response.send_message(
                "操作が早すぎます。少し待ってから再度お試しください。",
                ephemeral=True,
            )
            return

        try:
            await interaction.response.defer(ephemeral=True)
        except (discord.HTTPException, discord.InteractionResponded):
            logger.warning(
                "Role button defer failed: guild=%s user=%s service=%s",
                interaction.guild_id,
                interaction.user.id if interaction.user else None,
                self.service_name,
                exc_info=True,
            )
            return

        # ギルド・サービスごとのロックで並行リクエストをシリアライズ
        async with get_resource_lock(
            f"bump_notification:{self.guild_id}:{self.service_name}"
        ):
            # 現在の設定を取得
            current_role_id: str | None = None
            async with async_session() as session:
                reminder = await get_bump_reminder(
                    session, self.guild_id, self.service_name
                )
                if reminder:
                    current_role_id = reminder.role_id

            view = BumpRoleSelectView(self.guild_id, self.service_name, current_role_id)
            await interaction.followup.send(
                f"**{self.service_name}** の通知先ロールを選択してください。",
                view=view,
                ephemeral=True,
            )


class BumpCog(commands.Cog):
    """DISBOARD/ディス速報の bump リマインダー機能を提供する Cog。"""

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        # bump 設定済みギルド ID のインメモリキャッシュ
        # None = 未ロード (フォールスルー), set = ロード済み (キャッシュ使用)
        self._bump_guild_ids: set[str] | None = None

    async def cog_load(self) -> None:
        """Cog が読み込まれたときに呼ばれる。リマインダーチェックループを開始する。"""
        self._reminder_check.start()
        logger.info("Bump reminder cog loaded, reminder check loop started")

    async def cog_unload(self) -> None:
        """Cog がアンロードされたときに呼ばれる。ループを停止する。"""
        if self._reminder_check.is_running():
            self._reminder_check.cancel()

    def _get_monitored_services(self) -> tuple[BumpServiceDefinition, ...]:
        return BUMP_SERVICES

    def _get_monitored_service_names(self) -> list[str]:
        return [service.name for service in self._get_monitored_services()]

    def _get_service_by_bot_id(self, bot_id: int) -> BumpServiceDefinition | None:
        for service in self._get_monitored_services():
            if service.bot_id == bot_id:
                return service
        return None

    # ==========================================================================
    # クリーンアップリスナー
    # ==========================================================================

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel) -> None:
        """チャンネル削除時に bump 監視設定を削除する。"""
        guild_id = str(channel.guild.id)
        channel_id = str(channel.id)

        async with async_session() as session:
            config = await get_bump_config(session, guild_id)

            # 削除されたチャンネルが監視チャンネルと一致する場合のみ削除
            if config and config.channel_id == channel_id:
                await delete_bump_config(session, guild_id)
                if self._bump_guild_ids is not None:
                    self._bump_guild_ids.discard(guild_id)
                # リマインダーも削除 (チャンネルが存在しないため送信不可)
                count = await delete_bump_reminders_by_guild(session, guild_id)
                logger.info(
                    "Cleaned up bump config and %d reminder(s) for deleted channel: "
                    "guild=%s channel=%s",
                    count,
                    guild_id,
                    channel_id,
                )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild) -> None:
        """ギルドからボットが削除された時に関連する bump データを全て削除する。"""
        guild_id = str(guild.id)

        async with async_session() as session:
            # 設定を削除
            await delete_bump_config(session, guild_id)
            if self._bump_guild_ids is not None:
                self._bump_guild_ids.discard(guild_id)
            # リマインダーを削除
            count = await delete_bump_reminders_by_guild(session, guild_id)

        if count > 0:
            logger.info(
                "Cleaned up bump config and %d reminder(s) for removed guild: guild=%s",
                count,
                guild_id,
            )

    # ==========================================================================
    # メッセージ監視
    # ==========================================================================

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """メッセージを監視し、bump 成功を検知する。"""
        await self._process_bump_message(message)

    @commands.Cog.listener()
    async def on_message_edit(
        self, before: discord.Message, after: discord.Message
    ) -> None:
        """メッセージ編集を監視し、bump 成功を検知する。

        ディス速報は最初に空のメッセージを送信し、後から embed を追加するため、
        on_message_edit でも検知する必要がある。
        """
        # before に embed がなく、after に embed がある場合のみ処理
        # (既に検知済みのメッセージを再処理しないため)
        if not before.embeds and after.embeds:
            await self._process_bump_message(after)

    async def _process_bump_message(self, message: discord.Message) -> None:
        """bump メッセージを処理する共通ロジック。

        DISBOARD/ディス速報 Bot からのメッセージで、設定されたチャンネルかつ
        bump 成功の Embed が含まれていれば、リマインダーを登録する。
        """
        # ギルドがなければ無視 (DM など)
        if not message.guild:
            return

        service = self._get_service_by_bot_id(message.author.id)

        # 監視対象 Bot 以外は無視 (DEBUG_USER_ID はテスト用)
        if not service and message.author.id != DEBUG_USER_ID:
            return

        guild_id = str(message.guild.id)

        # インメモリキャッシュで高速フィルタリング (DB アクセスゼロ)
        if self._bump_guild_ids is not None and guild_id not in self._bump_guild_ids:
            return

        bot_name = service.name if service else "DEBUG"
        logger.info(
            "Bump bot message received: bot=%s guild=%s channel=%s",
            bot_name,
            message.guild.id,
            message.channel.id,
        )

        # Embed もメッセージ本文もなければ無視
        if not message.embeds and not message.content:
            logger.info(
                "Bump bot message has no embeds or content, waiting for edit: bot=%s",
                bot_name,
            )
            return

        # bump 成功かどうかを判定 (DB 不要な判定を先に行う)
        service_name = self._detect_bump_success(message)
        if not service_name:
            return

        # bump 実行者を取得
        user = self._get_bump_user(message)
        if not user:
            logger.warning(
                "Could not get bump user from interaction_metadata: "
                "guild=%s service=%s interaction_metadata=%s",
                guild_id,
                service_name,
                message.interaction_metadata,
            )
            return

        # Server Bumper ロールを持っているか確認
        if not self._has_target_role(user):
            logger.info(
                "User does not have required role, skipping reminder: "
                "user=%s required_role=%s guild=%s",
                user.name,
                TARGET_ROLE_NAME,
                guild_id,
            )
            return

        # 1セッションで設定確認 + アトミックなリマインダー登録
        remind_at = datetime.now(UTC) + timedelta(
            hours=_get_reminder_hours(service_name)
        )
        async with async_session() as session:
            # このギルドの bump 監視設定を確認
            config = await get_bump_config(session, guild_id)

            # 設定がないか、設定されたチャンネルでなければ無視
            if not config or config.channel_id != str(message.channel.id):
                logger.info(
                    "Bump monitoring not configured for this channel: "
                    "guild=%s config_channel=%s message_channel=%s",
                    guild_id,
                    config.channel_id if config else None,
                    message.channel.id,
                )
                return

            # アトミックに bump 検知の権利を取得
            # (複数インスタンス実行時、最初に claim したインスタンスだけが続行する)
            reminder = await claim_bump_detection(
                session,
                guild_id=guild_id,
                channel_id=str(message.channel.id),
                service_name=service_name,
                remind_at=remind_at,
            )
            if not reminder:
                logger.info(
                    "Bump already processed by another instance: guild=%s service=%s",
                    guild_id,
                    service_name,
                )
                return

            logger.info(
                "Bump success detected: service=%s guild=%s user=%s",
                service_name,
                guild_id,
                user.name,
            )

            is_enabled = reminder.is_enabled
            custom_role_id = reminder.role_id

        # 通知先ロール名を取得
        role_name: str | None = None
        if custom_role_id:
            role = message.guild.get_role(int(custom_role_id))
            if role:
                role_name = role.name

        # bump 検知の確認 Embed を送信
        embed = self._build_detection_embed(
            service_name, user, remind_at, is_enabled, role_name
        )
        view = BumpNotificationView(guild_id, service_name, is_enabled)
        self.bot.add_view(view)

        try:
            await message.channel.send(embed=embed, view=view)
        except discord.HTTPException as e:
            logger.warning("Failed to send bump detection embed: %s", e)

        logger.info(
            "Bump detected: service=%s user=%s remind_at=%s is_enabled=%s",
            service_name,
            user.name,
            remind_at.isoformat(),
            is_enabled,
        )

    def _detect_bump_success(self, message: discord.Message) -> str | None:
        """メッセージから bump 成功を検知し、サービス名を返す。

        Returns:
            サービス名。検知できなければ None
        """
        service = self._get_service_by_bot_id(message.author.id)
        if not service:
            return None

        for embed in message.embeds:
            description = embed.description or ""
            title = embed.title or ""
            fields = embed.fields or []

            # embed 内容をログ出力
            fields_summary = [
                {"name": f.name, "value": f.value[:80] if f.value else ""}
                for f in fields
            ]
            logger.debug(
                "Parsing embed: bot=%s title=%s description=%s fields=%s",
                service.name,
                title[:80] if title else None,
                description[:80] if description else None,
                fields_summary,
            )

            for keyword in service.success_keywords:
                if service.check_title and keyword in title:
                    return service.name
                if service.check_description and keyword in description:
                    return service.name
                if service.check_fields:
                    for field in fields:
                        if keyword in (field.name or ""):
                            return service.name
                        if keyword in (field.value or ""):
                            return service.name

        if service.check_content and message.content:
            for keyword in service.success_keywords:
                if keyword in message.content:
                    return service.name

        # 検知できなかった
        logger.debug(
            "Bump success keyword not found: bot=%s keyword=%s",
            service.name,
            ",".join(service.success_keywords),
        )
        return None

    def _get_bump_user(self, message: discord.Message) -> discord.Member | None:
        """bump を実行したユーザーを取得する。

        message.interaction_metadata から取得を試み、失敗したら None を返す。
        """
        # スラッシュコマンドの場合、interaction_metadata.user に実行者がいる
        if message.interaction_metadata and message.interaction_metadata.user:
            user = message.interaction_metadata.user
            # Member でない場合は guild から取得し直す
            if isinstance(user, discord.Member):
                return user
            if message.guild:
                return message.guild.get_member(user.id)
        return None

    def _has_target_role(self, member: discord.Member) -> bool:
        """メンバーが Server Bumper ロールを持っているか確認する。"""
        return any(role.name == TARGET_ROLE_NAME for role in member.roles)

    async def _find_recent_bumps(
        self, channel: discord.TextChannel, limit: int = 100
    ) -> dict[str, datetime]:
        """チャンネル履歴からサービス別の最新 bump 成功時刻を探す。

        Args:
            channel: 検索対象のチャンネル
            limit: 検索するメッセージ数の上限

        Returns:
            {"DISBOARD": datetime, "ディス速報": datetime} の部分集合
        """
        latest: dict[str, datetime] = {}
        monitored_service_names = set(self._get_monitored_service_names())
        try:
            async for message in channel.history(limit=limit):
                # 監視対象サービス Bot 以外は無視
                if not self._get_service_by_bot_id(message.author.id):
                    continue

                # bump 成功かどうかを判定
                service_name = self._detect_bump_success(message)
                if service_name and service_name not in latest:
                    latest[service_name] = message.created_at
                    if monitored_service_names.issubset(set(latest)):
                        break

        except discord.HTTPException as e:
            logger.warning("Failed to search channel history: %s", e)

        return latest

    async def _sync_next_reminder_from_history(
        self, guild: discord.Guild, channel_id: str
    ) -> tuple[bool, str]:
        channel = self.bot.get_channel(int(channel_id))
        if not isinstance(channel, discord.TextChannel):
            return (False, "監視チャンネルを取得できませんでした。")

        recent_bumps = await self._find_recent_bumps(channel)
        if not recent_bumps:
            return (False, "履歴から bump 成功メッセージを見つけられませんでした。")
        now = datetime.now(UTC)
        configured: list[str] = []
        skipped: list[str] = []

        async with async_session() as session:
            for service_name, bump_time in recent_bumps.items():
                remind_at = bump_time + timedelta(
                    hours=_get_reminder_hours(service_name)
                )
                if remind_at <= now:
                    skipped.append(service_name)
                    continue

                reminder = await upsert_bump_reminder(
                    session,
                    guild_id=str(guild.id),
                    channel_id=channel_id,
                    service_name=service_name,
                    remind_at=remind_at,
                )
                ts = int(remind_at.timestamp())
                status = "有効" if reminder.is_enabled else "無効"
                configured.append(f"・{service_name}: <t:{ts}:F> (通知: **{status}**)")

        if configured:
            msg = "履歴から次回通知を設定しました。\n" + "\n".join(configured)
            if skipped:
                msg += "\n\n次回可能時刻を過ぎていたため未設定: " + " / ".join(skipped)
            return (True, msg)

        return (
            False,
            (
                "履歴には bump 成功がありましたが、"
                "いずれも次回可能時刻を過ぎているため設定しませんでした。"
            ),
        )

    # ==========================================================================
    # Embed 生成
    # ==========================================================================

    def _build_detection_embed(
        self,
        service_name: str,
        user: discord.Member,
        remind_at: datetime,
        is_enabled: bool,
        role_name: str | None = None,
    ) -> discord.Embed:
        """bump 検知時の確認 Embed を生成する。

        Args:
            service_name: サービス名 ("DISBOARD" または "ディス速報")
            user: bump を実行したユーザー
            remind_at: リマインド予定時刻
            is_enabled: 通知が有効かどうか
            role_name: 通知先ロール名 (None の場合はデフォルトロール)

        Returns:
            確認用の Embed
        """
        # Discord タイムスタンプ形式
        ts = int(remind_at.timestamp())
        time_absolute = f"<t:{ts}:t>"  # 短い時刻表示 (例: 21:30)

        # 通知先ロール名 (デフォルトは Server Bumper)
        display_role = role_name or TARGET_ROLE_NAME

        if is_enabled:
            description = (
                f"{user.mention} さんが **{service_name}** を bump しました！\n\n"
                f"次の bump リマインドは {time_absolute} に送信します。\n"
                f"現在の通知先: `@{display_role}`"
            )
        else:
            description = (
                f"{user.mention} さんが **{service_name}** を bump しました！\n\n"
                f"通知は現在 **無効** です。\n"
                f"現在の通知先: `@{display_role}`"
            )

        embed = discord.Embed(
            title="Bump 検知",
            description=description,
            color=DEFAULT_EMBED_COLOR,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=service_name)
        return embed

    def _build_reminder_embed(self, service_name: str) -> discord.Embed:
        """bump リマインダーの Embed を生成する。

        Args:
            service_name: サービス名 ("DISBOARD" または "ディス速報")

        Returns:
            リマインダー用の Embed
        """
        embed = discord.Embed(
            title="Bump リマインダー",
            description=(
                f"**{service_name}** の bump ができるようになりました！\n\n"
                f"サーバーを上位に表示させるために bump しましょう。"
            ),
            color=DEFAULT_EMBED_COLOR,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text=service_name)
        return embed

    def _format_service_status(
        self,
        guild: discord.Guild,
        service_name: str,
        reminder: BumpReminder | None,
    ) -> str:
        """サービス別の status 表示テキストを生成する。"""
        role_display = f"`@{TARGET_ROLE_NAME}` (デフォルト)"
        notify_status = "有効 (デフォルト)"
        next_bump = "未判定"

        if reminder:
            if reminder.role_id:
                role = guild.get_role(int(reminder.role_id))
                if role:
                    role_display = f"`@{role.name}`"
                else:
                    role_display = (
                        f"`@{TARGET_ROLE_NAME}` (デフォルト, カスタムロール未解決)"
                    )

            notify_status = "有効" if reminder.is_enabled else "無効"

            if reminder.remind_at:
                ts = int(reminder.remind_at.timestamp())
                now = datetime.now(UTC)
                if reminder.remind_at > now:
                    next_bump = f"<t:{ts}:F> (<t:{ts}:R>)"
                else:
                    next_bump = f"可能です (前回記録: <t:{ts}:F>)"
            else:
                next_bump = "未設定"

        return (
            f"・{service_name}:\n"
            f"  通知: **{notify_status}**\n"
            f"  通知ロール: {role_display}\n"
            f"  次回bump可能時刻: {next_bump}"
        )

    # ==========================================================================
    # リマインダーチェックループ
    # ==========================================================================

    @tasks.loop(seconds=REMINDER_CHECK_INTERVAL_SECONDS)
    async def _reminder_check(self) -> None:
        """30秒ごとに実行されるリマインダーチェック処理。

        DB から送信予定時刻を過ぎたリマインダーを取得し、
        対象チャンネルに Server Bumper ロールをメンションして通知する。
        """
        now = datetime.now(UTC)

        async with async_session() as session:
            due_reminders = await get_due_bump_reminders(session, now)

            for reminder in due_reminders:
                # アトミックにクリア → 成功したインスタンスだけが送信
                cleared = await clear_bump_reminder(session, reminder.id)
                if cleared:
                    await self._send_reminder(reminder)

    @_reminder_check.before_loop
    async def _before_reminder_check(self) -> None:
        """リマインダーチェックループ開始前に Bot の接続完了を待つ。"""
        await self.bot.wait_until_ready()

    async def _send_reminder(self, reminder: BumpReminder) -> None:
        """リマインダー通知を送信する。

        Args:
            reminder: 送信する BumpReminder オブジェクト
        """
        channel = self.bot.get_channel(int(reminder.channel_id))
        if not isinstance(channel, discord.TextChannel):
            logger.warning(
                "Reminder channel %s not found or not a text channel "
                "(guild=%s, service=%s)",
                reminder.channel_id,
                reminder.guild_id,
                reminder.service_name,
            )
            return

        guild = channel.guild
        role: discord.Role | None = None

        # カスタムロールが設定されている場合はそれを使用
        if reminder.role_id:
            role = guild.get_role(int(reminder.role_id))
            if not role:
                logger.warning(
                    "Custom role %s not found in guild %s",
                    reminder.role_id,
                    guild.name,
                )

        # カスタムロールがない場合はデフォルトの Server Bumper ロールを使用
        if not role:
            role = discord.utils.get(guild.roles, name=TARGET_ROLE_NAME)

        if role:
            mention = role.mention
        else:
            # ロールが見つからない場合は @here で代用
            mention = "@here"
            logger.warning(
                "Role '%s' not found in guild %s, using @here instead",
                TARGET_ROLE_NAME,
                guild.name,
            )

        # リマインダー Embed を送信
        embed = self._build_reminder_embed(reminder.service_name)
        view = BumpNotificationView(
            reminder.guild_id, reminder.service_name, reminder.is_enabled
        )
        self.bot.add_view(view)

        try:
            await channel.send(content=mention, embed=embed, view=view)
            logger.info(
                "Sent bump reminder: guild=%s service=%s",
                reminder.guild_id,
                reminder.service_name,
            )
        except discord.HTTPException as e:
            logger.error(
                "Failed to send bump reminder: guild=%s channel=%s service=%s error=%s",
                reminder.guild_id,
                reminder.channel_id,
                reminder.service_name,
                e,
            )

    # ==========================================================================
    # スラッシュコマンド
    # ==========================================================================

    bump_group = app_commands.Group(
        name="bump",
        description="Bump リマインダーの設定",
        default_permissions=discord.Permissions(administrator=True),
    )

    @bump_group.command(name="setup", description="このチャンネルでbump監視を開始")
    async def bump_setup(self, interaction: discord.Interaction) -> None:
        """このチャンネルを bump 監視チャンネルとして設定する。"""
        if not interaction.guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        # インタラクションを即座に確認 (複数インスタンス実行時の重複防止)
        try:
            await interaction.response.defer()
        except (discord.HTTPException, discord.InteractionResponded):
            return

        guild_id = str(interaction.guild.id)
        channel_id = str(interaction.channel_id)

        # ギルド単位のロックで重複セットアップを防止
        async with get_resource_lock(f"bump_setup:{guild_id}"):
            # 設定を保存
            async with async_session() as session:
                await upsert_bump_config(session, guild_id, channel_id)

            # キャッシュに追加
            if self._bump_guild_ids is not None:
                self._bump_guild_ids.add(guild_id)

            # チャンネルの履歴から最近の bump を探す
            channel = interaction.channel
            recent_bump_info: str | None = None
            detected_service: str | None = None
            is_enabled = True
            reminder_time_text: str | None = None  # 具体的なリマインド時刻
            custom_role_name: str | None = None  # カスタム通知ロール名

            if isinstance(channel, discord.TextChannel):
                recent_bumps = await self._find_recent_bumps(channel)
                if recent_bumps:
                    service_name, bump_time = max(
                        recent_bumps.items(), key=lambda item: item[1]
                    )
                    detected_service = service_name
                    remind_at = bump_time + timedelta(
                        hours=_get_reminder_hours(service_name)
                    )
                    now = datetime.now(UTC)

                    if remind_at > now:
                        # 次の bump まで待機中 → リマインダーを作成
                        async with async_session() as session:
                            reminder = await upsert_bump_reminder(
                                session,
                                guild_id=guild_id,
                                channel_id=channel_id,
                                service_name=service_name,
                                remind_at=remind_at,
                            )
                            is_enabled = reminder.is_enabled
                            # カスタムロール名を取得
                            if reminder.role_id:
                                role = interaction.guild.get_role(int(reminder.role_id))
                                if role:
                                    custom_role_name = role.name
                        ts = int(remind_at.timestamp())
                        reminder_time_text = f"<t:{ts}:t>"
                        recent_bump_info = (
                            f"\n\n**📊 直近の bump を検出:**\n"
                            f"サービス: **{service_name}**\n"
                            f"次の bump 可能時刻: {reminder_time_text}\n"
                            f"リマインダーを自動設定しました。"
                        )
                    else:
                        # 既に bump 可能
                        recent_bump_info = (
                            f"\n\n**📊 直近の bump を検出:**\n"
                            f"サービス: **{service_name}**\n"
                            f"✅ 現在 bump 可能です！"
                        )

        # リマインド時刻が分かっている場合は具体的な時刻を表示
        if reminder_time_text:
            reminder_desc = f"{reminder_time_text} にリマインドを送信します。"
        else:
            reminder_desc = "リマインドを送信します。"

        # 通知先ロール名を表示
        display_role = custom_role_name or TARGET_ROLE_NAME

        base_description = (
            f"監視チャンネル: <#{channel_id}>\n"
            f"現在の通知先: `@{display_role}`\n\n"
            f"監視対象サービス: {', '.join(self._get_monitored_service_names())}\n"
            f"bump 成功を検知し、{reminder_desc}"
        )

        embed = discord.Embed(
            title="Bump 監視を開始しました",
            description=base_description + (recent_bump_info or ""),
            color=DEFAULT_EMBED_COLOR,
            timestamp=datetime.now(UTC),
        )
        embed.set_footer(text="Bump リマインダー")

        if detected_service:
            # 直近の bump が検出された場合、そのサービスのボタンを表示
            view = BumpNotificationView(guild_id, detected_service, is_enabled)
            self.bot.add_view(view)
            await interaction.followup.send(embed=embed, view=view)
        else:
            # 検出されなかった場合、全サービスのボタンを表示
            await interaction.followup.send(embed=embed)
            for service_name in self._get_monitored_service_names():
                view = BumpNotificationView(guild_id, service_name, True)
                self.bot.add_view(view)
                await interaction.followup.send(
                    f"**{service_name}** の通知設定:", view=view
                )
        logger.info(
            "Bump monitoring enabled: guild=%s channel=%s",
            guild_id,
            channel_id,
        )

    @bump_group.command(name="status", description="bump 監視の設定状況を確認する")
    async def bump_status(self, interaction: discord.Interaction) -> None:
        """現在の bump 監視設定を表示する。"""
        if not interaction.guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        guild_id = str(interaction.guild.id)

        async with async_session() as session:
            config = await get_bump_config(session, guild_id)
            reminders_by_service: dict[str, BumpReminder | None] = {}
            for service_name in self._get_monitored_service_names():
                reminders_by_service[service_name] = await get_bump_reminder(
                    session, guild_id, service_name
                )

        if config:
            # Discord タイムスタンプ形式で設定日時を表示
            ts = int(config.created_at.timestamp())
            service_statuses = [
                self._format_service_status(
                    interaction.guild, service_name, reminders_by_service[service_name]
                )
                for service_name in self._get_monitored_service_names()
            ]

            embed = discord.Embed(
                title="Bump 監視設定",
                description=(
                    f"**監視チャンネル:** <#{config.channel_id}>\n"
                    f"**設定日時:** <t:{ts}:F>\n\n"
                    f"**サービス別ステータス:**\n"
                    f"{'\n'.join(service_statuses)}"
                ),
                color=DEFAULT_EMBED_COLOR,
            )
            embed.set_footer(text="Bump リマインダー")
            await interaction.response.send_message(embed=embed)
        else:
            embed = discord.Embed(
                title="Bump 監視設定",
                description=(
                    "このサーバーでは bump 監視が設定されていません。\n\n"
                    "`/bump setup` で設定してください。"
                ),
                color=DEFAULT_EMBED_COLOR,
            )
            embed.set_footer(text="Bump リマインダー")
            await interaction.response.send_message(embed=embed)

    @bump_group.command(name="disable", description="bump 監視を停止する")
    async def bump_disable(self, interaction: discord.Interaction) -> None:
        """bump 監視を停止する。"""
        if not interaction.guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        guild_id = str(interaction.guild.id)

        async with async_session() as session:
            deleted = await delete_bump_config(session, guild_id)

        # キャッシュから削除
        if self._bump_guild_ids is not None:
            self._bump_guild_ids.discard(guild_id)

        if deleted:
            embed = discord.Embed(
                title="Bump 監視を停止しました",
                description="このサーバーでの bump 監視を無効にしました。",
                color=DEFAULT_EMBED_COLOR,
                timestamp=datetime.now(UTC),
            )
            embed.set_footer(text="Bump リマインダー")
            await interaction.response.send_message(embed=embed)
            logger.info("Bump monitoring disabled: guild=%s", guild_id)
        else:
            embed = discord.Embed(
                title="Bump 監視",
                description="bump 監視は既に無効になっています。",
                color=DEFAULT_EMBED_COLOR,
            )
            embed.set_footer(text="Bump リマインダー")
            await interaction.response.send_message(embed=embed)

    @bump_group.command(
        name="sync-from-history",
        description="監視チャンネル履歴から前回bumpを判定して次回通知を設定する",
    )
    async def bump_sync_from_history(self, interaction: discord.Interaction) -> None:
        """監視チャンネル履歴から前回 bump を検出して次回通知を設定する。"""
        if not interaction.guild:
            await interaction.response.send_message(
                "このコマンドはサーバー内でのみ使用できます。", ephemeral=True
            )
            return

        await interaction.response.defer(ephemeral=True)

        guild_id = str(interaction.guild.id)
        async with async_session() as session:
            config = await get_bump_config(session, guild_id)

        if not config:
            await interaction.followup.send(
                "bump 監視設定がありません。先に `/bump setup` を実行してください。",
                ephemeral=True,
            )
            return

        ok, message = await self._sync_next_reminder_from_history(
            interaction.guild, config.channel_id
        )
        if ok:
            logger.info(
                "Synced reminder from history: guild=%s channel=%s",
                guild_id,
                config.channel_id,
            )
        else:
            logger.info(
                "History sync did not set reminder: guild=%s channel=%s reason=%s",
                guild_id,
                config.channel_id,
                message,
            )
        await interaction.followup.send(message, ephemeral=True)


# BumpReminder の型ヒント用 (circular import 回避)
from src.database.models import BumpReminder  # noqa: E402, F401


async def setup(bot: commands.Bot) -> None:
    """Cog を Bot に登録する関数。bot.load_extension() から呼ばれる。"""
    # 永続 View の登録 (Bot 再起動後もボタンが動作するように)
    # 注: 実際のデータは DB から取得するため、ここではダミーの View を登録
    # discord.py は custom_id のプレフィックスでマッチングする
    for service in BUMP_SERVICES:
        bot.add_view(BumpNotificationView("0", service.name, True))

    cog = BumpCog(bot)
    await bot.add_cog(cog)

    # bump 設定済みギルド ID のキャッシュを構築
    try:
        async with async_session() as session:
            from src.services.bump_service import get_all_bump_configs

            configs = await get_all_bump_configs(session)
            cog._bump_guild_ids = {c.guild_id for c in configs}
        logger.info("Bump guild cache loaded (%d guild(s))", len(cog._bump_guild_ids))
    except Exception:
        logger.critical("Failed to load bump guild cache", exc_info=True)
