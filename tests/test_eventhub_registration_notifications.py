import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import cogs.eventhub as eventhub


class _FakeRpc:
    async def execute(self):
        return SimpleNamespace(data=[{"result": "registered"}])


class _FakeClient:
    def rpc(self, *_args, **_kwargs):
        return _FakeRpc()


def _interaction() -> SimpleNamespace:
    return SimpleNamespace(
        guild=SimpleNamespace(id=123),
        user=SimpleNamespace(id=456),
        client=SimpleNamespace(db=SimpleNamespace(get_client=AsyncMock(return_value=_FakeClient()))),
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_response=AsyncMock(),
    )


def test_direct_registration_notifies_admin_channel(monkeypatch) -> None:
    async def run_test() -> None:
        notifications = []

        async def notify(_bot, _client, event_id, guild, user):
            notifications.append((event_id, guild.id, user.id))

        monkeypatch.setattr(eventhub, "_notify_admin_registration", notify)
        monkeypatch.setattr(eventhub, "_grant_event_role", AsyncMock(return_value=None))
        monkeypatch.setattr(eventhub, "_refresh_embed", AsyncMock())

        interaction = _interaction()
        view = eventhub._ConfirmView(42)
        await view.children[0].callback(interaction)

        assert notifications == [(42, 123, 456)]

    asyncio.run(run_test())


def test_form_registration_notifies_admin_channel(monkeypatch) -> None:
    async def run_test() -> None:
        notifications = []

        async def notify(_bot, _client, event_id, guild, user):
            notifications.append((event_id, guild.id, user.id))

        monkeypatch.setattr(eventhub, "_notify_admin_registration", notify)
        monkeypatch.setattr(eventhub, "_grant_event_role", AsyncMock(return_value=None))

        interaction = _interaction()
        modal = eventhub.FormModal(42, [])
        await modal.on_submit(interaction)

        assert notifications == [(42, 123, 456)]

    asyncio.run(run_test())
