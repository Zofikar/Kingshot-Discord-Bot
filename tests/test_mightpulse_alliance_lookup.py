"""Tests for the alliance refresh path: alliance-id lookups (rename-safe).

A tag is only an alliance's *current* label: MightPulse's tag-keyed endpoints
(``/v1/alliances/{kid}/{tag}``) answer ``alliance_not_found`` after a rename,
which used to freeze the stored tag/name/power forever. The provider now reads
alliances by their stable ``aid``, so a rename is picked up, and the nightly
maintenance records the old tag as a historical alias.
"""
import asyncio
import json as jsonlib
import types

import aiohttp
import pytest

from extension import nap as nap_mod
from extension.cogs import nap as nap_cog
from extension.providers import mightpulse
from extension.tasks import nightly as nightly_mod

AID = 247300011
KID = 2464
OLD_TAG = "MNX"
NEW_TAG = "TKO"

AID_URL = f"https://api.mightpulse.com/v1/alliances/{AID}?include=info"
TAG_URL = f"https://api.mightpulse.com/v1/alliances/{KID}/{OLD_TAG}?include=info"
STATUS_URL = f"https://mightpulse.com/api/alliances/{AID}/refresh/status"
REFRESH_URL = f"https://mightpulse.com/api/alliances/{AID}/refresh?force=1&kid={KID}"
PAGE_URL = f"https://mightpulse.com/{KID}/{OLD_TAG}"

FRESH_ENVELOPE = {
    "ok": True, "aid": AID, "kid": KID, "tag": NEW_TAG, "fresh": True,
    "alliance": {
        "aid": AID, "name": "theknightsONE", "abbr": NEW_TAG, "kid": KID,
        "power": 1858681076,
    },
}
STALE_TAG_ENVELOPE = {
    "ok": True, "aid": AID, "kid": KID, "tag": OLD_TAG,
    "alliance": {"aid": AID, "name": "FAMILLY", "abbr": OLD_TAG, "kid": KID,
                 "power": 1117021914},
}
NOT_FOUND = {"ok": False, "error": "alliance_not_found"}


class _Resp:
    def __init__(self, status, payload=None):
        self.status = status
        self._payload = payload
        self.url = ""

    async def json(self):
        return self._payload

    async def text(self):
        return jsonlib.dumps(self._payload) if self._payload is not None else ""

    async def read(self):
        return b""

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Jar:
    def filter_cookies(self, url):
        return {}


class _Session:
    """Minimal aiohttp.ClientSession stand-in routed by (method, url)."""

    def __init__(self, routes, calls, **_kwargs):
        self._routes = routes
        self.calls = calls
        self.cookie_jar = _Jar()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def _call(self, method, url):
        self.calls.append((method, url))
        route = self._routes.get((method, url))
        return route if route is not None else _Resp(404, NOT_FOUND)

    def get(self, url, **_kwargs):
        return self._call("GET", url)

    def post(self, url, **_kwargs):
        return self._call("POST", url)


@pytest.fixture(autouse=True)
def _provider(monkeypatch):
    """Route all provider HTTP through the fake session and isolate the caches."""
    calls = []
    routes = {}

    class _FakeAiohttp:
        # Only the aiohttp names the provider touches; the provider's timeout
        # handling uses the real asyncio.TimeoutError.
        ClientError = aiohttp.ClientError
        ClientTimeout = aiohttp.ClientTimeout
        CookieJar = aiohttp.CookieJar

        @staticmethod
        def ClientSession(**kwargs):
            return _Session(routes, calls, **kwargs)

    monkeypatch.setattr(mightpulse, "aiohttp", _FakeAiohttp)
    monkeypatch.setattr(mightpulse, "SITE_REFRESH_POLL_SECONDS", 0)
    mightpulse.configure(types.SimpleNamespace(
        mightpulse_api_key="test-key",
        player_cache_ttl_seconds=900,
        alliance_cache_ttl_seconds=3600,
        allowed_kingdom_id=KID,
    ))
    mightpulse._ALLIANCE_CACHE_BY_AID.clear()
    mightpulse._ALLIANCE_CACHE_BY_KEY.clear()
    yield types.SimpleNamespace(routes=routes, calls=calls)
    mightpulse._ALLIANCE_CACHE_BY_AID.clear()
    mightpulse._ALLIANCE_CACHE_BY_KEY.clear()


def test_fetch_alliance_by_aid_reads_the_id_endpoint(_provider):
    _provider.routes[("GET", AID_URL)] = _Resp(200, FRESH_ENVELOPE)

    alliance = asyncio.run(mightpulse.fetch_alliance_by_aid(AID))

    assert alliance["abbr"] == NEW_TAG
    assert alliance["name"] == "theknightsONE"
    assert alliance["aid"] == AID
    assert alliance["kid"] == KID
    assert _provider.calls == [("GET", AID_URL)]
    # Cached by aid, so a later page-load style read needs no HTTP call.
    assert mightpulse.get_cached_alliance(aid=AID)["abbr"] == NEW_TAG


