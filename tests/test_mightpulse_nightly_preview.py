"""Nightly alliance dry run: real orchestration, in-memory storage, no Discord.

Live: RUN_MIGHTPULSE_LIVE=1 python -m pytest -s -q tests/test_mightpulse_nightly_preview.py
GETs consume quota and may queue upstream refreshes. No production DB is read.
"""
import asyncio
import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values

from extension.providers import mightpulse
from extension.providers.mightpulse_api import MightPulseAPI
from extension.tasks import nightly


async def run_preview(monkeypatch, kid):
    rows = {}
    failures = []
    refreshed_power = {}
    refresh = mightpulse.fetch_current_alliance

    async def tracked_refresh(aid, **kwargs):
        result = await refresh(aid, **kwargs)
        if not isinstance(result[0], dict):
            failures.append(aid)
        else:
            refreshed_power[str(aid)] = result[0].get("power")
        return result

    def upsert(aid, **fields):
        rows.setdefault(str(aid), {"alliance_id": str(aid)}).update(fields)

    class Settings:
        def get_json(self, key, default):
            return [kid] if key == "whitelist_kids" else default

        def get_int(self, key, default):
            return default

        def get(self, key):
            return "2" if key == "nap_candidate_multiplier" else None

    monkeypatch.setattr(nightly.storage, "all_alliances", lambda: list(rows.values()))
    monkeypatch.setattr(nightly.storage, "upsert_alliance", upsert)
    monkeypatch.setattr(nightly.storage, "cleanup_dead_alliances", lambda candidates: [])
    monkeypatch.setattr(mightpulse, "fetch_current_alliance", tracked_refresh)
    bot = SimpleNamespace(extension_settings=Settings(), maintenance_lock=asyncio.Lock())
    task = nightly.NightlyTasks(bot)
    monkeypatch.setattr(task, "_sync_alliance_discord_state", AsyncMock(return_value=0))
    monkeypatch.setattr(task, "_sync_linked_players", AsyncMock(return_value={"synced": 0, "skipped": 0}))
    started = time.monotonic()
    await task._run_nightly_maintenance(triggered_by="preview")
    ranking = sorted(rows.values(), key=lambda row: row["power"], reverse=True)
    print(f"\nNightly alliance preview — kingdom {kid}")
    print("Rank  Tag          Power                 Name")
    for rank, row in enumerate(ranking[:10], 1):
        status = " [INFO REFRESH FAILED; FALLBACK VALUE]" if int(row["alliance_id"]) in failures else " [REFRESHED INFO]"
        print(f"{rank:>4}  {row['abbr']:<10} {row['power']:>18,}  {row['name']}{status}")
    print(f"Candidates: {len(rows)}; info refresh failures: {len(failures)}; elapsed: {time.monotonic() - started:.1f}s")
    print("Dry run only: no NAP exclusions/lookback, blacklist, linked-player sync, or Discord posting.")
    assert ranking, "Nightly discovery returned no usable rankings"
    assert all(row["power"] > 0 for row in ranking)
    assert all(row["power"] == refreshed_power[row["alliance_id"]]
               for row in ranking if row["alliance_id"] in refreshed_power)
    return ranking, failures


def test_nightly_preview_ranks_by_refreshed_power(monkeypatch, capsys):
    candidates = [{"aid": n, "abbr": f"T{n}", "name": f"Alliance {n}", "power": 1000 - n}
                  for n in range(1, 21)]
    board = AsyncMock(return_value=candidates)
    monkeypatch.setattr(mightpulse, "fetch_kingdom_alliance_power_ranking", board)

    async def refresh(aid, **kwargs):
        return (None if aid == 2 else {"abbr": f"T{aid}", "name": f"Alliance {aid}", "power": 2000 + aid}), "official-api"

    monkeypatch.setattr(mightpulse, "fetch_current_alliance", refresh)
    ranking, failures = asyncio.run(run_preview(monkeypatch, 2464))
    board.assert_awaited_once_with(2464, limit=20)
    assert len(ranking) == 20
    assert failures == [2]
    assert ranking[0]["alliance_id"] == "20"
    assert ranking[0]["power"] == 2020
    output = capsys.readouterr().out
    assert "Alliance 20" in output
    assert "Alliance 10" not in output
    assert "REFRESHED INFO" in output


@pytest.mark.skipif(os.environ.get("RUN_MIGHTPULSE_LIVE") != "1", reason="Live API checks are opt-in")
def test_nightly_top10_live(monkeypatch):
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    key = os.environ.get("MIGHTPULSE_API_KEY") or env.get("MIGHTPULSE_API_KEY")
    assert key, "MIGHTPULSE_API_KEY is required"
    kid = int(os.environ.get("MIGHTPULSE_TEST_KID", "2464"))
    monkeypatch.setattr(mightpulse._config, "api_key", key)
    monkeypatch.setattr(mightpulse, "_ALLIANCE_CACHE_BY_AID", {})
    monkeypatch.setattr(mightpulse, "_ALLIANCE_CACHE_BY_KEY", {})

    async def check():
        client = MightPulseAPI(key)
        usage = await client.usage()
        assert usage.status == 200 and isinstance(usage.data, dict)
        assert usage.data["daily"]["remaining"] >= 121, "Insufficient quota for bounded retries/fallbacks"
        print(f"\nMighty: {client.mighty}; usage before: {usage.data}")
        ranking, failures = await run_preview(monkeypatch, kid)
        after = await client.usage()
        print(f"Usage after: {after.data}")
        assert len(ranking) >= 10, "Fewer than ten usable alliances"
        assert not failures, f"Alliance info refresh failed: {failures}"

    asyncio.run(check())