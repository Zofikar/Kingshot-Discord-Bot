"""MightPulse data provider (live player / alliance / kingdom-ranking data).

This is the extension's source of live game stats (power, rank, alliance, kingdom
ranking) that the upstream bot no longer has since the game removed the player
API. Reads use api.mightpulse.com with a Bearer key. Nightly recovery can
explicitly use undocumented website routes in mightpulse_website_unofficial.
Pending updates are awaited before data is used to change Discord state.

State (API key, cache TTLs, allowed kingdom) is injected via ``configure()`` from
the extension config, keeping this module decoupled from environment parsing.
"""

import time as time_module
import asyncio


from .mightpulse_api import API_BASE, API_WAIT_SECONDS, API_MAX_ATTEMPTS, MightPulseAPI
from .mightpulse_unofficial import MightPulseUnofficialAPI
from .mightpulse_website_unofficial import force_alliance_refresh
API_MAX_DATA_AGE_SECONDS = 3600
NIGHTLY_MAX_DATA_AGE_SECONDS = 60
NIGHTLY_PUBLICATION_MAX_AGE_SECONDS = 600


class _Config:
    api_key: str | None = None
    player_cache_ttl_seconds: int = 900
    alliance_cache_ttl_seconds: int = 3600
    allowed_kingdom_id: int | None = None


_config = _Config()


def configure(config) -> None:
    """Inject runtime config (API key + TTLs + allowed kingdom)."""
    _config.api_key = config.mightpulse_api_key
    _config.player_cache_ttl_seconds = config.player_cache_ttl_seconds
    _config.alliance_cache_ttl_seconds = config.alliance_cache_ttl_seconds
    _config.allowed_kingdom_id = config.allowed_kingdom_id


def available() -> bool:
    """True when live game-data actions can run (a MightPulse key is set)."""
    return bool(_config.api_key)


# Process-local trustworthy-data cache (never persisted across restarts).
_PLAYER_CACHE: dict[str, tuple[float, dict]] = {}
_ALLIANCE_CACHE_BY_AID: dict[int, tuple[float, dict]] = {}
_ALLIANCE_CACHE_BY_KEY: dict[str, tuple[float, dict]] = {}


def _cache_get(cache, key, ttl_seconds):
    entry = cache.get(key)
    if not entry:
        return None
    stored_at, value = entry
    age = time_module.monotonic() - stored_at
    if age >= ttl_seconds:
        cache.pop(key, None)
        return None
    return dict(value)


def _cache_put(cache, key, value):
    cache[key] = (time_module.monotonic(), dict(value))


def _alliance_cache_key(kid, tag):
    if kid is None or not tag:
        return None
    return f"{int(kid)}:{str(tag).strip().upper()}"


def cache_player(gid, player):
    if isinstance(player, dict):
        _cache_put(_PLAYER_CACHE, str(gid), player)


def get_cached_player(gid):
    return _cache_get(_PLAYER_CACHE, str(gid), _config.player_cache_ttl_seconds)


def cache_alliance(alliance, *, aid=None, kid=None, tag=None):
    if not isinstance(alliance, dict):
        return
    resolved_aid = alliance.get("aid") if alliance.get("aid") is not None else aid
    resolved_kid = alliance.get("kid") if alliance.get("kid") is not None else kid
    resolved_tag = alliance.get("abbr") or tag
    if resolved_aid is not None:
        try:
            _cache_put(_ALLIANCE_CACHE_BY_AID, int(resolved_aid), alliance)
        except (TypeError, ValueError):
            pass
    key = _alliance_cache_key(resolved_kid, resolved_tag)
    if key:
        _cache_put(_ALLIANCE_CACHE_BY_KEY, key, alliance)


def get_cached_alliance(*, aid=None, kid=None, tag=None):
    if aid is not None:
        try:
            cached = _cache_get(_ALLIANCE_CACHE_BY_AID, int(aid), _config.alliance_cache_ttl_seconds)
            if cached:
                return cached
        except (TypeError, ValueError):
            pass
    key = _alliance_cache_key(kid, tag)
    if key:
        return _cache_get(_ALLIANCE_CACHE_BY_KEY, key, _config.alliance_cache_ttl_seconds)
    return None


def _usable_envelope(data):
    """Never promote a pending/failed update's embedded snapshot to current data."""
    if not isinstance(data, dict) or not data.get("ok"):
        return False
    state = data.get("refresh")
    if data.get("pending") or state in ("queued", "running", "failed", "expired", "skipped"):
        return False
    if data.get("fresh") is True:
        return True
    if state == "cooldown":
        try:
            age = float(data["age_seconds"])
            return 0 <= age <= API_MAX_DATA_AGE_SECONDS
        except (KeyError, TypeError, ValueError):
            return False
    return False


