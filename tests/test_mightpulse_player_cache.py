"""Player cache lifetime and explicit-refresh logging regressions."""

import asyncio
from unittest.mock import AsyncMock

import pytest

from extension.providers import mightpulse


GID = "432453913"
PLAYER = {"uid": 108938976, "nick_name": "Governor"}


@pytest.fixture
def provider(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(mightpulse, "_PLAYER_CACHE", {})
    monkeypatch.setattr(mightpulse._config, "player_cache_ttl_seconds", 900)
    monkeypatch.setattr(mightpulse.time_module, "monotonic", lambda: clock[0])
    fetch = AsyncMock(return_value={"ok": True, "fresh": True, "player": PLAYER})
    monkeypatch.setattr(mightpulse, "fetch_player_envelope", fetch)
    return clock, fetch


def test_repeated_reads_use_local_cache_until_ttl(provider, capsys):
    clock, fetch = provider
    assert asyncio.run(mightpulse.fetch_player_data(GID)) == PLAYER
    for elapsed in (6, 15, 38, 899):
        clock[0] = 1000.0 + elapsed
        assert asyncio.run(mightpulse.fetch_player_data(int(GID))) == PLAYER
    fetch.assert_awaited_once_with(GID)
    output = capsys.readouterr().out
    assert output.count(f"player gid={GID} MISS") == 1
    assert output.count(f"player gid={GID} HIT") == 4

    clock[0] = 1900.0
    assert asyncio.run(mightpulse.fetch_player_data(GID)) == PLAYER
    assert fetch.await_count == 2


@pytest.mark.parametrize("flags", [
    {"force_refresh": True},
    {"bypass_local_cache": True},
    {"force_refresh": True, "bypass_local_cache": True},
])
def test_explicit_refresh_logs_bypass_and_updates_cache(provider, capsys, flags):
    _, fetch = provider
    mightpulse.cache_player(GID, {"nick_name": "Old name"})
    assert asyncio.run(mightpulse.fetch_player_data(GID, **flags)) == PLAYER
    fetch.assert_awaited_once_with(GID)
    assert mightpulse.get_cached_player(GID) == PLAYER
    output = capsys.readouterr().out
    assert f"player gid={GID} BYPASS" in output
    assert f"force_refresh={int(flags.get('force_refresh', False))}" in output
    assert f"bypass_local_cache={int(flags.get('bypass_local_cache', False))}" in output
    assert "MISS" not in output


def test_pending_refresh_does_not_populate_cache(provider):
    _, fetch = provider
    fetch.return_value = {"ok": True, "fresh": False, "refresh": "queued", "player": PLAYER}

    assert asyncio.run(mightpulse.fetch_player_data(GID, force_refresh=True)) is None
    assert mightpulse.get_cached_player(GID) is None
    fetch.assert_awaited_once_with(GID)