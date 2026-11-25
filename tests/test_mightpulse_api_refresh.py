"""API-only refresh transport and nightly-consumer regressions."""
import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extension.providers import mightpulse, mightpulse_api
from extension.cogs import blacklist as blacklist_mod


class Response:
    def __init__(self, status, payload, headers=None):
        self.status = status
        self.payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        return self.payload


@pytest.fixture
def transport(monkeypatch):
    responses, calls = [], []

    class Session:
        def __init__(self, **kwargs):
            assert kwargs["timeout"].total > mightpulse.API_WAIT_SECONDS

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        def get(self, url, **kwargs):
            calls.append((url, kwargs))
            return responses.pop(0)

    sleep = AsyncMock()
    monkeypatch.setattr(mightpulse_api.aiohttp, "ClientSession", Session)
    monkeypatch.setattr(mightpulse_api.asyncio, "sleep", sleep)
    monkeypatch.setattr(mightpulse._config, "api_key", "test-key")
    monkeypatch.setattr(mightpulse, "_PLAYER_CACHE", {})
    monkeypatch.setattr(mightpulse, "force_alliance_refresh", AsyncMock(return_value=False))
    return responses, calls, sleep


def fresh(player):
    return {"ok": True, "fresh": True, "refresh": "done", "player": player}


@pytest.mark.parametrize("age,state,dated,accepted", [
    (0, "done", True, True), (45, "none", True, True),
    (1641, "none", True, False), (61, "cooldown", True, True),
    (599, "cooldown", True, True), (601, "cooldown", True, False),
    (0, "done", False, False), (0, "running", True, False),
])
def test_nightly_requires_recent_dated_snapshot(transport, age, state, dated, accepted):
    responses, calls, _ = transport
    alliance = {"aid": 42, "kid": 7, "abbr": "TAG", "power": 123}
    envelope = {"ok": True, "fresh": True, "refresh": state,
                "age_seconds": age, "alliance": alliance}
    if dated:
        envelope["cached_at"] = time.time() - age
    responses.extend([Response(200, envelope) for _ in range(mightpulse.API_MAX_ATTEMPTS)])
    result, _ = asyncio.run(mightpulse.fetch_current_alliance(42, kid=7, tag=None))
    if accepted:
        assert result["power"] == alliance["power"]
        assert result["snapshot_cached_at"] == envelope["cached_at"]
    else:
        assert result is None
    assert all("max_age=60" in url for url, _ in calls)


def test_nightly_tag_fallback_also_requests_recent_data(transport):
    responses, calls, _ = transport
    alliance = {"aid": 42, "kid": 7, "abbr": "TAG", "power": 123}
    responses.extend([Response(404, {}), Response(200, {
        "ok": True, "fresh": True, "refresh": "done", "age_seconds": 0,
        "cached_at": time.time(), "alliance": alliance})])
    result, _ = asyncio.run(mightpulse.fetch_current_alliance(42, kid=7, tag="TAG"))
    assert result["power"] == alliance["power"]
    assert "snapshot_cached_at" in result
    assert len(calls) == 2
    assert all("max_age=60" in url for url, _ in calls)


@pytest.mark.parametrize("failures", [None, ["42"]])
def test_scheduled_post_refuses_unverified_snapshot(failures):
    from extension.tasks.nightly import NightlyTasks
    cog = NightlyTasks(SimpleNamespace())
    cog.last_alliance_refresh_failures = failures
    assert asyncio.run(cog._post_nap_ranking()) is False


def test_scheduled_post_refuses_yesterdays_snapshot():
    from datetime import datetime, timedelta, timezone
    from extension.tasks.nightly import NightlyTasks
    cog = NightlyTasks(SimpleNamespace())
    cog.last_alliance_refresh_failures = []
    cog.last_alliance_snapshot_date = datetime.now(timezone.utc).date() - timedelta(days=1)
    assert asyncio.run(cog._post_nap_ranking()) is False


def test_provider_uses_unofficial_client_for_every_alliance_id_attempt(transport, monkeypatch):
    from extension.providers.mightpulse_api import ApiResponse
    from extension.providers.mightpulse_unofficial import MightPulseUnofficialAPI

    _, calls, sleep = transport
    monkeypatch.setattr(mightpulse, "_ALLIANCE_CACHE_BY_AID", {})
    monkeypatch.setattr(mightpulse, "_ALLIANCE_CACHE_BY_KEY", {})
    alliance = {"aid": 123, "kid": 2464, "abbr": "NEW", "power": 456}
    lookup = AsyncMock(side_effect=[
        ApiResponse(200, {}, {"ok": True, "refresh": "queued", "retry_after": 1}),
        ApiResponse(200, {}, {"ok": True, "fresh": True, "alliance": alliance}),
    ])
    monkeypatch.setattr(MightPulseUnofficialAPI, "alliance_by_id", lookup)

    assert asyncio.run(mightpulse.fetch_alliance_by_aid(123, bypass_local_cache=True)) == alliance
    assert lookup.await_count == 2
    for call in lookup.await_args_list:
        assert call.args == (123,)
        assert call.kwargs["include"] == "info"
        assert call.kwargs["wait"] == mightpulse.API_WAIT_SECONDS
        assert call.kwargs["session"] is not None
    sleep.assert_awaited_once_with(1)
    assert calls == []  # No direct provider HTTP request bypasses the client.
    assert mightpulse.get_cached_alliance(aid=123) == alliance