def test_fetch_alliance_by_aid_returns_none_when_unknown(_provider):
    assert asyncio.run(mightpulse.fetch_alliance_by_aid(AID)) is None


def test_read_official_alliance_prefers_id_then_falls_back_to_tag(_provider):
    _provider.routes[("GET", TAG_URL)] = _Resp(200, STALE_TAG_ENVELOPE)

    alliance = asyncio.run(mightpulse.read_official_alliance(AID, kid=KID, tag=OLD_TAG))

    assert alliance["abbr"] == OLD_TAG
    assert _provider.calls == [("GET", AID_URL), ("GET", TAG_URL)]


def test_current_alliance_uses_aid_when_stored_tag_is_stale(_provider):
    """A renamed alliance must still refresh instead of 404ing on its old tag."""
    _provider.routes[("GET", PAGE_URL)] = _Resp(200, None)
    _provider.routes[("GET", STATUS_URL)] = _Resp(
        200, {"ok": True, "cooldown_remaining_sec": 120, "queued": False, "started": False})
    _provider.routes[("GET", AID_URL)] = _Resp(200, FRESH_ENVELOPE)
    # The tag route only answers alliance_not_found (that tag no longer exists).
    _provider.routes[("GET", TAG_URL)] = _Resp(404, NOT_FOUND)

    data, source = asyncio.run(
        mightpulse.fetch_current_alliance(AID, kid=KID, tag=OLD_TAG))

    assert source == "official-api-recent"
    assert data["abbr"] == NEW_TAG
    assert data["power"] == 1858681076
    assert ("GET", AID_URL) in _provider.calls
    assert ("GET", TAG_URL) not in _provider.calls


def test_current_alliance_works_without_any_stored_tag(_provider):
    _provider.routes[("GET", f"https://mightpulse.com/{KID}")] = _Resp(200, None)
    _provider.routes[("GET", STATUS_URL)] = _Resp(
        200, {"ok": True, "cooldown_remaining_sec": 30, "queued": False, "started": False})
    _provider.routes[("GET", AID_URL)] = _Resp(200, FRESH_ENVELOPE)

    data, source = asyncio.run(mightpulse.fetch_current_alliance(AID, kid=KID, tag=None))

    assert source == "official-api-recent"
    assert data["abbr"] == NEW_TAG


def test_refresh_rereads_by_aid_and_detects_the_rename(_provider):
    """A completed site refresh must not be discarded by a tag-keyed re-read."""
    _provider.routes[("GET", PAGE_URL)] = _Resp(200, None)
    _provider.routes[("POST", REFRESH_URL)] = _Resp(
        200, {"ok": True, "queued": True, "alliance": STALE_TAG_ENVELOPE["alliance"]})
    _provider.routes[("GET", STATUS_URL)] = _Resp(
        200, {"ok": True, "cooldown_remaining_sec": 0, "queued": False, "started": False})
    _provider.routes[("GET", AID_URL)] = _Resp(200, FRESH_ENVELOPE)
    _provider.routes[("GET", TAG_URL)] = _Resp(404, NOT_FOUND)

    data = asyncio.run(mightpulse.refresh_site_alliance(
        AID, force=True, tag=OLD_TAG, bypass_local_cache=True))

    assert data["abbr"] == NEW_TAG
    assert data["power"] == 1858681076
    assert ("POST", REFRESH_URL) in _provider.calls
    assert ("GET", AID_URL) in _provider.calls


def test_refresh_still_refuses_stale_payload_when_refresh_never_completes(_provider, monkeypatch):
    _provider.routes[("GET", PAGE_URL)] = _Resp(200, None)
    _provider.routes[("POST", REFRESH_URL)] = _Resp(
        200, {"ok": True, "queued": True, "alliance": STALE_TAG_ENVELOPE["alliance"]})
    _provider.routes[("GET", STATUS_URL)] = _Resp(
        200, {"ok": True, "queued": True, "started": False})
    monkeypatch.setattr(mightpulse, "SITE_REFRESH_MAX_POLLS", 1)

    data = asyncio.run(mightpulse.refresh_site_alliance(
        AID, force=True, tag=OLD_TAG, bypass_local_cache=True))

    assert data is None


