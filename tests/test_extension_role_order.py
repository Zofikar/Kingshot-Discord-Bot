import asyncio
from types import SimpleNamespace

import pytest

from extension.cogs import identity


class Role:
    def __init__(self, role_id, position):
        self.id = role_id
        self.position = position


@pytest.mark.parametrize("initial", [
    [0, 1, 2, 3, 4, 5],
    [0, 4, 1, 3, 2, 5],
    [0, 2, 1, 3, 4, 5],
])
def test_alliance_block_sticks_to_anchor(monkeypatch, initial):
    roles = {role_id: Role(role_id, position)
             for position, role_id in enumerate(initial)}
    calls = []

    class Guild:
        @property
        def roles(self):
            return sorted(roles.values(), key=lambda role: role.position)

        def get_role(self, role_id):
            return roles.get(role_id)

        async def edit_role_positions(self, *, positions):
            calls.append(positions)
            for role, position in positions.items():
                role.position = position

    bot = SimpleNamespace(extension_settings=SimpleNamespace(
        get_int=lambda key: 2))
    monkeypatch.setattr(identity.storage, "all_alliances", lambda: [
        {"role_id": 1, "power": 100},
        {"role_id": 4, "power": 200},
    ])
    guild = Guild()
    asyncio.run(identity._reorder_alliance_roles(bot, guild))
    expected = [role_id for role_id in initial if role_id not in (1, 4)]
    anchor_index = expected.index(2)
    expected[anchor_index:anchor_index] = [1, 4]
    assert [role.id for role in guild.roles] == expected
    asyncio.run(identity._reorder_alliance_roles(bot, guild))
    assert len(calls) == 1


def test_reconcile_on_ready_and_position_change(monkeypatch):
    guild = object()
    bot = SimpleNamespace(guilds=[guild])
    calls = []

    async def reorder(bot, target):
        calls.append(target)

    monkeypatch.setattr(identity, "_reorder_alliance_roles", reorder)
    cog = identity.IdentityBase(bot)
    asyncio.run(cog.on_ready())
    asyncio.run(cog.on_guild_role_update(
        SimpleNamespace(position=1), SimpleNamespace(position=2, guild=guild)))
    asyncio.run(cog.on_guild_role_update(
        SimpleNamespace(position=2), SimpleNamespace(position=2, guild=guild)))
    assert calls == [guild, guild]