@pytest.mark.parametrize("wrapper", ["board", "boards"])
def test_live_ranking_response_shape(wrapper):
    rows = [{"rank": 1, "aid": 247300009, "abbr": "BTS", "score": 4670161235}]
    board = {"type": 1, "key": "alliance_power", "kind": "alliance", "rows": rows}
    payload = {"ok": True, "kid": 2464, wrapper: board if wrapper == "board" else [board]}
    assert mightpulse._extract_ranking_rows(payload) == rows


@pytest.mark.parametrize("status,state", [(202, None), (200, "queued"), (200, "running"), (200, "skipped")])
def test_pending_waits_then_returns_updated_player(transport, status, state):
    responses, calls, sleep = transport
    responses.extend([
        Response(status, {"ok": True, "fresh": False, "pending": status == 202,
                          "refresh": state, "player": {"nick_name": "Old"}}, {"Retry-After": "3"}),
        Response(200, fresh({"nick_name": "New"})),
    ])
    assert asyncio.run(mightpulse.fetch_player_data("123", force_refresh=True)) == {"nick_name": "New"}
    sleep.assert_awaited_once_with(3)
    assert len(calls) == 2
    assert all(url.endswith("/players/123?include=base&wait=30") for url, _ in calls)
    assert all(kwargs == {"headers": {"Authorization": "Bearer test-key"}} for _, kwargs in calls)


@pytest.mark.parametrize("state,age,expected", [
    ("cooldown", 120, True), ("cooldown", 3601, False),
    ("cooldown", None, False), ("failed", 120, False), ("expired", 120, False),
])
def test_cooldown_and_failed_data_policy(transport, state, age, expected):
    responses, _, _ = transport
    player = {"nick_name": "Current"}
    responses.append(Response(200, {"ok": True, "fresh": False, "refresh": state,
                                     "age_seconds": age, "player": player}))
    result = asyncio.run(mightpulse.fetch_player_data("123"))
    assert result == (player if expected else None)
    assert mightpulse.get_cached_player("123") == result


@pytest.mark.parametrize("status", [429, 503])
def test_transient_error_retries(transport, status):
    responses, _, sleep = transport
    responses.extend([Response(status, {}, {"Retry-After": "1"}), Response(200, fresh({"kid": 7}))])
    assert asyncio.run(mightpulse.fetch_player_data("123")) == {"kid": 7}
    assert sleep.await_count >= 1


def test_daily_rate_limit_does_not_sleep_for_hours(transport):
    responses, _, sleep = transport
    responses.append(Response(429, {}, {"Retry-After": "86400"}))
    assert asyncio.run(mightpulse.fetch_player_data("123")) == "RATE_LIMITED"
    sleep.assert_not_awaited()


def test_blacklist_preserves_last_known_player_when_api_update_pending(transport, monkeypatch):
    responses, _, _ = transport
    monkeypatch.setattr(mightpulse, "available", lambda: True)
    responses.extend([Response(200, {"ok": True, "fresh": False, "refresh": "running",
                                    "player": {"nick_name": "Untrusted"}})
                      for _ in range(mightpulse.API_MAX_ATTEMPTS)])
    record = {"fid": "123", "player": {"nick_name": "Last known"}, "refreshed_at": "yesterday"}
    cog = blacklist_mod.Blacklist(SimpleNamespace())
    asyncio.run(cog._refresh(record))
    assert record["player"] == {"nick_name": "Last known"}
    assert record["refreshed_at"] == "yesterday"
    assert record["refresh_failed"] is True


def test_blacklist_uses_updated_player_after_api_refresh(transport, monkeypatch):
    responses, _, _ = transport
    monkeypatch.setattr(mightpulse, "available", lambda: True)
    player = {"nick_name": "Renamed", "kid": 7, "alliance": {"aid": 42, "abbr": "NEW"}}
    responses.extend([
        Response(200, {"ok": True, "fresh": False, "refresh": "queued",
                       "player": {"nick_name": "Old"}}),
        Response(200, fresh(player)),
    ])
    record = {"fid": "123", "player": {"nick_name": "Old"}}
    asyncio.run(blacklist_mod.Blacklist(SimpleNamespace())._refresh(record))
    assert record["player"] == player
    assert record["refreshed_at"]
    assert record["refresh_failed"] is False