def _usable_nightly_envelope(data):
    """Nightly needs a dated snapshot, not the API's default `fresh` label."""
    if not _usable_envelope(data):
        return False
    try:
        age = float(data["age_seconds"])
        captured_at = float(data["cached_at"])
        actual_age = time_module.time() - captured_at
        return (0 <= age <= NIGHTLY_PUBLICATION_MAX_AGE_SECONDS
                and -5 <= actual_age <= NIGHTLY_PUBLICATION_MAX_AGE_SECONDS)
    except (KeyError, TypeError, ValueError, OverflowError):
        return False




async def fetch_player_envelope(gid: str) -> dict | str | None:
    """Read by Governor ID and wait for API-managed refreshes when necessary."""
    client = MightPulseAPI(_config.api_key)
    return await client.await_envelope(client.player, gid, include="base", wait=API_WAIT_SECONDS, usable=_usable_envelope)


async def fetch_player_data(
        gid: str,
        *,
        force_refresh: bool = False,
        bypass_local_cache: bool = False,
) -> dict | str | None:
    """Fetch player by Governor ID with local cache + stale-refresh handling.

    Both flags skip the local cache. The API refreshes stale data subject to
    its minimum update age; neither flag bypasses the server cooldown. Both
    populate the cache only when trustworthy data is returned.
    """
    gid = str(gid)

    if not force_refresh and not bypass_local_cache:
        cached = get_cached_player(gid)
        if cached:
            print(f"[LOCAL CACHE] player gid={gid} HIT")
            return cached

    if force_refresh or bypass_local_cache:
        print(
            f"[LOCAL CACHE] player gid={gid} BYPASS "
            f"force_refresh={int(force_refresh)} bypass_local_cache={int(bypass_local_cache)}"
        )
    else:
        print(f"[LOCAL CACHE] player gid={gid} MISS")
    envelope = await fetch_player_envelope(gid)
    if envelope in ("NOT_FOUND", "RATE_LIMITED") or envelope is None:
        return envelope

    player = envelope.get("player")
    if not _usable_envelope(envelope) or not isinstance(player, dict):
        return None
    cache_player(gid, player)
    return player



