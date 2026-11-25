"""Explicitly unofficial nightly recovery and publication freshness contracts."""
import asyncio
import json
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extension.providers import mightpulse, mightpulse_website_unofficial as website
from extension.tasks import nightly


def test_website_recovery_requires_verified_api_reread(monkeypatch):
    snapshot = {"power": 123, "snapshot_cached_at": time.time()}
    read = AsyncMock(side_effect=[None, None, snapshot])
    refresh = AsyncMock(return_value=True)
    monkeypatch.setattr(mightpulse, "read_official_alliance", read)
    monkeypatch.setattr(mightpulse, "force_alliance_refresh", refresh)
    monkeypatch.setattr(mightpulse.asyncio, "sleep", AsyncMock())
    result, source = asyncio.run(mightpulse.fetch_current_alliance(42, kid=7, tag="TAG"))
    assert result == snapshot
    assert source == "unofficial-website-refresh"
    refresh.assert_awaited_once_with(42, kid=7, tag="TAG")
    assert read.await_count == 3


def test_website_completion_alone_does_not_supply_power(monkeypatch):
    monkeypatch.setattr(mightpulse, "read_official_alliance", AsyncMock(return_value=None))
    monkeypatch.setattr(mightpulse, "force_alliance_refresh", AsyncMock(return_value=True))
    monkeypatch.setattr(mightpulse.asyncio, "sleep", AsyncMock())
    assert asyncio.run(mightpulse.fetch_current_alliance(42, kid=7, tag=None))[0] is None


@pytest.mark.parametrize("age,accepted", [(599, True), (601, False), (None, False),
                                         (float("nan"), False), (-60, False)])
def test_publication_checks_actual_snapshot_age(monkeypatch, age, accepted):
    cog = nightly.NightlyTasks(SimpleNamespace())
    cog.last_alliance_refresh_failures = []
    cog.last_alliance_snapshot_date = datetime.now(timezone.utc).date()
    cog.alliance_snapshot_times = {"42": None if age is None else time.time() - age}
    monkeypatch.setattr(nightly.storage, "all_alliances", lambda: [{"alliance_id": "42"}])
    assert cog.snapshot_ready_for_publication() is accepted


def test_publication_retries_failed_candidate(monkeypatch):
    cog = nightly.NightlyTasks(SimpleNamespace(maintenance_lock=asyncio.Lock()))
    cog.last_alliance_refresh_failures = ["42"]
    row = {"alliance_id": "42", "kid": 7, "abbr": "TAG", "name": "Name"}
    monkeypatch.setattr(nightly.storage, "all_alliances", lambda: [row])
    writes = []
    monkeypatch.setattr(nightly.storage, "upsert_alliance", lambda *args, **kwargs: writes.append(kwargs))
    captured = time.time()
    monkeypatch.setattr(mightpulse, "fetch_current_alliance", AsyncMock(return_value=(
        {"power": 456, "snapshot_cached_at": captured}, "unofficial-website-refresh")))
    asyncio.run(cog._recover_publication_snapshot())
    assert cog.last_alliance_refresh_failures == []
    assert cog.alliance_snapshot_times["42"] == captured
    assert writes[0]["power"] == 456


@pytest.mark.parametrize("completes", [True, False])
def test_browser_transport_polls_and_never_sends_bearer(monkeypatch, completes):
    calls = []

    class Response:
        status = 200

        def __init__(self, payload):
            self.payload = payload

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def read(self):
            return b""

        async def text(self):
            return json.dumps(self.payload)

    class Session:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response({"queued": not completes, "started": False})

        def post(self, url, **kwargs):
            calls.append((url, kwargs))
            return Response({"queued": True, "alliance": {"power": 999}})

    monkeypatch.setattr(website.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(website.asyncio, "sleep", AsyncMock())
    assert asyncio.run(website.force_alliance_refresh(42, kid=7, tag="A/B")) is completes
    assert calls[0][0] == "https://mightpulse.com/7/A%2FB"
    assert "force=1" in calls[1][0]
    assert all("Authorization" not in kwargs["headers"] for _, kwargs in calls)