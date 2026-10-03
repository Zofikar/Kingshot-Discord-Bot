import asyncio
from types import SimpleNamespace

from extension.cogs.identity import IdentityMightpulse
from extension.tasks import nightly as nightly_mod


def test_identity_exposes_verify_and_register_alias():
    names = {command.name for command in IdentityMightpulse.__cog_app_commands__}
    assert "verify" in names
    assert "register" in names


def test_nightly_refreshes_player_name_and_syncs_member(monkeypatch):
    member = SimpleNamespace(id=42)

    class Guild:
        id = 99

        def get_member(self, discord_id):
            return member if discord_id == 42 else None

        async def fetch_member(self, discord_id):
            return None

    bot = SimpleNamespace(guilds=[Guild()], maintenance_lock=asyncio.Lock())
    cog = nightly_mod.NightlyTasks(bot)
    registered = []
    synced = []

    monkeypatch.setattr(
        nightly_mod.storage, "linked_main_accounts", lambda: [(123, 42, 99)])

    async def fetch_player(fid, **kwargs):
        assert fid == 123
        assert kwargs == {"force_refresh": False, "bypass_local_cache": True}
        return {
            "nick_name": "New Name", "kid": 2464, "power": 100,
            "alliance": {"aid": 7, "abbr": "TAG", "name": "Alliance", "rank": 4},
        }

    monkeypatch.setattr(nightly_mod.mightpulse, "fetch_player_data", fetch_player)
    monkeypatch.setattr(nightly_mod.storage, "register_player",
                        lambda **fields: registered.append(fields))

    from extension.cogs import identity as identity_mod

    async def sync_member(_bot, target):
        synced.append(target)

    monkeypatch.setattr(identity_mod, "sync_member", sync_member)

    stats = asyncio.run(cog._sync_linked_players())

    assert stats == {"synced": 1, "skipped": 0}
    assert registered[0]["nickname"] == "New Name"
    assert registered[0]["discord_id"] == 42
    assert synced == [member]