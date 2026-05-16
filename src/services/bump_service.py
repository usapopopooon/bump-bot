"""BumpReminder, BumpConfig の DB 操作。"""

from datetime import datetime

from sqlalchemy import delete, select, update
from sqlalchemy import or_ as db_or
from sqlalchemy.ext.asyncio import AsyncSession

from src.database.models import BumpConfig, BumpReminder

__all__ = [
    "claim_bump_detection",
    "clear_bump_reminder",
    "delete_bump_config",
    "delete_bump_reminders_by_guild",
    "get_all_bump_configs",
    "get_bump_config",
    "get_bump_reminder",
    "get_due_bump_reminders",
    "toggle_bump_reminder",
    "update_bump_reminder_role",
    "upsert_bump_config",
    "upsert_bump_reminder",
]


# =============================================================================
# BumpReminder (bump リマインダー) 操作
# =============================================================================


async def upsert_bump_reminder(
    session: AsyncSession,
    guild_id: str,
    channel_id: str,
    service_name: str,
    remind_at: datetime,
) -> BumpReminder:
    """bump リマインダーを作成または更新する。

    同じ guild_id + service_name の組み合わせが既に存在する場合は上書きする。
    UPSERT (INSERT or UPDATE) パターンを実装。

    Args:
        session (AsyncSession): DB セッション。
        guild_id (str): Discord サーバーの ID。
        channel_id (str): リマインド通知を送信するチャンネルの ID。
        service_name (str): サービス名 ("DISBOARD" または "ディス速報")。
        remind_at (datetime): リマインドを送信する予定時刻 (UTC)。

    Returns:
        BumpReminder: 作成または更新された BumpReminder オブジェクト。

    Notes:
        - 既存レコードがある場合は channel_id と remind_at のみ更新
        - is_enabled と role_id は既存の値を保持
        - commit() を内部で呼び出す

    Examples:
        bump 検知後のリマインダー設定::

            from datetime import UTC, datetime, timedelta

            async with async_session() as session:
                remind_at = datetime.now(UTC) + timedelta(hours=2)
                reminder = await upsert_bump_reminder(
                    session,
                    guild_id=str(guild.id),
                    channel_id=str(channel.id),
                    service_name="DISBOARD",
                    remind_at=remind_at,
                )

    See Also:
        - :func:`get_due_bump_reminders`: 期限切れリマインダー取得
        - :func:`clear_bump_reminder`: リマインダーのクリア
        - :class:`src.database.models.BumpReminder`: リマインダーモデル
    """
    # 既存のレコードを検索
    result = await session.execute(
        select(BumpReminder).where(
            BumpReminder.guild_id == guild_id,
            BumpReminder.service_name == service_name,
        )
    )
    existing = result.scalar_one_or_none()

    if existing:
        # 既存レコードを更新
        existing.channel_id = channel_id
        existing.remind_at = remind_at
        await session.commit()
        return existing

    # 新規作成
    reminder = BumpReminder(
        guild_id=guild_id,
        channel_id=channel_id,
        service_name=service_name,
        remind_at=remind_at,
    )
    session.add(reminder)
    await session.commit()
    await session.refresh(reminder)
    return reminder


async def get_due_bump_reminders(
    session: AsyncSession,
    now: datetime,
) -> list[BumpReminder]:
    """送信予定時刻を過ぎた有効な bump リマインダーを取得する。

    Args:
        session: DB セッション
        now: 現在時刻 (UTC)

    Returns:
        remind_at <= now かつ is_enabled = True の BumpReminder のリスト
    """
    result = await session.execute(
        select(BumpReminder).where(
            BumpReminder.remind_at <= now,
            BumpReminder.remind_at.isnot(None),
            BumpReminder.is_enabled.is_(True),
        )
    )
    return list(result.scalars().all())


async def clear_bump_reminder(session: AsyncSession, reminder_id: int) -> bool:
    """bump リマインダーの remind_at をアトミックにクリアする。

    リマインド送信前に呼ばれる。レコードは削除せず、remind_at を None にする。
    複数インスタンス実行時、最初にクリアしたインスタンスのみ True を返す。

    Args:
        session: DB セッション
        reminder_id: クリアするリマインダーの ID

    Returns:
        クリアできたら True、既にクリア済みなら False
    """
    result = await session.execute(
        update(BumpReminder)
        .where(
            BumpReminder.id == reminder_id,
            BumpReminder.remind_at.isnot(None),
        )
        .values(remind_at=None)
    )
    await session.commit()
    return bool(result.rowcount)  # type: ignore[attr-defined]


