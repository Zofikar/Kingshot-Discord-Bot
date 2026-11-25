"""Documented route contracts, plus the separately isolated ID extension."""
import asyncio
import ast
import inspect
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from dotenv import dotenv_values

from extension.providers.mightpulse_api import ApiResponse, MightPulseAPI
from extension.providers.mightpulse_unofficial import MightPulseUnofficialAPI


def test_provider_contains_no_http_transport_or_endpoint_paths():
    from extension.providers import mightpulse

    source = inspect.getsource(mightpulse)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert not {alias.name for alias in node.names} & {"aiohttp", "json", "urllib.parse"}
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {"request", "open", "ClientSession", "ClientTimeout", "json", "text"}
    assert 'players/' not in source
    assert 'alliances/' not in source
    assert 'kingdoms/' not in source


ROUTES = [
    ("usage", (), {}, "usage", {}),
    ("player", (123,), {"include": "base,heroes", "id_type": "uid", "wait": 30, "max_age": 300}, "players/123", {"include": "base,heroes", "id_type": "uid", "wait": 30, "max_age": 300}),
    ("players", ([123, 456],), {}, "players", {"ids": "123,456"}),
    ("alliance", (2464, "A/B"), {"include": "info,roster"}, "alliances/2464/A%2FB", {"include": "info,roster"}),
    ("kingdoms", (), {"page": 1, "size": 24}, "kingdoms", {"page": 1, "size": 24}),
    ("kingdom", (2464,), {"include": "boards", "limit": 100}, "kingdoms/2464", {"include": "boards", "limit": 100}),
    ("kingdom_ranks", (2464,), {"board": "alliance_power", "limit": 100}, "kingdoms/2464/ranks", {"board": "alliance_power", "limit": 100}),
    ("kingdom_events", (2464,), {}, "kingdoms/2464/events", {}),
    ("kingdom_kvk", (2464,), {}, "kingdoms/2464/kvk", {}),
    ("kingdom_kvk_scores", (2464,), {}, "kingdoms/2464/kvk/scores", {}),
    ("kvk_matchups", (), {}, "kvk/matchups", {}),
    ("alliance_by_id", (247300011,), {"include": "info", "wait": 30}, "alliances/247300011", {"include": "info", "wait": 30}),
]


@pytest.mark.parametrize("method,args,kwargs,path,params", ROUTES)
def test_route_contract(method, args, kwargs, path, params):
    client = MightPulseUnofficialAPI("test-key")
    expected = ApiResponse(202, {"Retry-After": "2"}, {"pending": True})
    client.request = AsyncMock(return_value=expected)
    assert asyncio.run(getattr(client, method)(*args, **kwargs)) is expected
    actual_path = client.request.call_args.args[0]
    actual_params = {k: v for k, v in client.request.call_args.kwargs.items() if v is not None}
    assert (actual_path, actual_params) == (path, params)


def test_transport_encoding_and_auth():
    client = MightPulseAPI("test-key")
    session = type("Session", (), {"get": lambda self, *a, **kw: (a, kw)})()
    args, kwargs = client.open(session, "players", ids="1,2", include=None, wait=0)
    assert args == ("https://api.mightpulse.com/v1/players?ids=1,2&wait=0",)
    assert kwargs == {"headers": {"Authorization": "Bearer test-key"}}


@pytest.mark.parametrize("ids", [[], list(range(51))])
def test_batch_bounds(ids):
    with pytest.raises(ValueError):
        asyncio.run(MightPulseAPI("test-key").players(ids))


@pytest.mark.skipif(os.environ.get("RUN_MIGHTPULSE_LIVE") != "1", reason="Authenticated API checks are opt-in")
def test_all_routes_live():
    env = dotenv_values(Path(__file__).resolve().parents[1] / ".env")
    key = os.environ.get("MIGHTPULSE_API_KEY") or env.get("MIGHTPULSE_API_KEY")
    assert key, "MIGHTPULSE_API_KEY is required"

    async def check():
        client = MightPulseUnofficialAPI(key)
        usage = await client.usage()
        assert usage.status == 200 and isinstance(client.mighty, bool)
        assert usage.data["daily"]["remaining"] >= 20
        assert usage.data["minute"]["remaining"] >= 20
        kid = int(os.environ.get("MIGHTPULSE_TEST_KID", "2464"))
        gid = os.environ.get("MIGHTPULSE_TEST_GID", "432453913")
        aid = int(os.environ.get("MIGHTPULSE_TEST_AID", "247300011"))
        alliance = await client.alliance_by_id(aid, include="info", wait=0)
        assert alliance.status == 200 and alliance.data["ok"]
        tag = alliance.data["alliance"]["abbr"]
        calls = [
            ("player", client.player(gid, include="base,heroes,ranks,gov_gear", wait=0)),
            ("players", client.players([gid], include="base", wait=0)),
            ("alliance", client.alliance(kid, tag, include="info,roster", wait=0)),
            ("kingdoms", client.kingdoms(page=1, size=1)),
            ("kingdom", client.kingdom(kid, include="boards", limit=1)),
            ("ranks", client.kingdom_ranks(kid, board="alliance_power", limit=100)),
        ]
        for name, call in calls:
            result = await call
            assert result.status == 200, (name, result.status)
            assert result.data["ok"], name
        for name, method in [("events", client.kingdom_events), ("kvk", client.kingdom_kvk), ("scores", client.kingdom_kvk_scores), ("matchups", lambda _: client.kvk_matchups())]:
            result = await method(kid)
            if not client.mighty:
                assert result.status == 403 and result.data["error"] == "mighty_required", name
            else:
                assert result.status in (200, 404), (name, result.status)
                if result.status == 200:
                    assert result.data["ok"], name
        assert client.last_usage == usage.data
    asyncio.run(check())
