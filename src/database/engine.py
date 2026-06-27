"""SQLAlchemy async engine setup.

非同期データベースエンジンとセッションファクトリを作成する。
アプリ全体で共有する DB 接続の設定をここで一元管理する。

用語:
    - engine: DB への接続を管理するオブジェクト (コネクションプール)
    - session: 1回の DB 操作単位。クエリの実行・コミット・ロールバックを行う
    - sessionmaker: セッションを生成するファクトリ (工場)

Examples:
    基本的な使い方::

        from src.database.engine import async_session, init_db

        # テーブルを初期化
        await init_db()

        # セッションを使用してクエリを実行
        async with async_session() as session:
            result = await session.execute(select(User))
            users = result.scalars().all()

    接続確認::

        from src.database.engine import check_database_connection_with_retry

        if await check_database_connection_with_retry():
            print("Database connected!")
        else:
            print("Connection failed")

See Also:
    - :mod:`src.database.models`: テーブル定義
    - :mod:`src.services.common_service`: CRUD 操作関数
    - SQLAlchemy asyncio: https://docs.sqlalchemy.org/en/20/orm/extensions/asyncio.html

Notes:
    本番環境では以下の環境変数で DB 接続設定を調整できる:

    - DATABASE_REQUIRE_SSL=true: SSL 接続を有効化
    - DB_POOL_SIZE: コネクションプールサイズ (デフォルト: 1)
    - DB_MAX_OVERFLOW: オーバーフロー接続数 (デフォルト: 0)
"""

import asyncio
import logging
import os
import ssl
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.config import settings
from src.constants import DEFAULT_DB_MAX_OVERFLOW, DEFAULT_DB_POOL_SIZE
from src.database.models import (
    DEFAULT_BUMP_REMINDER_DELAY_MINUTES,
    DISBOARD_BUMP_REMINDER_DELAY_MINUTES,
    Base,
)

logger = logging.getLogger(__name__)

# 接続タイムアウト (秒)
CONNECTION_TIMEOUT = 10

# リトライ設定
MAX_RETRIES = 3
RETRY_DELAY = 2  # 秒


def _parse_int_env(name: str, default: int) -> int:
    """環境変数を整数としてパースする。

    環境変数が設定されていない場合や、無効な値が設定されている場合は
    デフォルト値を返す。Linux 環境での設定ミスによるクラッシュを防ぐ。

    Args:
        name: 環境変数名
        default: デフォルト値

    Returns:
        パースした整数値、またはデフォルト値

    Examples:
        DB_POOL_SIZE=1 → 1
        DB_POOL_SIZE=abc → default (警告ログ出力)
        DB_POOL_SIZE=   → default (未設定扱い)
    """
    value = os.environ.get(name, "").strip()
    if not value:
        return default

    try:
        return int(value)
    except ValueError:
        logger.warning(
            "Invalid value for %s: '%s' (expected integer). Using default: %d",
            name,
            value,
            default,
        )
        return default


# --- コネクションプール設定 ---
# Heroku の基本プラン (mini/basic) は最大20接続
# 安全マージンを取って小さめに設定
POOL_SIZE = _parse_int_env("DB_POOL_SIZE", DEFAULT_DB_POOL_SIZE)
MAX_OVERFLOW = _parse_int_env("DB_MAX_OVERFLOW", DEFAULT_DB_MAX_OVERFLOW)

# --- SSL 設定 ---
# Heroku Postgres は SSL 接続を要求する
# DATABASE_URL に sslmode が含まれているか、環境変数で明示的に指定する
DATABASE_REQUIRE_SSL = os.environ.get("DATABASE_REQUIRE_SSL", "").lower() == "true"


