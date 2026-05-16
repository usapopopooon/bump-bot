"""Bump reminder cog for DISBOARD and ディス速報.

DISBOARD/ディス速報の bump 成功を検知し、2時間後にリマインドを送信する。

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

# リマインダーの送信間隔 (bump から何時間後か)
REMINDER_HOURS = 2

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

        # ギルド・サービスごとのロックで並行リクエストをシリアライズ
        async with get_resource_lock(
            f"bump_notification:{self.guild_id}:{self.service_name}"
        ):
            async with async_session() as session:
                await update_bump_reminder_role(
                    session, self.guild_id, self.service_name, str(selected_role.id)
                )

            await interaction.response.edit_message(
                content=f"通知先ロールを **{selected_role.name}** に変更しました。",
                view=None,
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
                await update_bump_reminder_role(session, guild_id, service_name, None)

            msg = f"通知先ロールを **{TARGET_ROLE_NAME}** (デフォルト) に戻しました。"
            await interaction.response.edit_message(content=msg, view=None)
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
            await interaction.response.send_message(
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

        # DISBOARD/ディス速報 Bot 以外は無視 (DEBUG_USER_ID はテスト用)
        if message.author.id not in (DISBOARD_BOT_ID, DISSOKU_BOT_ID, DEBUG_USER_ID):
            return

        guild_id = str(message.guild.id)

        # インメモリキャッシュで高速フィルタリング (DB アクセスゼロ)
        if self._bump_guild_ids is not None and guild_id not in self._bump_guild_ids:
            return

        bot_name = "DISBOARD" if message.author.id == DISBOARD_BOT_ID else "ディス速報"
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
        # members intent 無効時は User が来るため、ロールチェックはスキップする。
        if isinstance(user, discord.Member) and not self._has_target_role(user):
            logger.info(
                "User does not have required role, skipping reminder: "
                "user=%s required_role=%s guild=%s",
                user.name,
                TARGET_ROLE_NAME,
                guild_id,
            )
            return

        # 1セッションで設定確認 + アトミックなリマインダー登録
        remind_at = datetime.now(UTC) + timedelta(hours=REMINDER_HOURS)
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
            サービス名 ("DISBOARD" or "ディス速報")。検知できなければ None
        """
        is_disboard = message.author.id == DISBOARD_BOT_ID
        is_dissoku = message.author.id == DISSOKU_BOT_ID

        for embed in message.embeds:
            description = embed.description or ""
            title = embed.title or ""
            fields = embed.fields or []

            # embed 内容をログ出力
            fields_summary = [
                {"name": f.name, "value": f.value[:80] if f.value else ""}
                for f in fields
            ]
            logger.info(
                "Parsing embed: bot=%s title=%s description=%s fields=%s",
                "DISBOARD" if is_disboard else "ディス速報",
                title[:80] if title else None,
                description[:80] if description else None,
                fields_summary,
            )

            # DISBOARD: description に「表示順をアップ」
            if is_disboard and DISBOARD_SUCCESS_KEYWORD in description:
                return "DISBOARD"

            # ディス速報: title, description, fields のいずれかに「アップ」
            if is_dissoku:
                if DISSOKU_SUCCESS_KEYWORD in title:
                    return "ディス速報"
                if DISSOKU_SUCCESS_KEYWORD in description:
                    return "ディス速報"
                for field in fields:
                    if DISSOKU_SUCCESS_KEYWORD in (field.name or ""):
                        return "ディス速報"
                    if DISSOKU_SUCCESS_KEYWORD in (field.value or ""):
                        return "ディス速報"

        # ディス速報: message.content に「アップ」
        if (
            is_dissoku
            and message.content
            and DISSOKU_SUCCESS_KEYWORD in message.content
        ):
            return "ディス速報"

        # 検知できなかった
        logger.info(
            "Bump success keyword not found: bot=%s keyword=%s",
            "DISBOARD" if is_disboard else "ディス速報",
            DISBOARD_SUCCESS_KEYWORD if is_disboard else DISSOKU_SUCCESS_KEYWORD,
        )
        return None

    def _get_bump_user(
        self, message: discord.Message
    ) -> discord.abc.User | discord.Member | None:
        """bump を実行したユーザーを取得する。

        message.interaction_metadata から取得を試み、失敗したら None を返す。
        """
        # スラッシュコマンドの場合、interaction_metadata.user に実行者がいる
        if message.interaction_metadata and message.interaction_metadata.user:
            user = message.interaction_metadata.user
            # Member でない場合でも User として利用可能
            if isinstance(user, discord.Member):
                return user
            return user
        return None

    def _has_target_role(self, member: discord.Member) -> bool:
        """メンバーが Server Bumper ロールを持っているか確認する。"""
        return any(role.name == TARGET_ROLE_NAME for role in member.roles)

    async def _find_recent_bump(
        self, channel: discord.TextChannel, limit: int = 100
    ) -> tuple[str, datetime] | None:
        """チャンネルの履歴から最近の bump 成功メッセージを探す。

        Args:
            channel: 検索対象のチャンネル
            limit: 検索するメッセージ数の上限

        Returns:
            (サービス名, bump時刻) のタプル。見つからなければ None
        """
        try:
            async for message in channel.history(limit=limit):
                # DISBOARD/ディス速報 Bot 以外は無視
                if message.author.id not in (DISBOARD_BOT_ID, DISSOKU_BOT_ID):
                    continue

                # bump 成功かどうかを判定
                service_name = self._detect_bump_success(message)
                if service_name:
                    return (service_name, message.created_at)

        except discord.HTTPException as e:
            logger.warning("Failed to search channel history: %s", e)

        return None

    # ==========================================================================
    # Embed 生成
    # ==========================================================================

    def _build_detection_embed(
        self,
        service_name: str,
        user: discord.abc.User | discord.Member,
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
                result = await self._find_recent_bump(channel)
                if result:
                    service_name, bump_time = result
                    detected_service = service_name
                    remind_at = bump_time + timedelta(hours=REMINDER_HOURS)
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
            "DISBOARD (`/bump`) または ディス速報 (`/dissoku up`) の "
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
            # 検出されなかった場合、両方のサービスのボタンを表示
            await interaction.followup.send(embed=embed)
            # DISBOARD 用
            view_disboard = BumpNotificationView(guild_id, "DISBOARD", True)
            self.bot.add_view(view_disboard)
            await interaction.followup.send(
                "**DISBOARD** の通知設定:", view=view_disboard
            )
            # ディス速報用
            view_dissoku = BumpNotificationView(guild_id, "ディス速報", True)
            self.bot.add_view(view_dissoku)
            await interaction.followup.send(
                "**ディス速報** の通知設定:", view=view_dissoku
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
            # 各サービスのリマインダー設定を取得
            disboard_reminder = await get_bump_reminder(session, guild_id, "DISBOARD")
            dissoku_reminder = await get_bump_reminder(session, guild_id, "ディス速報")

        if config:
            # Discord タイムスタンプ形式で設定日時を表示
            ts = int(config.created_at.timestamp())

            # 各サービスの通知先ロールを取得
            def get_role_display(
                reminder: BumpReminder | None, guild: discord.Guild
            ) -> str:
                if reminder and reminder.role_id:
                    role = guild.get_role(int(reminder.role_id))
                    if role:
                        return f"`@{role.name}`"
                return f"`@{TARGET_ROLE_NAME}` (デフォルト)"

            disboard_role = get_role_display(disboard_reminder, interaction.guild)
            dissoku_role = get_role_display(dissoku_reminder, interaction.guild)

            embed = discord.Embed(
                title="Bump 監視設定",
                description=(
                    f"**監視チャンネル:** <#{config.channel_id}>\n"
                    f"**設定日時:** <t:{ts}:F>\n\n"
                    f"**通知先ロール:**\n"
                    f"・DISBOARD: {disboard_role}\n"
                    f"・ディス速報: {dissoku_role}"
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


# BumpReminder の型ヒント用 (circular import 回避)
from src.database.models import BumpReminder  # noqa: E402, F401


async def setup(bot: commands.Bot) -> None:
    """Cog を Bot に登録する関数。bot.load_extension() から呼ばれる。"""
    # 永続 View の登録 (Bot 再起動後もボタンが動作するように)
    # 注: 実際のデータは DB から取得するため、ここではダミーの View を登録
    # discord.py は custom_id のプレフィックスでマッチングする
    bot.add_view(BumpNotificationView("0", "DISBOARD", True))
    bot.add_view(BumpNotificationView("0", "ディス速報", True))

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
