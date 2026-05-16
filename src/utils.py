"""Shared utility functions."""

from __future__ import annotations

import asyncio
import re
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from functools import lru_cache

import emoji

from src.config import settings

# =============================================================================
# リソースロック管理 (並行処理の競合防止)
# =============================================================================

_resource_locks: dict[str, tuple[asyncio.Lock, float]] = {}
_LOCK_CLEANUP_INTERVAL = 600
_LOCK_EXPIRY_TIME = 300
_lock_last_cleanup_time = float("-inf")


def _cleanup_resource_locks() -> None:
    """古い未使用ロックを削除する."""
    global _lock_last_cleanup_time
    now = time.monotonic()

    if (
        _lock_last_cleanup_time > 0
        and now - _lock_last_cleanup_time < _LOCK_CLEANUP_INTERVAL
    ):
        return

    _lock_last_cleanup_time = now
    for key in list(_resource_locks):
        lock, last_access = _resource_locks[key]
        if now - last_access > _LOCK_EXPIRY_TIME and not lock.locked():
            del _resource_locks[key]


def get_resource_lock(resource_key: str) -> asyncio.Lock:
    """リソースキーに対応するロックを取得する."""
    _cleanup_resource_locks()
    now = time.monotonic()

    entry = _resource_locks.get(resource_key)
    if entry is None:
        lock = asyncio.Lock()
        _resource_locks[resource_key] = (lock, now)
        return lock

    _resource_locks[resource_key] = (entry[0], now)
    return entry[0]


def clear_resource_locks() -> None:
    """全てのリソースロックをクリアする (テスト用)."""
    global _lock_last_cleanup_time
    _resource_locks.clear()
    _lock_last_cleanup_time = float("-inf")


def get_resource_lock_count() -> int:
    """現在管理されているロックの数を返す (テスト/デバッグ用)."""
    return len(_resource_locks)


# Discord カスタム絵文字パターン: <:name:id> または <a:name:id> (アニメーション)
DISCORD_CUSTOM_EMOJI_PATTERN = re.compile(r"^<a?:\w+:\d+>$")

# 制御文字パターン (改行、タブ、キャリッジリターン等)
# C0/C1 制御文字と一部の特殊文字を含む
CONTROL_CHAR_PATTERN = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _has_lone_surrogate(text: str) -> bool:
    """壊れたサロゲートペアが含まれているかチェックする.

    Args:
        text: 検証する文字列

    Returns:
        壊れたサロゲートが含まれている場合True
    """
    # サロゲート範囲 U+D800-U+DFFF をビット演算で検出 (メモリ割り当てゼロ):
    # 上位 5 ビットが 11011 なら surrogate → (code point & 0xF800) == 0xD800
    return any((ord(c) & 0xF800) == 0xD800 for c in text)


def is_valid_emoji(text: str | None) -> bool:
    """絵文字として有効かどうかを検証する.

    Args:
        text: 検証する文字列

    Returns:
        Discordカスタム絵文字またはUnicode絵文字の場合True

    Note:
        emoji ライブラリを使用して、ZWJ シーケンス (🧑‍🧑‍🧒 等) や
        keycap 絵文字 (3️⃣ 等) を含む全ての Unicode 絵文字に対応。

        以下のケースは無効として拒否:
        - 空文字、None
        - 壊れたサロゲートペア
        - 制御文字 (改行、タブ等) を含む文字列
    """
    if not text:
        return False

    # Discord カスタム絵文字は高速パス: サロゲート/制御文字チェック不要
    # (フォーマットが固定 <:name:id> or <a:name:id> のため ASCII のみ)
    if text[0] == "<" and DISCORD_CUSTOM_EMOJI_PATTERN.match(text):
        return True

    # 壊れたサロゲートペア or 制御文字 → 無効 (短絡評価)
    if _has_lone_surrogate(text) or CONTROL_CHAR_PATTERN.search(text):
        return False

    # NFC 正規化して比較 (異体字セレクタ等の正規化)
    normalized = unicodedata.normalize("NFC", text)

    # Unicode 絵文字 (emoji ライブラリで検証)
    if emoji.is_emoji(normalized):
        return True

    # VS16 (U+FE0F) が含まれる場合のみ除去して再検証
    # (不要な str.replace 回避)
    if "\ufe0f" in normalized:
        stripped = normalized.replace("\ufe0f", "")
        return bool(stripped and emoji.is_emoji(stripped))

    return False


def normalize_emoji(text: str) -> str:
    """絵文字を正規化して返す.

    DB保存前に呼び出すことで、同じ見た目の絵文字が
    異なるバイト列で保存されることを防ぐ。

    Args:
        text: 正規化する絵文字文字列

    Returns:
        NFC正規化された絵文字文字列

    Note:
        Discord カスタム絵文字 (<:name:id>) はそのまま返す。
        Unicode 絵文字は NFC 正規化を適用。
    """
    if not text:
        return text

    # Discord カスタム絵文字はそのまま
    if DISCORD_CUSTOM_EMOJI_PATTERN.match(text):
        return text

    # NFC 正規化で統一的な保存形式にする
    normalized = unicodedata.normalize("NFC", text)

    # ZWJ シーケンス (👨‍👩‍👧, 🏃‍♀️ 等) は VS16 を保持する
    # Discord が fully-qualified 形式を要求するため、VS16 除去すると拒否される
    if "\u200d" in normalized:
        return normalized

    # 単純な絵文字のみ VS16 除去を試みる (⚓️ → ⚓)
    if "\ufe0f" in normalized:
        stripped = normalized.replace("\ufe0f", "")
        if stripped and emoji.is_emoji(stripped):
            return stripped

    return normalized


# =============================================================================
# 日時フォーマット (タイムゾーンオフセット対応)
# =============================================================================


@lru_cache(maxsize=4)
def _make_timezone(offset: int) -> timezone:
    """timezone オブジェクトをキャッシュして返す."""
    return timezone(timedelta(hours=offset))


_timezone_offset_override: int | None = None


def set_timezone_offset(offset: int) -> None:
    """DB から読み込んだタイムゾーンオフセットを設定する."""
    global _timezone_offset_override  # noqa: PLW0603
    _timezone_offset_override = offset


def get_timezone_offset() -> int:
    """現在のタイムゾーンオフセットを返す."""
    if _timezone_offset_override is not None:
        return _timezone_offset_override
    return settings.timezone_offset


def _get_timezone() -> timezone:
    """設定されたタイムゾーンオフセットから timezone オブジェクトを返す."""
    return _make_timezone(get_timezone_offset())


def format_datetime(
    dt: datetime | None,
    fmt: str = "%Y-%m-%d %H:%M",
    *,
    fallback: str = "-",
) -> str:
    """datetime を設定されたタイムゾーンでフォーマットする.

    Args:
        dt: フォーマット対象の datetime (None 可)
        fmt: strftime フォーマット文字列
        fallback: dt が None の場合の代替文字列

    Returns:
        フォーマット済み文字列
    """
    if dt is None:
        return fallback
    tz = _get_timezone()
    local_dt = dt.astimezone(tz)
    return local_dt.strftime(fmt)
