"""Opt-in authenticated smoke test; never posts to Discord or writes bot storage.

Run with RUN_MIGHTPULSE_LIVE=1. API GETs can queue upstream refreshes and consume
quota. IDs default to existing regression examples and can be overridden.
"""
import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import pytest
from dotenv import dotenv_values

from extension.cogs.blacklist import Blacklist
from extension.providers import mightpulse


@pytest.mark.skipif(os.environ.get("RUN_MIGHTPULSE_LIVE") != "1", reason="Live API checks are opt-in")
def test_authenticated_provider_and_blacklist(monkeypatch):
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    key = os.environ.get("MIGHTPULSE_API_KEY") or env.get("MIGHTPULSE_API_KEY")
    assert key, "MIGHTPULSE_API_KEY is required"
    monkeypatch.setattr(mightpulse._config, "api_key", key)
    monkeypatch.setattr(mightpulse, "_PLAYER_CACHE", {})
    monkeypatch.setattr(mightpulse, "_ALLIANCE_CACHE_BY_AID", {})
    monkeypatch.setattr(mightpulse, "_ALLIANCE_CACHE_BY_KEY", {})
    gid = os.environ.get("MIGHTPULSE_TEST_GID", "432453913")
    aid = int(os.environ.get("MIGHTPULSE_TEST_AID", "247300011"))
    kid = int(os.environ.get("MIGHTPULSE_TEST_KID", "2464"))

    async def check():
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20)) as session:
            async with session.get(f"{mightpulse.API_BASE}/usage", headers={"Authorization": f"Bearer {key}"}) as response:
                assert response.status == 200
                usage = await response.json()
                assert usage["daily"]["remaining"] >= 10
                assert usage["minute"]["remaining"] >= 10
        rows = await mightpulse.fetch_kingdom_alliance_power_ranking(kid, limit=3)
        assert rows and all(row["power"] > 0 for row in rows)
        alliance, source = await mightpulse.fetch_current_alliance(aid, kid=kid, tag=None)
        assert isinstance(alliance, dict) and int(alliance["aid"]) == aid
        assert source == "official-api"
        player = await mightpulse.fetch_player_data(gid, force_refresh=True)
        assert isinstance(player, dict)
        assert int(player["governor_id"]) == int(gid)
        assert {"nick_name", "kid", "power", "alliance"} <= player.keys()
        # Use a throwaway record: exercise blacklist refresh without persistence
        # or notifications, reusing the cached player to avoid another API call.
        record = {"fid": gid}
        await Blacklist(SimpleNamespace())._refresh(record, force_refresh=False)
        assert record["player"] == player
        assert record["refresh_failed"] is False
        assert record["refreshed_at"]

    asyncio.run(check())