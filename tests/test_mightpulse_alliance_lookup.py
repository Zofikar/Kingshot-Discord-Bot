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
        self.guilds = []
        self.extension_settings = _Settings(
            {"whitelist_kids": [KID], "nap_alliances_count": 20})


class _SaveRecorder:
    saved = []

    def __init__(self, *_args, **_kwargs):
        pass

    def save(self, data):
        _SaveRecorder.saved.append(data)


def _row(aid, abbr, kid=KID, *, role_id=None, name="n"):
    return {"alliance_id": aid, "abbr": abbr, "kid": kid, "power": 1, "name": name,
            "role_id": role_id}


def _nightly_cog(monkeypatch, *, rows, fetched, nap_context, rows_fn=None, linked=None):
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
    monkeypatch.setattr(nightly_mod.storage, "discord_ids_for_alliances",
                        linked or (lambda aids: []))
    monkeypatch.setattr(nightly_mod.storage, "linked_main_accounts", lambda: [])
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


# ── Discord side of a rename: role name + member `[TAG]` prefixes ───────────

ROLE_ID = 777700


class _Role:
    def __init__(self, role_id, name):
        self.id = role_id
        self.name = name
        self.position = 5
        self.renames = []

    async def edit(self, *, name=None, reason=None):
        self.renames.append(name)
        if name is not None:
            self.name = name
        return self


class _Member:
    def __init__(self, member_id, nick):
        self.id = member_id
        self.guild = None
        self.nick = nick
        self.bot = False
        self.roles = []
        self.nick_edits = []

    async def add_roles(self, *roles, reason=None):
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles, reason=None):
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)

    async def edit(self, *, nick=None, reason=None):
        self.nick_edits.append(nick)
        if nick is not None:
            self.nick = nick


class _Guild:
    def __init__(self, roles=(), members=(), cached=True):
        self._roles = {r.id: r for r in roles}
        self._members = {m.id: m for m in members}
        self.created = []
        self.cached = cached          # False = member cache not filled yet
        for member in self._members.values():
            member.guild = self

    @property
    def members(self):
        return list(self._members.values())

    def get_role(self, role_id):
        return self._roles.get(role_id)

    def get_member(self, member_id):
        return self._members.get(member_id) if self.cached else None

    async def fetch_member(self, member_id):
        return self._members.get(member_id)

    async def create_role(self, *, name, mentionable=True, reason=None):
        role = _Role(max(self._roles, default=0) + 1000, name)
        self._roles[role.id] = role
        self.created.append(role)
        return role


def _wire_identity(monkeypatch, *, rows, people):
    """Point the identity cog's storage reads at the fake guild fixtures.

    ``people`` = [(discord_id, fid, in-game name), ...]; every fid belongs to the
    first ``rows`` entry's alliance. Returns the ``discord_ids_for_alliances``
    stand-in for the nightly task.
    """
    from extension.cogs import identity as identity_mod

    by_aid = {str(r["alliance_id"]): r for r in rows}
    aid = rows[0]["alliance_id"]
    fid_aid = {fid: aid for _did, fid, _name in people}
    fid_name = {fid: name for _did, fid, name in people}
    discord_fid = {did: fid for did, fid, _name in people}

    monkeypatch.setattr(identity_mod.storage, "alliance_row",
                        lambda aid_: by_aid.get(str(aid_)))
    monkeypatch.setattr(identity_mod.storage, "user_row",
                        lambda fid: {"nickname": fid_name.get(fid), "rank": 3,
                                     "alliance": fid_aid.get(fid)})
    monkeypatch.setattr(identity_mod.storage, "main_fid_for_discord",
                        lambda did: discord_fid.get(did))
    monkeypatch.setattr(identity_mod.storage, "top_alliances_by_power", lambda n: [])
    monkeypatch.setattr(identity_mod.storage, "all_alliance_role_ids",
                        lambda: [r["role_id"] for r in rows if r["role_id"]])

    def _linked(aids):
        wanted = {str(a) for a in aids}
        return [did for did, fid, _name in people if str(fid_aid[fid]) in wanted]

    return _linked


