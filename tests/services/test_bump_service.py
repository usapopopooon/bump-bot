from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from src.database.models import BumpReminder
from src.services import bump_service


class DummySession:
    def __init__(self) -> None:
        self.add = self._add_sync
        self.commit = AsyncMock()
        self.added: list[object] = []

    def _add_sync(self, obj: object) -> None:
        self.added.append(obj)


@pytest.mark.asyncio
async def test_update_bump_reminder_role_updates_existing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = DummySession()
    reminder = BumpReminder(
        guild_id="1",
        channel_id="2",
        service_name="DISBOARD",
        remind_at=None,
        is_enabled=True,
        role_id=None,
    )

    monkeypatch.setattr(
        bump_service,
        "get_bump_reminder",
        AsyncMock(return_value=reminder),
    )

    updated = await bump_service.update_bump_reminder_role(
        session, "1", "DISBOARD", "999"
    )

    assert updated is True
    assert reminder.role_id == "999"
    session.commit.assert_awaited_once()