def _get_connect_args() -> dict[str, Any]:
    """asyncpg ドライバ用の接続引数を取得する。

    環境変数 DATABASE_REQUIRE_SSL=true が設定されている場合、
    SSL コンテキストを含む接続引数を返す。Heroku Postgres など
    SSL 接続を要求するクラウドデータベース向け。

    Returns:
        dict[str, Any]: asyncpg 接続引数の辞書。
            SSL が有効な場合は {"ssl": SSLContext} を含む。
            SSL が無効な場合は空の辞書 {}。

    Notes:
        Heroku Postgres は自己署名証明書を使用するため、
        証明書の検証は無効化している (CERT_NONE)。

        - check_hostname=False: ホスト名の検証をスキップ
        - verify_mode=CERT_NONE: 証明書の検証をスキップ

    Examples:
        SSL なしの場合::

            # DATABASE_REQUIRE_SSL が未設定
            args = _get_connect_args()
            assert args == {}

        SSL ありの場合::

            # DATABASE_REQUIRE_SSL=true が設定されている
            args = _get_connect_args()
            assert "ssl" in args
            assert isinstance(args["ssl"], ssl.SSLContext)

    See Also:
        - :data:`DATABASE_REQUIRE_SSL`: SSL 有効化フラグ
        - asyncpg SSL: https://magicstack.github.io/asyncpg/current/api/
    """
    connect_args: dict[str, Any] = {}

    if DATABASE_REQUIRE_SSL:
        # Heroku Postgres の証明書検証をスキップ (自己署名証明書のため)
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE
        connect_args["ssl"] = ssl_context
        logger.info("Database SSL enabled")

    return connect_args


# --- 非同期エンジンの作成 ---
# create_async_engine: 非同期で DB に接続するためのエンジンを作る
# echo=False: SQL 文をログに出力しない (True にするとデバッグ時に便利)
# pool_pre_ping=True: 接続プールの接続が有効かチェックする
engine = create_async_engine(
    settings.async_database_url,
    echo=False,
    pool_pre_ping=True,  # 接続前にpingして無効な接続を検出
    pool_size=POOL_SIZE,
    max_overflow=MAX_OVERFLOW,
    connect_args=_get_connect_args(),
)

# --- セッションファクトリの作成 ---
# async_sessionmaker: セッション (DB 操作の単位) を生成するファクトリ
# expire_on_commit=False: commit 後もオブジェクトの属性にアクセスできるようにする
#   (False にしないと、commit 後に session.name 等を読むとエラーになる)
async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def init_db() -> None:
    """データベーステーブルを初期化する。

    SQLAlchemy の Base.metadata.create_all を使用して、
    定義されている全てのテーブルを作成する。テーブルが既に存在する場合は
    何もしない (CREATE TABLE IF NOT EXISTS に相当)。

    Returns:
        None

    Raises:
        sqlalchemy.exc.OperationalError: データベース接続に失敗した場合。
        sqlalchemy.exc.ProgrammingError: SQL 実行エラーが発生した場合。

    Notes:
        - 既存のテーブルスキーマは変更しない
        - スキーマ変更には Alembic マイグレーションを使用すること
        - 冪等性がある (何度呼んでも安全)

    Examples:
        Bot 起動時のテーブル初期化::

            from src.database.engine import init_db

            async def setup():
                await init_db()
                print("Tables created")

    See Also:
        - :class:`src.database.models.Base`: テーブル定義の基底クラス
        - Alembic: https://alembic.sqlalchemy.org/
    """
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await _ensure_bump_reminder_delay_column(conn)


