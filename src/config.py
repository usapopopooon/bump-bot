"""Configuration settings using pydantic-settings.

pydantic-settings を使い、.env ファイルや環境変数から設定値を読み込む。
Bot トークンや DB 接続先など、環境ごとに異なる値をここで一元管理する。

Examples:
    基本的な使い方::

        from src.config import settings

        # Discord トークンを取得
        token = settings.discord_token

        # 非同期 DB URL を取得 (SQLAlchemy 用)
        db_url = settings.async_database_url

    環境変数での設定::

        # .env ファイルまたは環境変数で設定
        DISCORD_TOKEN=your-bot-token
        DATABASE_URL=postgresql://user:pass@localhost/db
See Also:
    - pydantic-settings: https://docs.pydantic.dev/latest/concepts/pydantic_settings/
    - src.constants: デフォルト値の定義
"""

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.constants import DEFAULT_DATABASE_URL


class Settings(BaseSettings):
    """アプリケーション設定クラス。

    pydantic-settings の BaseSettings を継承し、環境変数や .env ファイルから
    設定値を自動的に読み込む。フィールド名を大文字にした環境変数が
    自動的にマッピングされる (例: discord_token → DISCORD_TOKEN)。

    Attributes:
        discord_token (str): Discord Bot のトークン。必須。
            Discord Developer Portal で取得する。
        database_url (str): データベース接続 URL。
            デフォルトはローカル PostgreSQL。
        admin_email (str): Web 管理画面の初期管理者メールアドレス。
        admin_password (str): Web 管理画面の初期管理者パスワード。
        bump_channel_id (int): bump リマインダー用チャンネル ID。
            0 の場合は機能無効。
        smtp_host (str): SMTP サーバーのホスト名。
        smtp_port (int): SMTP サーバーのポート番号。
        smtp_user (str): SMTP 認証用ユーザー名。
        smtp_password (str): SMTP 認証用パスワード。
        smtp_from_email (str): メール送信元アドレス。
        smtp_use_tls (bool): TLS を使用するかどうか。
        app_url (str): アプリの URL (パスワードリセットリンク用)。

    Examples:
        設定値へのアクセス::

            from src.config import settings

            # Bot トークンを取得
            print(settings.discord_token)

            # SMTP が有効か確認
            if settings.smtp_enabled:
                send_email(...)

    See Also:
        - :class:`pydantic_settings.BaseSettings`: 基底クラス
        - :mod:`src.database.engine`: データベース接続設定
    """

    # --- pydantic-settings の設定 ---
    # env_file: 読み込む .env ファイルのパス
    # env_file_encoding: .env ファイルのエンコーディング
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
    )

    # --- 必須フィールド ---
    # Discord Bot のトークン。Discord Developer Portal で取得する
    # デフォルト値 "" を設定し、バリデータで明確なエラーメッセージを出す
    discord_token: str = ""

    @model_validator(mode="after")
    def validate_required_fields(self) -> "Settings":
        """必須フィールドのバリデーションを行う。

        pydantic の model_validator デコレータにより、モデル作成後に
        自動的に呼び出される。必須フィールドが設定されていない場合は
        明確なエラーメッセージを提供する。

        Returns:
            Settings: バリデーション済みの自身のインスタンス。

        Raises:
            ValueError: discord_token が空または空白のみの場合。
                エラーメッセージには Discord Developer Portal の URL が含まれる。

        Notes:
            - 空白のみのトークンも無効として扱う
            - pydantic のデフォルトエラーよりも分かりやすいメッセージを提供

        Examples:
            バリデーションエラーの例::

                # DISCORD_TOKEN が未設定の場合
                >>> settings = Settings()  # doctest: +SKIP
                ValueError: DISCORD_TOKEN environment variable is required...
        """
        # 空白のみのトークンも無効として扱う
        if not self.discord_token or not self.discord_token.strip():
            raise ValueError(
                "DISCORD_TOKEN environment variable is required. "
                "Get your bot token from the Discord Developer Portal: "
                "https://discord.com/developers/applications"
            )
        return self

    # --- オプションフィールド (デフォルト値あり) ---
    # データベース接続URL。デフォルトはローカルの PostgreSQL
    database_url: str = DEFAULT_DATABASE_URL

    # Web 管理画面の初期管理者メールアドレス
    admin_email: str = "admin@example.com"

    # Web 管理画面の初期管理者パスワード
    admin_password: str = "changeme"

    # bump リマインダー用チャンネルの ID
    # 0 の場合は bump リマインダー機能を無効化
    bump_channel_id: int = 0

    # --- SMTP 設定 (パスワードリセット用) ---
    # SMTP サーバーのホスト名
    smtp_host: str = ""

    # SMTP サーバーのポート番号 (587: TLS, 465: SSL, 25: 非暗号化)
    smtp_port: int = 587

    # SMTP 認証用ユーザー名
    smtp_user: str = ""

    # SMTP 認証用パスワード
    smtp_password: str = ""

    # メール送信元アドレス
    smtp_from_email: str = ""

    # TLS を使用するかどうか
    smtp_use_tls: bool = True

    # アプリの URL (パスワードリセットリンク用)
    app_url: str = "http://localhost:8000"

    # フロントエンドのベース URL (Discord ログ内リンク用)
    # 未設定時は app_url をフォールバックとして使用
    frontend_url: str = ""

    # タイムゾーンオフセット (UTC からの時差。例: 9 = JST, -5 = EST)
    timezone_offset: int = 9

    @property
    def smtp_enabled(self) -> bool:
        """SMTP が設定されているかどうかを判定する。

        smtp_host が設定されていれば SMTP 機能が有効とみなす。

        Returns:
            bool: SMTP が有効なら True、無効なら False。

        Examples:
            SMTP の有効性チェック::

                if settings.smtp_enabled:
                    await send_password_reset_email(email)
                else:
                    logger.warning("SMTP is not configured")

        See Also:
            - :attr:`smtp_auth_required`: 認証が必要かどうか
        """
        return bool(self.smtp_host)

    @property
    def ticket_web_base_url(self) -> str:
        """チケットログの Web URL ベースを返す。

        Returns:
            str: FRONTEND_URL が設定されていればその値、未設定なら app_url。
        """
        return self.frontend_url or self.app_url

    @property
    def smtp_auth_required(self) -> bool:
        """SMTP 認証が必要かどうかを判定する。

        smtp_user と smtp_password の両方が設定されていれば認証が必要。

        Returns:
            bool: 認証が必要なら True、不要なら False。

        Notes:
            認証不要の SMTP サーバー (ローカル開発用など) にも対応するため、
            ユーザー名とパスワードが両方揃っている場合のみ True を返す。

        See Also:
            - :attr:`smtp_enabled`: SMTP が有効かどうか
        """
        return bool(self.smtp_user and self.smtp_password)

    @property
    def async_database_url(self) -> str:
        """DATABASE_URL を非同期ドライバ対応の形式に変換する。

        Heroku は postgres:// で始まる URL を提供するが、
        SQLAlchemy の非同期エンジンは postgresql+asyncpg:// を要求する。
        このプロパティで自動的に変換する。

        Returns:
            str: asyncpg ドライバを使用する形式の接続 URL。

        Notes:
            変換パターン:

            - ``postgres://...`` → ``postgresql+asyncpg://...`` (Heroku 形式)
            - ``postgresql://...`` → ``postgresql+asyncpg://...`` (標準 PostgreSQL)
            - ``postgresql+asyncpg://...`` → そのまま (既に非同期形式)

        Examples:
            URL 変換の例::

                # Heroku 形式
                settings.database_url = "postgres://user:pass@host/db"
                assert settings.async_database_url == "postgresql+asyncpg://user:pass@host/db"

                # 標準 PostgreSQL 形式
                settings.database_url = "postgresql://user:pass@host/db"
                assert settings.async_database_url == "postgresql+asyncpg://user:pass@host/db"

        See Also:
            - :mod:`src.database.engine`: この URL を使用してエンジンを作成
            - asyncpg: https://github.com/MagicStack/asyncpg
        """
        url = self.database_url
        # 既に asyncpg ドライバが指定されている場合はそのまま返す
        if url.startswith("postgresql+asyncpg://"):
            return url
        # Heroku は postgres:// を使うが、SQLAlchemy は postgresql+asyncpg:// が必要
        if url.startswith("postgres://"):
            url = url.replace("postgres://", "postgresql+asyncpg://", 1)
        elif url.startswith("postgresql://"):
            url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return url


#: アプリケーション全体で共有する設定インスタンス。
#:
#: モジュール読み込み時にインスタンスを1つだけ作成し、アプリ全体で共有する。
#: pydantic-settings が環境変数から値を自動的に注入する。
#:
#: Examples:
#:     設定値の使用::
#:
#:         from src.config import settings
#:         print(settings.discord_token)
settings = Settings()