def _fresh(aid=AID, tag=NEW_TAG, name="theknightsONE"):
    return {"aid": aid, "abbr": tag, "name": name, "kid": KID, "power": 1858681076}


def _renamed_cog(monkeypatch, *, rows, guild, people, fetched=None, nap_context=None):
    cog, upserts = _nightly_cog(
        monkeypatch,
        rows=rows,
        fetched=fetched if fetched is not None else _fresh(),
        nap_context=nap_context or (lambda _bot: (nap_mod.NapLogic(), {})),
        linked=_wire_identity(monkeypatch, rows=rows, people=people),
    )
    cog.bot.guilds = [guild]
    return cog, upserts


def test_nightly_renames_alliance_role_and_member_tags(monkeypatch):
    """MNX -> TKO must reach Discord: role name *and* every member's prefix."""
    rows = [_row(AID, NEW_TAG, role_id=ROLE_ID, name="theknightsONE")]
    role = _Role(ROLE_ID, f"[{OLD_TAG}] FAMILLY")     # what Discord still shows
    frodo = _Member(11, f"[{OLD_TAG}] Frodo")         # stale tag in his name
    sam = _Member(12, f"[{NEW_TAG}] Sam")             # already re-tagged
    guild = _Guild([role], [frodo, sam])

    cog, _upserts = _renamed_cog(
        monkeypatch, rows=rows, guild=guild,
        people=[(11, 5001, "Frodo"), (12, 5002, "Sam")])

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert role.name == f"[{NEW_TAG}] theknightsONE"
    assert role.renames == [f"[{NEW_TAG}] theknightsONE"]
    assert frodo.nick == f"[{NEW_TAG}] Frodo"
    assert frodo.nick_edits == [f"[{NEW_TAG}] Frodo"]
    assert frodo.roles == [role]        # the alliance role is re-applied
    assert sam.nick_edits == []         # already tagged -> untouched


def test_nightly_fixes_member_tags_when_only_they_lagged(monkeypatch):
    """A member who re-synced early renames the role; the rest still need a fix."""
    rows = [_row(AID, NEW_TAG, role_id=ROLE_ID, name="theknightsONE")]
    role = _Role(ROLE_ID, f"[{NEW_TAG}] theknightsONE")   # role already correct
    frodo = _Member(11, f"[{OLD_TAG}] Frodo")             # ...but his tag is not
    guild = _Guild([role], [frodo])

    cog, _upserts = _renamed_cog(
        monkeypatch, rows=rows, guild=guild, people=[(11, 5001, "Frodo")])

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert frodo.nick == f"[{NEW_TAG}] Frodo"
    assert role.renames == []           # no pointless second rename


def test_nightly_discord_sync_is_idempotent(monkeypatch):
    rows = [_row(AID, NEW_TAG, role_id=ROLE_ID, name="theknightsONE")]
    role = _Role(ROLE_ID, f"[{NEW_TAG}] theknightsONE")
    frodo = _Member(11, f"[{NEW_TAG}] Frodo")
    guild = _Guild([role], [frodo])

    cog, _upserts = _renamed_cog(
        monkeypatch, rows=rows, guild=guild, people=[(11, 5001, "Frodo")])

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert role.renames == [] and frodo.nick_edits == [] and guild.created == []


def test_nightly_leaves_metadata_only_alliances_alone(monkeypatch):
    """Discovery rows without a role (nobody registered yet) create no role."""
    rows = [_row(AID, OLD_TAG, role_id=None)]
    guild = _Guild()

    cog, _upserts = _renamed_cog(
        monkeypatch, rows=rows, guild=guild, people=[(11, 5001, "Frodo")])

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert guild.created == []