def _extract_ranking_rows(payload) -> list[dict]:
    """Best-effort extraction for MightPulse kingdom rank responses."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]

    if not isinstance(payload, dict):
        return []

    board = payload.get("board")
    if isinstance(board, dict) and board.get("key") == "alliance_power":
        rows = board.get("rows")
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    boards = payload.get("boards")
    if isinstance(boards, list):
        for board in boards:
            if isinstance(board, dict) and board.get("key") == "alliance_power":
                rows = board.get("rows")
                if isinstance(rows, list):
                    return [row for row in rows if isinstance(row, dict)]

    for key in ("ranks", "rows", "results", "entries", "items", "leaderboard", "board"):
        value = payload.get(key)
        if isinstance(value, list):
            return [row for row in value if isinstance(row, dict)]
        if isinstance(value, dict):
            for nested_key in ("rows", "results", "entries", "items", "ranks"):
                nested = value.get(nested_key)
                if isinstance(nested, list):
                    return [row for row in nested if isinstance(row, dict)]

    kingdom = payload.get("kingdom")
    if isinstance(kingdom, dict):
        boards = kingdom.get("boards")
        if isinstance(boards, dict):
            board = boards.get("alliance_power")
            if isinstance(board, list):
                return [row for row in board if isinstance(row, dict)]
            if isinstance(board, dict):
                for key in ("rows", "entries", "items", "ranks"):
                    value = board.get(key)
                    if isinstance(value, list):
                        return [row for row in value if isinstance(row, dict)]

    return []


async def fetch_kingdom_alliance_power_ranking(
        kid: int,
        *,
        limit: int = 100,
) -> list[dict] | None:
    """Fetch kingdom-wide alliance power ranking (the NAP leaderboard source)."""
    limit = max(1, min(int(limit), 100))
    client = MightPulseAPI(_config.api_key, base_url=API_BASE)

    response = await client.kingdom_ranks(kid, board="alliance_power", limit=limit)
    if response.status != 200 or response.data is None:
        return None
    payload = response.data
    rows = _extract_ranking_rows(payload)
    normalized = []
    seen_aids = set()

    for row in rows:
        aid = row.get("aid")
        tag = row.get("abbr") or row.get("tag")
        name = row.get("name") or tag
        # The leaderboard "score" is the authoritative alliance power; the
        # separate "power" field can be wildly off (e.g. ~9.5M vs ~1.8B
        # in-game). Prefer "score", matching the monolith.
        power = row.get("score")
        if power is None:
            power = row.get("power")

        try:
            aid_key = int(aid) if aid is not None else None
            power_value = int(power or 0)
        except (TypeError, ValueError):
            continue

        if aid_key is None or not tag or power_value <= 0:
            continue
        if aid_key in seen_aids:
            continue

        seen_aids.add(aid_key)
        normalized.append({
            "aid": aid_key,
            "abbr": str(tag),
            "name": str(name or tag),
            "power": power_value,
            "raw_rank": row.get("rank"),
        })

    if normalized and all(row.get("raw_rank") is not None for row in normalized):
        try:
            normalized.sort(key=lambda x: int(x["raw_rank"]))
        except (TypeError, ValueError):
            pass

    print(f"[NAP] Kingdom leaderboard returned {len(normalized)} valid alliance-power rows for kingdom {kid}.")
    return normalized


async def fetch_alliance_data(
        kid: int,
        tag: str,
        *,
        bypass_local_cache: bool = False,
        require_recent: bool = False,
) -> dict | None:
    """Fetch alliance info from the public MightPulse API (1-hour local cache)."""
    if not bypass_local_cache and not require_recent:
        cached = get_cached_alliance(kid=kid, tag=tag)
        if cached:
            print(f"[LOCAL CACHE] alliance {kid}/{tag} HIT")
            return cached

    print(f"[LOCAL CACHE] alliance {kid}/{tag} MISS")
    # Tags are case-sensitive and can contain URL-reserved characters.
    client = MightPulseAPI(_config.api_key)
    usable = _usable_nightly_envelope if require_recent else _usable_envelope
    params = {"max_age": NIGHTLY_MAX_DATA_AGE_SECONDS} if require_recent else {}
    data = await client.await_envelope(client.alliance, kid, tag, include="info", wait=API_WAIT_SECONDS, usable=usable, **params)
    if not usable(data):
        return None
    alliance = data.get("alliance")
    if isinstance(alliance, dict):
        if require_recent:
            alliance = dict(alliance, snapshot_cached_at=float(data["cached_at"]))
        cache_alliance(alliance, kid=kid, tag=tag)
        return alliance
    return None


async def fetch_alliance_by_aid(
        aid: int,
        *,
        bypass_local_cache: bool = False,
        require_recent: bool = False,
) -> dict | None:
    """Fetch alliance info by *alliance id* from the public MightPulse API.

    Tag-keyed reads return
    ``{"ok": false, "error": "alliance_not_found"}`` as soon as an alliance
    renames, because the stored tag no longer exists. Alliance ids never change,
    so this endpoint keeps working across renames and also reports the current
    tag/name (used to detect the rename and record a historical tag alias).
    """
    if not bypass_local_cache and not require_recent:
        cached = get_cached_alliance(aid=aid)
        if cached:
            print(f"[LOCAL CACHE] alliance aid={aid} HIT")
            return cached

    print(f"[LOCAL CACHE] alliance aid={aid} MISS")
    client = MightPulseUnofficialAPI(_config.api_key)
    usable = _usable_nightly_envelope if require_recent else _usable_envelope
    params = {"max_age": NIGHTLY_MAX_DATA_AGE_SECONDS} if require_recent else {}
    data = await client.await_envelope(client.alliance_by_id, aid, include="info", wait=API_WAIT_SECONDS, usable=usable, **params)
    if not usable(data):
        return None
    alliance = data.get("alliance")
    if not isinstance(alliance, dict):
        return None
    alliance = dict(alliance)
    if require_recent:
        alliance["snapshot_cached_at"] = float(data["cached_at"])
    if alliance.get("aid") is None:
        alliance["aid"] = data.get("aid") or int(aid)
    if alliance.get("kid") is None:
        alliance["kid"] = data.get("kid")
    if alliance.get("abbr") is None:
        alliance["abbr"] = data.get("tag")
    cache_alliance(alliance, aid=aid, kid=data.get("kid"))
    return alliance


async def read_official_alliance(aid: int, *, kid: int | None = None, tag: str | None = None, require_recent: bool = False) -> dict | None:
    """Aid-first official-API read, falling back to the legacy tag route.

    The id lookup is authoritative and rename-proof; the tag lookup only stays
    as a fallback for entries that predate the id endpoint.
    """
    params = {"require_recent": True} if require_recent else {}
    alliance = await fetch_alliance_by_aid(aid, bypass_local_cache=True, **params)
    if isinstance(alliance, dict):
        return alliance
    if tag and kid:
        alliance = await fetch_alliance_data(int(kid), tag, bypass_local_cache=True, **params)
        if isinstance(alliance, dict):
            return alliance
    return None


async def fetch_current_alliance(
        aid: int,
        *,
        kid: int,
        tag: str | None,
) -> tuple[dict | None, str]:
    """Target 60-second data; recover via explicitly unofficial website routes.

Only dated snapshots within the ten-minute publication budget are accepted.
Website responses themselves are never treated as verified power snapshots.
"""
    alliance = await read_official_alliance(aid, kid=kid, tag=tag, require_recent=True)
    if isinstance(alliance, dict):
        return alliance, "official-api"
    if await force_alliance_refresh(aid, kid=kid, tag=tag):
        for attempt in range(3):
            await asyncio.sleep(1 if attempt == 0 else 2)
            alliance = await read_official_alliance(aid, kid=kid, tag=tag, require_recent=True)
            if isinstance(alliance, dict):
                return alliance, "unofficial-website-refresh"
    return None, "unofficial-website-refresh-failed"
