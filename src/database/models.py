"""SQLAlchemy models for bump reminder."""

from datetime import UTC, datetime

from sqlalchemy import Boolean, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

DEFAULT_BUMP_REMINDER_DELAY_MINUTES = 120
DISBOARD_BUMP_REMINDER_DELAY_MINUTES = 300


class Base(DeclarativeBase):
    """Base model class."""


class BumpReminder(Base):
    """Reminder per guild and service."""

    __tablename__ = "bump_reminders"
    __table_args__ = (
        UniqueConstraint("guild_id", "service_name", name="uq_guild_service"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    guild_id: Mapped[str] = mapped_column(String, nullable=False, index=True)
    channel_id: Mapped[str] = mapped_column(String, nullable=False)
    service_name: Mapped[str] = mapped_column(String, nullable=False)
    remind_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    role_id: Mapped[str | None] = mapped_column(String, nullable=True)
    reminder_delay_minutes: Mapped[int] = mapped_column(
        Integer,
        default=DEFAULT_BUMP_REMINDER_DELAY_MINUTES,
        server_default=str(DEFAULT_BUMP_REMINDER_DELAY_MINUTES),
        nullable=False,
    )


class BumpConfig(Base):
    """Bump watch target channel per guild."""

    __tablename__ = "bump_configs"

    guild_id: Mapped[str] = mapped_column(String, primary_key=True)
    channel_id: Mapped[str] = mapped_column(String, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
