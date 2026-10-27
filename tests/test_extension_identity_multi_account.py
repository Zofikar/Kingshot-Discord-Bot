"""Exercise identity commands with real temporary storage and mocked Discord/API I/O."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extension import storage
from extension.cogs import identity
from extension.settings import SettingsStore


@pytest.fixture
def accounts(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = SettingsStore()
    for key, value in {
        "verified_role_id": 10, "role_r4_id": 14,
        "role_r5_id": 15, "role_council_id": 16,
    }.items():
        settings.set(key, value)
    storage.upsert_alliance(7, abbr="AAA", name="Alpha", role_id=107, power=200)
    storage.upsert_alliance(8, abbr="BBB", name="Beta", role_id=108, power=100)
    players = {
        "100": {"nick_name": "Main", "kid": 2464, "power": 100,
                "alliance": {"aid": 7, "abbr": "AAA", "name": "Alpha", "rank": 4}},
        "200": {"nick_name": "Alt", "kid": 2464, "power": 50,
                "alliance": {"aid": 8, "abbr": "BBB", "name": "Beta", "rank": 5}},
    }

    async def fetch(gid, **kwargs):
        assert kwargs == {"force_refresh": True}
        return players[str(gid)]

    monkeypatch.setattr(identity.mightpulse, "fetch_player_data", fetch)
    applied = []

    async def apply(bot, member, nick, add, remove):
        applied.append((member.id, nick, set(add), set(remove)))

    monkeypatch.setattr(identity, "_apply", apply)
    bot = SimpleNamespace(extension_settings=settings)
    member = SimpleNamespace(id=42, mention="<@42>", guild=SimpleNamespace(
        get_role=lambda role_id: SimpleNamespace(
            id=role_id, name={107: "[AAA] Alpha", 108: "[BBB] Beta"}[role_id])))
    return SimpleNamespace(
        live=identity.IdentityMightpulse(bot), base=identity.IdentityBase(bot),
        member=member, applied=applied,
    )


def interaction(member):
    return SimpleNamespace(
        user=member, guild_id=99,
        response=SimpleNamespace(defer=AsyncMock()),
        followup=SimpleNamespace(send=AsyncMock()),
    )


def verify(accounts, fid, member=None):
    inter = interaction(member or accounts.member)
    asyncio.run(identity.IdentityMightpulse.verify.callback(accounts.live, inter, gid=str(fid)))
    inter.followup.send.assert_awaited_once_with(
        f"Linked and synced ID `{fid}`.", ephemeral=True)
    return inter


def test_verify_two_accounts_keeps_first_main_and_uses_only_its_roles(accounts):
    verify(accounts, 100)
    verify(accounts, 200)

    assert storage.fids_for_discord(42) == [(100, True), (200, False)]
    assert storage.main_fid_for_discord(42) == 100
    assert accounts.applied == [
        (42, "[AAA] Main", {10, 14, 107}, {15, 16, 108}),
        (42, "[AAA] Main", {10, 14, 107}, {15, 16, 108}),
    ]
    assert storage.user_row(200)["rank"] == 5


def test_set_main_switches_identity_and_reverify_preserves_selection(accounts):
    verify(accounts, 100)
    verify(accounts, 200)
    inter = interaction(accounts.member)
    asyncio.run(identity.IdentityBase.set_main_fid.callback(accounts.base, inter, fid=200))

    assert storage.fids_for_discord(42) == [(200, True), (100, False)]
    assert accounts.applied[-1] == (42, "[BBB] Alt", {10, 15, 16, 108}, {14, 107})
    verify(accounts, 100)
    assert storage.main_fid_for_discord(42) == 200
    assert accounts.applied[-1] == (42, "[BBB] Alt", {10, 15, 16, 108}, {14, 107})


def test_userdata_lists_both_and_unregister_only_detaches_requested_account(accounts):
    verify(accounts, 100)
    verify(accounts, 200)
    inter = interaction(accounts.member)
    asyncio.run(identity.IdentityBase.userdata_by_user.callback(
        accounts.base, inter, member=accounts.member))
    message = inter.followup.send.await_args.args[0]
    assert "`100` Main (main)" in message
    assert "`200` Alt" in message
    assert "`200` Alt (main)" not in message

    inter = interaction(accounts.member)
    asyncio.run(identity.IdentityBase.unregister.callback(accounts.base, inter, fids="200"))
    assert storage.fids_for_discord(42) == [(100, True)]
    assert storage.discord_for_fid(200) is None
    assert storage.user_row(200) is not None
    assert accounts.applied[-1] == (42, "[AAA] Main", {10, 14, 107}, {15, 16, 108})


@pytest.mark.parametrize("claimed_fid", [100, 200], ids=["bound-main", "bound-secondary"])
def test_verify_rejects_another_users_linked_id(accounts, claimed_fid, monkeypatch):
    verify(accounts, 100)
    verify(accounts, 200)
    assert storage.fids_for_discord(42) == [(100, True), (200, False)]
    other = SimpleNamespace(id=43, guild=accounts.member.guild)
    fetch = AsyncMock()
    monkeypatch.setattr(identity.mightpulse, "fetch_player_data", fetch)
    inter = interaction(other)
    asyncio.run(identity.IdentityMightpulse.verify.callback(
        accounts.live, inter, gid=str(claimed_fid)))

    assert storage.discord_for_fid(claimed_fid) == 42
    assert storage.fids_for_discord(42) == [(100, True), (200, False)]
    assert storage.fids_for_discord(43) == []
    assert [entry[0] for entry in accounts.applied] == [42, 42]
    fetch.assert_not_awaited()
    assert "already registered to another Discord user" in inter.followup.send.await_args.args[0]


def test_verify_rechecks_ownership_after_api_lookup(accounts, monkeypatch):
    original = identity.mightpulse.fetch_player_data

    async def fetch(gid, **kwargs):
        player = await original(gid, **kwargs)
        storage.register_player(fid=100, discord_id=43, nickname="Other owner")
        return player

    monkeypatch.setattr(identity.mightpulse, "fetch_player_data", fetch)
    inter = interaction(accounts.member)
    asyncio.run(identity.IdentityMightpulse.verify.callback(accounts.live, inter, gid="100"))
    assert storage.discord_for_fid(100) == 43
    assert storage.user_row(100)["nickname"] == "Other owner"
    assert accounts.applied == []
    assert "already registered to another Discord user" in inter.followup.send.await_args.args[0]