async def _ensure_bump_reminder_delay_column(conn: Any) -> None:
    """既存DBにリマインド間隔カラムがない場合だけ追加する。"""
    await conn.execute(
        text("SELECT pg_advisory_xact_lock(hashtext('bump_reminder_delay_minutes'))")
    )
    result = await conn.execute(
        text(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.columns
                WHERE table_schema = current_schema()
                  AND table_name = 'bump_reminders'
                  AND column_name = 'reminder_delay_minutes'
            )
            """
        )
    )
    if result.scalar():
        return

    await conn.execute(
        text(
            "ALTER TABLE bump_reminders "
            "ADD COLUMN IF NOT EXISTS reminder_delay_minutes INTEGER NOT NULL DEFAULT "
            f"{DEFAULT_BUMP_REMINDER_DELAY_MINUTES}"
        )
    )
    await conn.execute(
        text(
            "UPDATE bump_reminders "
            "SET reminder_delay_minutes = :minutes "
            "WHERE service_name = 'DISBOARD'"
        ),
        {"minutes": DISBOARD_BUMP_REMINDER_DELAY_MINUTES},
    )


async def get_session() -> AsyncSession:
    """新しい非同期 DB セッションを取得する。

    AsyncSession インスタンスを生成して返す。通常は
    ``async with async_session() as session:`` の形で直接使用するため、
    この関数は主に依存注入パターンや特殊なケースで使用する。

    Returns:
        AsyncSession: 新しい非同期セッションインスタンス。
            呼び出し元でセッションのライフサイクルを管理する必要がある。

    Notes:
        - この関数で取得したセッションは自動でクローズされない
        - 通常は ``async with async_session() as session:`` を推奨
        - FastAPI の Depends() などで使用する場合に便利

    Examples:
        依存注入での使用 (FastAPI)::

            from fastapi import Depends
            from src.database.engine import get_session

            async def get_user(session: AsyncSession = Depends(get_session)):
                result = await session.execute(select(User))
                return result.scalars().first()

    See Also:
        - :data:`async_session`: セッションファクトリ
    """
    async with async_session() as session:
        return session


async def check_database_connection(
    timeout: float = CONNECTION_TIMEOUT,
    retries: int = 1,
    retry_delay: float = RETRY_DELAY,
) -> bool:
    """データベース接続を確認する。

    データベースに接続し、簡単なクエリ (SELECT 1) を実行して
    接続が正常かどうかを確認する。接続失敗時はリトライを行い、
    全て失敗した場合は False を返す。

    Args:
        timeout (float): 接続タイムアウト (秒)。デフォルトは CONNECTION_TIMEOUT (10秒)。
        retries (int): リトライ回数。1 = リトライなし。デフォルトは 1。
        retry_delay (float): リトライ間隔 (秒)。デフォルトは RETRY_DELAY (2秒)。

    Returns:
        bool: 接続成功なら True、全リトライ失敗なら False。

    Notes:
        - 接続 URL からホスト名を抽出してログに出力 (認証情報は隠蔽)
        - タイムアウトは asyncio.wait_for で実装
        - 各リトライの状況は WARNING レベルでログ出力

    Examples:
        基本的な接続確認::

            if await check_database_connection():
                print("Database is reachable")
            else:
                print("Database connection failed")

        リトライ付き接続確認::

            # 3回リトライ、各リトライ間に5秒待機
            success = await check_database_connection(
                timeout=15.0,
                retries=3,
                retry_delay=5.0,
            )

    See Also:
        - :func:`check_database_connection_with_retry`: リトライ付きの便利関数
        - :data:`CONNECTION_TIMEOUT`: デフォルトタイムアウト値
        - :data:`MAX_RETRIES`: デフォルトリトライ回数
    """

    async def _check() -> bool:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return True

    url = settings.database_url
    db_host = url.split("@")[-1] if "@" in url else "unknown"

    for attempt in range(1, retries + 1):
        try:
            await asyncio.wait_for(_check(), timeout=timeout)
            logger.info("Database connection successful")
            return True
        except TimeoutError:
            logger.warning(
                "Database connection timed out (attempt %d/%d) at %s",
                attempt,
                retries,
                db_host,
            )
        except Exception as e:
            # エラーログに認証情報を表示しない
            # 予期しない例外の場合はスタックトレースを出力
            logger.exception(
                "Failed to connect to database (attempt %d/%d) at %s: %s",
                attempt,
                retries,
                db_host,
                e,
            )

        # 最後のリトライでなければ待機
        if attempt < retries:
            logger.info("Retrying in %s seconds...", retry_delay)
            await asyncio.sleep(retry_delay)

    logger.error("Database connection failed after %d attempts at %s", retries, db_host)
    return False


async def check_database_connection_with_retry() -> bool:
    """リトライ付きでデータベース接続を確認する。

    デプロイ直後など、データベースがまだ起動中の場合に備えて
    MAX_RETRIES 回のリトライを行う。Bot 起動時のヘルスチェックに使用。

    Returns:
        bool: 接続成功なら True、全リトライ失敗なら False。

    Notes:
        - MAX_RETRIES (デフォルト: 3) 回リトライする
        - RETRY_DELAY (デフォルト: 2秒) 間隔でリトライ
        - Heroku など、dyno 起動と DB 起動のタイミングがずれる環境向け

    Examples:
        Bot 起動時の使用例::

            from src.database.engine import check_database_connection_with_retry

            async def main():
                if not await check_database_connection_with_retry():
                    logger.error("Database connection failed")
                    sys.exit(1)
                # Bot 起動処理...

    See Also:
        - :func:`check_database_connection`: 基本の接続確認関数
        - :data:`MAX_RETRIES`: リトライ回数定数
    """
    return await check_database_connection(retries=MAX_RETRIES)
