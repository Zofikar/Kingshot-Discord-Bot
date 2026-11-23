import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extension import blacklist as store, storage
from extension.cogs import blacklist as cog_mod


@pytest.fixture
def db(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    storage.ensure_schema()


def test_persistence_expiry_and_removal(db):
    record = {"fid": "123", "reason": "test", "ends_at": None}
    store.save(record)
    storage.Storage().save(storage.Storage().load())
    assert store.records() == [record]
    assert store.active(record)
    assert not store.active({"ends_at": "2020-01-01T00:00:00+00:00"})
    assert store.remove("123")
    assert not store.remove("123")


def test_end_validation():
    assert store.parse_end(None) is None
    assert store.parse_end("2099-01-01 00:00").endswith("+00:00")
    for value in ("invalid", "2020-01-01"):
        with pytest.raises(ValueError):
            store.parse_end(value)


def test_name_lookup_requires_unique_match(db):
    with storage._connect("db/users.sqlite") as conn:
        conn.executemany("INSERT INTO users (fid, nickname, kid) VALUES (?, ?, ?)",
                         [(1, "Player", 7), (2, "PLAYER", 7), (3, "Player", 8)])
    assert store.find_fids(7, "player") == ["1", "2"]
    assert store.find_fids(8, " player ") == ["3"]


def test_refresh_notifies_only_fresh_active_protected_players(db, monkeypatch):
    for fid in ("1", "2", "3"):
        store.save({"fid": fid, "reason": "test", "ends_at": None})
    monkeypatch.setattr(cog_mod.mightpulse, "available", lambda: True)
    async def fetch(fid, **kwargs):
        assert kwargs == {"force_refresh": True, "bypass_local_cache": True}
        return None if fid == "2" else {"alliance": {"aid": int(fid)}}
    monkeypatch.setattr(cog_mod.mightpulse, "fetch_player_data", fetch)
    monkeypatch.setattr(cog_mod, "_protection", lambda bot, aid, tag: (aid == 1, "test"))
    cog = cog_mod.Blacklist(SimpleNamespace())
    cog._notify = AsyncMock()
    records = asyncio.run(cog.refresh(notify=True))
    assert len(records) == 3
    assert records[1]["refresh_failed"]
    cog._notify.assert_awaited_once_with(records[0], 1, None)


def test_commands_deny_non_admins(monkeypatch):
    monkeypatch.setattr(cog_mod.PermissionManager, "is_admin", lambda uid: (False,))
    interaction = SimpleNamespace(user=SimpleNamespace(id=1),
                                  response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    cog = cog_mod.Blacklist(SimpleNamespace())
    for command, kwargs in ((cog_mod.Blacklist.blacklist, {"reason": "test", "fid": "1"}),
                            (cog_mod.Blacklist.unblacklist, {"fid": "1"})):
        asyncio.run(command.callback(cog, interaction, **kwargs))
    assert interaction.followup.send.await_count == 2


@pytest.mark.parametrize("records", [[], [{"fid": "123", "reason": "test", "ends_at": None}]])
def test_list_available_to_non_admins(monkeypatch, records):
    def unexpected_admin_check(uid):
        pytest.fail("Listing must not require admin permission")
    monkeypatch.setattr(cog_mod.PermissionManager, "is_admin", unexpected_admin_check)
    interaction = SimpleNamespace(user=SimpleNamespace(id=1),
                                  response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    cog = cog_mod.Blacklist(SimpleNamespace())
    cog.refresh = AsyncMock(return_value=records)
    asyncio.run(cog_mod.Blacklist.blacklist_list.callback(cog, interaction))
    interaction.response.defer.assert_awaited_once_with(ephemeral=True)
    cog.refresh.assert_awaited_once_with(force_refresh=False)
    interaction.followup.send.assert_awaited_once()
    assert interaction.followup.send.call_args.kwargs["ephemeral"] is True
    content = interaction.followup.send.call_args.args[0]
    assert ("123" in content) if records else (content == "No active blacklisted players.")


def test_list_reuses_local_player_cache(db, monkeypatch):
    store.save({"fid": "123", "reason": "test", "ends_at": None})
    monkeypatch.setattr(cog_mod.mightpulse, "available", lambda: True)
    monkeypatch.setattr(cog_mod.mightpulse, "_PLAYER_CACHE", {})
    monkeypatch.setattr(cog_mod.mightpulse._config, "player_cache_ttl_seconds", 900)
    player = {"nick_name": "Cached player", "kid": 7}
    cog_mod.mightpulse.cache_player("123", player)
    fetch = AsyncMock()
    monkeypatch.setattr(cog_mod.mightpulse, "fetch_player_envelope", fetch)
    interaction = SimpleNamespace(response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    cog = cog_mod.Blacklist(SimpleNamespace())

    for _ in range(2):
        asyncio.run(cog_mod.Blacklist.blacklist_list.callback(cog, interaction))

    fetch.assert_not_awaited()
    assert store.records()[0]["player"] == player
    assert not store.records()[0]["refresh_failed"]
    assert interaction.followup.send.await_count == 2


@pytest.mark.parametrize("by_name", [False, True])
def test_adding_player_bypasses_cache_only_for_name_and_kd(db, monkeypatch, by_name):
    monkeypatch.setattr(cog_mod.PermissionManager, "is_admin", lambda uid: (True,))
    monkeypatch.setattr(cog_mod.mightpulse, "available", lambda: True)
    fetch = AsyncMock(return_value={"nick_name": "Fresh player"})
    monkeypatch.setattr(cog_mod.mightpulse, "fetch_player_data", fetch)
    interaction = SimpleNamespace(user=SimpleNamespace(id=1),
                                  response=SimpleNamespace(defer=AsyncMock()),
                                  followup=SimpleNamespace(send=AsyncMock()))
    cog = cog_mod.Blacklist(SimpleNamespace())
    monkeypatch.setattr(store, "find_fids", lambda kd, name: ["123"])

    asyncio.run(cog_mod.Blacklist.blacklist.callback(
        cog, interaction, reason="test",
        **({"kd": 7, "player_name": "Fresh player"} if by_name else {"fid": "123"})))

    fetch.assert_awaited_once_with("123", force_refresh=by_name, bypass_local_cache=by_name)
    assert store.records()[0]["player"]["nick_name"] == "Fresh player"