async def claim_bump_detection(
    session: AsyncSession,
    guild_id: str,
    channel_id: str,
    service_name: str,
    remind_at: datetime,
) -> BumpReminder | None:
    """bump 検知の権利をアトミックに取得する。

    複数インスタンス実行時、最初に claim したインスタンスだけがリマインダーを返す。
    remind_at が直近 60 秒以内に更新済みの場合は None を返す。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID
        channel_id: bump を検知したチャンネルの ID
        service_name: サービス名 ("DISBOARD" または "ディス速報")
        remind_at: リマインド予定時刻 (UTC)

    Returns:
        claim 成功なら BumpReminder、既に別インスタンスが処理済みなら None
    """
    from datetime import timedelta

    threshold = remind_at - timedelta(seconds=60)

    # アトミック UPDATE: remind_at が古い or NULL の場合のみ成功する
    result = await session.execute(
        update(BumpReminder)
        .where(
            BumpReminder.guild_id == guild_id,
            BumpReminder.service_name == service_name,
            db_or(
                BumpReminder.remind_at.is_(None),
                BumpReminder.remind_at <= threshold,
            ),
        )
        .values(remind_at=remind_at, channel_id=channel_id)
    )
    await session.commit()

    if result.rowcount > 0:  # type: ignore[attr-defined]
        # claim 成功 — is_enabled/role_id を取得して返す
        return await get_bump_reminder(session, guild_id, service_name)

    # レコードが存在するが直近で更新済み (別インスタンスが先に処理)
    existing = await get_bump_reminder(session, guild_id, service_name)
    if existing is not None:
        return None

    # レコードが存在しない (初回検知) — 新規作成
    reminder = BumpReminder(
        guild_id=guild_id,
        channel_id=channel_id,
        service_name=service_name,
        remind_at=remind_at,
    )
    session.add(reminder)
    await session.commit()
    await session.refresh(reminder)
    return reminder


async def get_bump_reminder(
    session: AsyncSession,
    guild_id: str,
    service_name: str,
) -> BumpReminder | None:
    """guild_id と service_name で bump リマインダーを取得する。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID
        service_name: サービス名 ("DISBOARD" または "ディス速報")

    Returns:
        見つかった BumpReminder、なければ None
    """
    result = await session.execute(
        select(BumpReminder).where(
            BumpReminder.guild_id == guild_id,
            BumpReminder.service_name == service_name,
        )
    )
    return result.scalar_one_or_none()


async def toggle_bump_reminder(
    session: AsyncSession,
    guild_id: str,
    service_name: str,
) -> bool:
    """bump リマインダーの有効/無効を切り替える。

    レコードが存在しない場合は無効状態で新規作成する。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID
        service_name: サービス名 ("DISBOARD" または "ディス速報")

    Returns:
        切り替え後の is_enabled 値
    """
    reminder = await get_bump_reminder(session, guild_id, service_name)

    if reminder:
        reminder.is_enabled = not reminder.is_enabled
        await session.commit()
        return reminder.is_enabled

    # レコードがない場合は無効状態で新規作成
    new_reminder = BumpReminder(
        guild_id=guild_id,
        channel_id="",  # 通知先は bump 検知時に設定される
        service_name=service_name,
        remind_at=None,
        is_enabled=False,
    )
    session.add(new_reminder)
    await session.commit()
    return False


async def update_bump_reminder_role(
    session: AsyncSession,
    guild_id: str,
    service_name: str,
    role_id: str | None,
) -> bool:
    """bump リマインダーの通知ロールを更新する。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID
        service_name: サービス名 ("DISBOARD" または "ディス速報")
        role_id: 新しい通知ロールの ID (None ならデフォルトロールに戻す)

    Returns:
        更新できたら True、レコードが見つからなければ False
    """
    reminder = await get_bump_reminder(session, guild_id, service_name)

    if reminder:
        reminder.role_id = role_id
        await session.commit()
        return True

    return False


# =============================================================================
# BumpConfig (bump 監視設定) 操作
# =============================================================================


async def get_bump_config(
    session: AsyncSession,
    guild_id: str,
) -> BumpConfig | None:
    """ギルドの bump 監視設定を取得する。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID

    Returns:
        見つかった BumpConfig、なければ None
    """
    result = await session.execute(
        select(BumpConfig).where(BumpConfig.guild_id == guild_id)
    )
    return result.scalar_one_or_none()


async def upsert_bump_config(
    session: AsyncSession,
    guild_id: str,
    channel_id: str,
) -> BumpConfig:
    """bump 監視設定を作成または更新する。

    既に設定がある場合はチャンネル ID を上書きする。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID
        channel_id: bump を監視するチャンネルの ID

    Returns:
        作成または更新された BumpConfig オブジェクト
    """
    existing = await get_bump_config(session, guild_id)

    if existing:
        existing.channel_id = channel_id
        await session.commit()
        return existing

    config = BumpConfig(guild_id=guild_id, channel_id=channel_id)
    session.add(config)
    await session.commit()
    await session.refresh(config)
    return config


async def delete_bump_config(
    session: AsyncSession,
    guild_id: str,
) -> bool:
    """bump 監視設定を削除する。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID

    Returns:
        削除できたら True、見つからなければ False
    """
    config = await get_bump_config(session, guild_id)

    if config:
        await session.delete(config)
        await session.commit()
        return True

    return False


async def delete_bump_reminders_by_guild(session: AsyncSession, guild_id: str) -> int:
    """指定ギルドの全 bump リマインダーを削除する。

    Bot がギルドから退出したときにクリーンアップとして使用。

    Args:
        session: DB セッション
        guild_id: Discord サーバーの ID

    Returns:
        削除したリマインダーの数
    """
    result = await session.execute(
        delete(BumpReminder).where(BumpReminder.guild_id == guild_id)
    )
    await session.commit()
    return int(result.rowcount)  # type: ignore[attr-defined]


async def get_all_bump_configs(session: AsyncSession) -> list[BumpConfig]:
    """全ての bump 設定を取得する。

    Args:
        session: DB セッション

    Returns:
        全 bump 設定のリスト
    """
    result = await session.execute(select(BumpConfig))
    return list(result.scalars().all())