def test_nightly_does_not_nickname_members_without_a_tag(monkeypatch):
    """Only nicknames already carrying a stale tag are touched (no mass renames)."""
    rows = [_row(AID, NEW_TAG, role_id=ROLE_ID, name="theknightsONE")]
    role = _Role(ROLE_ID, f"[{NEW_TAG}] theknightsONE")   # nothing left to rename
    frodo = _Member(11, "Frodo")                          # plain nickname
    sam = _Member(12, None)                               # no nickname at all
    guild = _Guild([role], [frodo, sam])

    cog, _upserts = _renamed_cog(
        monkeypatch, rows=rows, guild=guild,
        people=[(11, 5001, "Frodo"), (12, 5002, "Sam")])

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert frodo.nick_edits == [] and sam.nick_edits == []


def test_nightly_retags_unlinked_members_from_the_recorded_alias(monkeypatch):
    """Players who never linked a FID still carry the retired tag in their name.

    The alias recorded when the rename was detected says `[MNX]` belonged to this
    alliance, so the prefix is rewritten in place — the rest of the name, and the
    member's own separator, are preserved.
    """
    rows = [_row(AID, NEW_TAG, role_id=ROLE_ID, name="theknightsONE")]
    role = _Role(ROLE_ID, f"[{NEW_TAG}] theknightsONE")   # role side already fine
    frodo = _Member(11, f"[{NEW_TAG}] Frodo")
    gollum = _Member(98, f"[{OLD_TAG}] Gollum")           # unlinked, spaced tag
    smeagol = _Member(99, f"[{OLD_TAG}]Smeagol")          # unlinked, no space
    guild = _Guild([role], [frodo, gollum, smeagol])
    aliases = {OLD_TAG: AID}

    cog, _upserts = _renamed_cog(
        monkeypatch, rows=rows, guild=guild, people=[(11, 5001, "Frodo")],
        nap_context=lambda _bot: (nap_mod.NapLogic(nap_tag_aliases=dict(aliases)),
                                  {"nap_tag_aliases": aliases}))

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert gollum.nick == f"[{NEW_TAG}] Gollum"
    assert smeagol.nick == f"[{NEW_TAG}]Smeagol"
    assert frodo.nick_edits == []          # already correct -> untouched


def test_nightly_retags_linked_member_missing_from_the_member_cache(monkeypatch):
    """A cache miss must not leave a linked member's tag stale."""
    rows = [_row(AID, NEW_TAG, role_id=ROLE_ID, name="theknightsONE")]
    role = _Role(ROLE_ID, f"[{NEW_TAG}] theknightsONE")
    frodo = _Member(11, f"[{OLD_TAG}] Frodo")
    guild = _Guild([role], [frodo], cached=False)

    cog, _upserts = _renamed_cog(
        monkeypatch, rows=rows, guild=guild, people=[(11, 5001, "Frodo")])

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert frodo.nick == f"[{NEW_TAG}] Frodo"


def test_nightly_leaves_unknown_tags_and_bots_alone(monkeypatch):
    """Only tags the bot can resolve are rewritten, and never for a bot user."""
    rows = [_row(AID, NEW_TAG, role_id=ROLE_ID, name="theknightsONE")]
    role = _Role(ROLE_ID, f"[{NEW_TAG}] theknightsONE")
    guest = _Member(96, "[XYZ] Pippin")                 # tag nobody knows
    sam = _Member(12, f"[{NEW_TAG}] Sam")               # already current
    robot = _Member(97, f"[{OLD_TAG}] Robot")
    robot.bot = True
    guild = _Guild([role], [guest, sam, robot])
    aliases = {OLD_TAG: AID}

    cog, _upserts = _renamed_cog(
        monkeypatch, rows=rows, guild=guild, people=[],
        nap_context=lambda _bot: (nap_mod.NapLogic(nap_tag_aliases=dict(aliases)),
                                  {"nap_tag_aliases": aliases}))

    asyncio.run(cog._run_nightly_maintenance(triggered_by="test"))

    assert guest.nick_edits == []
    assert sam.nick_edits == []
    assert robot.nick_edits == []