class _Settings:
    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def get_int(self, key, default=0):
        try:
            return int(self._values.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_json(self, key, default=None):
        return self._values.get(key, default)


class _Bot:
    def __init__(self):
        self.maintenance_lock = asyncio.Lock()
        self.extension_settings = _Settings(
            {"whitelist_kids": [KID], "nap_alliances_count": 20})


class _SaveRecorder:
    saved = []

    def __init__(self, *_args, **_kwargs):
        pass

    def save(self, data):
        _SaveRecorder.saved.append(data)


def _row(aid, abbr, kid=KID):
    return {"alliance_id": aid, "abbr": abbr, "kid": kid, "power": 1, "name": "n",
            "role_id": None}


def _nightly_cog(monkeypatch, *, rows, fetched, nap_context, rows_fn=None):
    """A NightlyTasks cog with storage/provider/Discord side effects captured."""
    cog = nightly_mod.NightlyTasks(_Bot())

    async def _no_discovery():
        return 0

    async def _fetch(aid, *, kid, tag):
        return fetched, "test"

    upserts = []
    monkeypatch.setattr(cog, "_discover_candidates", _no_discovery)
    monkeypatch.setattr(nightly_mod.mightpulse, "fetch_current_alliance", _fetch)
    monkeypatch.setattr(nightly_mod.storage, "all_alliances",
                        rows_fn or (lambda: rows))
    monkeypatch.setattr(nightly_mod.storage, "upsert_alliance",
                        lambda aid, **kw: upserts.append((aid, kw)))
    monkeypatch.setattr(nightly_mod.storage, "Storage", _SaveRecorder)
    monkeypatch.setattr(nap_cog, "_nap_context", nap_context)
    _SaveRecorder.saved = []
    return cog, upserts


def test_nightly_records_alias_and_new_tag_on_rename(monkeypatch):
    logic = nap_mod.NapLogic(nap_alliances_count=20)
    nap_state = {"nap_tag_aliases": {}}

    def _context(_bot):
        nap_state["nap_tag_aliases"] = logic.nap_tag_aliases
        return logic, nap_state

    cog, upserts = _nightly_cog(
        monkeypatch,
        rows=[_row(AID, OLD_TAG)],
        fetched={"aid": AID, "abbr": NEW_TAG, "name": "theknightsONE", "kid": KID,
                 "power": 1858681076},
        nap_context=_context,
    )

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert upserts == [(AID, {"abbr": NEW_TAG, "name": "theknightsONE", "kid": KID,
                              "power": 1858681076})]
    # The old tag stays resolvable for lookback / exclusions / academy commands.
    assert logic.nap_tag_aliases[OLD_TAG] == AID
    assert _SaveRecorder.saved[-1]["nap_tag_aliases"][OLD_TAG] == AID


def test_nightly_records_alias_even_when_discovery_already_rewrote_the_tag(monkeypatch):
    """Discovery upserts the live board tag first, so the rename must be
    detected against the *pre-discovery* snapshot."""
    logic = nap_mod.NapLogic(nap_alliances_count=20)
    nap_state = {"nap_tag_aliases": {}}

    def _context(_bot):
        nap_state["nap_tag_aliases"] = logic.nap_tag_aliases
        return logic, nap_state

    reads = [
        [_row(AID, OLD_TAG)],   # pre-discovery snapshot
        [_row(AID, NEW_TAG)],   # discovery already rewrote abbr
    ]

    def _all_alliances():
        return reads.pop(0) if reads else [_row(AID, NEW_TAG)]

    cog, upserts = _nightly_cog(
        monkeypatch,
        rows=None,
        fetched={"aid": AID, "abbr": NEW_TAG, "name": "theknightsONE", "kid": KID,
                 "power": 1858681076},
        nap_context=_context,
        rows_fn=_all_alliances,
    )

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert logic.nap_tag_aliases[OLD_TAG] == AID
    assert _SaveRecorder.saved[-1]["nap_tag_aliases"][OLD_TAG] == AID


def test_nightly_reads_no_nap_state_when_nothing_renamed(monkeypatch):
    def _context(_bot):
        raise AssertionError("NAP state must not be loaded when no rename happened")

    cog, upserts = _nightly_cog(
        monkeypatch,
        rows=[_row(AID, NEW_TAG)],
        fetched={"aid": AID, "abbr": NEW_TAG, "name": "theknightsONE", "kid": KID,
                 "power": 1858681076},
        nap_context=_context,
    )

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert upserts[0][1]["abbr"] == NEW_TAG
    assert _SaveRecorder.saved == []


def test_nightly_keeps_previous_values_on_failed_refresh(monkeypatch):
    cog, upserts = _nightly_cog(
        monkeypatch,
        rows=[_row(AID, OLD_TAG)],
        fetched=None,
        nap_context=lambda _bot: (nap_mod.NapLogic(), {}),
    )

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert upserts == []
