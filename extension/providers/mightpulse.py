"""MightPulse data provider (live player / alliance / kingdom-ranking data).

This is the extension's source of live game stats (power, rank, alliance, kingdom
ranking) that the upstream bot no longer has since the game removed the player
API. It talks to api.mightpulse.com with a Bearer key, plus a keyless site-scrape
refresh flow used as a fallback when the public API returns stale data.

State (API key, cache TTLs, allowed kingdom) is injected via ``configure()`` from
the extension config, keeping this module decoupled from environment parsing.
"""

import asyncio
import json
import time as time_module

import aiohttp

MIGHTPULSE_SITE_BASE = "https://mightpulse.com"
SITE_REFRESH_POLL_SECONDS = 2
SITE_REFRESH_MAX_POLLS = 20


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


def normalize_site_player(raw):
    """Convert MightPulse website player JSON to the public API shape."""
    aid = raw.get("aid")
    alliance = None
    if aid:
        alliance = {
            "aid": aid,
            "abbr": raw.get("alliance_abbr"),
            "name": raw.get("alliance_name") or raw.get("alliance_abbr"),
            "rank": raw.get("alliance_rank"),
        }
    return {
        "uid": raw.get("uid"),
        "governor_id": raw.get("fid") or raw.get("governor_id"),
        "nick_name": raw.get("nick_name"),
        "kid": raw.get("kid"),
        "power": raw.get("power", 0),
        "alliance": alliance,
    }


def normalize_site_alliance(raw):
    """Return the alliance fields the rest of the bot uses."""
    return {
        "aid": raw.get("aid"),
        "kid": raw.get("kid"),
        "abbr": raw.get("abbr"),
        "name": raw.get("name") or raw.get("abbr"),
        "power": raw.get("power", 0),
    }


def browser_user_agent():
    return (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/140.0.0.0 Safari/537.36"
    )


def navigation_headers():
    return {
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-User": "?1",
        "Sec-Fetch-Dest": "document",
        "Upgrade-Insecure-Requests": "1",
        "User-Agent": browser_user_agent(),
    }


def api_site_headers(referer):
    return {
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": MIGHTPULSE_SITE_BASE,
        "Referer": referer,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
        "Sec-Fetch-Site": "same-origin",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Dest": "empty",
        "User-Agent": browser_user_agent(),
    }


async def fetch_player_envelope(gid: str) -> dict | str | None:
    """Fetch public player data and preserve freshness + internal UID metadata."""
    url = f"https://api.mightpulse.com/v1/players/{gid}?include=base"
    headers = {"Authorization": f"Bearer {_config.api_key}"}

    try:
        timeout = aiohttp.ClientTimeout(total=100)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    if not data.get("ok"):
                        return None
                    return data
                if resp.status == 404:
                    return "NOT_FOUND"
                if resp.status == 429:
                    return "RATE_LIMITED"
                body = await resp.text()
                print(f"[MIGHTPULSE] Player GET HTTP {resp.status} for gid={gid}: {body[:500]}")
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[MIGHTPULSE] Player GET failed for gid={gid}: {e}")
        return None


async def _open_site_page(session, url, label) -> bool:
    """Open a normal MightPulse HTML page first so the session receives cookies."""
    try:
        async with session.get(url, headers=navigation_headers(), allow_redirects=True) as resp:
            await resp.read()
            print(f"[{label}] HTTP={resp.status} final_url={resp.url}")
            return resp.status == 200
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[{label}] failed: {e}")
        return False


async def get_player_refresh_status(session, uid: int) -> dict | None:
    url = f"{MIGHTPULSE_SITE_BASE}/api/players/{uid}/refresh/status"
    referer = f"{MIGHTPULSE_SITE_BASE}/player/{uid}"
    try:
        async with session.get(url, headers=api_site_headers(referer)) as resp:
            body = await resp.text()
            if resp.status != 200:
                print(f"[MP PLAYER STATUS] uid={uid} HTTP {resp.status}: {body[:500]}")
                return None
            return json.loads(body)
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        print(f"[MP PLAYER STATUS] uid={uid} failed: {e}")
        return None


async def refresh_site_player(uid: int, force: bool) -> dict | None:
    """Mirror MightPulse browser flow: open profile page, POST refresh, poll status."""
    profile_url = f"{MIGHTPULSE_SITE_BASE}/player/{uid}"
    refresh_url = f"{MIGHTPULSE_SITE_BASE}/api/players/{uid}/refresh?force={1 if force else 0}"

    timeout = aiohttp.ClientTimeout(total=120)
    cookie_jar = aiohttp.CookieJar(unsafe=True)

    try:
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=cookie_jar) as session:
            await _open_site_page(session, profile_url, "MP PLAYER PAGE")

            cookies = session.cookie_jar.filter_cookies(MIGHTPULSE_SITE_BASE)
            print(f"[MP PLAYER COOKIES] uid={uid} count={len(cookies)} names={list(cookies.keys())}")

            async with session.post(
                    refresh_url,
                    headers=api_site_headers(profile_url),
                    data=b"",
            ) as resp:
                body = await resp.text()
                print(
                    f"[MP PLAYER REFRESH] uid={uid} force={int(force)} "
                    f"HTTP={resp.status} URL={resp.url} BODY={body[:1000]}"
                )

                if resp.status != 200:
                    return None

                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    return None

            raw_player = data.get("player")
            if isinstance(raw_player, dict):
                return normalize_site_player(raw_player)

            if data.get("queued") or data.get("accepted") or data.get("started"):
                for _ in range(SITE_REFRESH_MAX_POLLS):
                    await asyncio.sleep(SITE_REFRESH_POLL_SECONDS)
                    status = await get_player_refresh_status(session, uid)
                    if not status:
                        continue
                    print(
                        f"[MP PLAYER STATUS] uid={uid} queued={status.get('queued')} "
                        f"started={status.get('started')} position={status.get('position')} "
                        f"wait={status.get('wait_sec')} cooldown={status.get('cooldown_remaining_sec')}"
                    )
                    if not status.get("queued") and not status.get("started"):
                        break

            return None

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[MP PLAYER REFRESH] uid={uid} failed: {e}")
        return None


async def fetch_player_data(
        gid: str,
        *,
        force_refresh: bool = False,
        bypass_local_cache: bool = False,
) -> dict | str | None:
    """Fetch player by Governor ID with local cache + stale-refresh handling."""
    gid = str(gid)

    if not force_refresh and not bypass_local_cache:
        cached = get_cached_player(gid)
        if cached:
            print(f"[LOCAL CACHE] player gid={gid} HIT")
            return cached

    print(f"[LOCAL CACHE] player gid={gid} MISS")
    envelope = await fetch_player_envelope(gid)
    if envelope in ("NOT_FOUND", "RATE_LIMITED") or envelope is None:
        return envelope

    player = envelope.get("player")
    if not isinstance(player, dict):
        return None

    fresh = bool(envelope.get("fresh"))
    internal_uid = envelope.get("uid") or player.get("uid")

    print(
        f"[MIGHTPULSE] gid={gid} fresh={fresh} "
        f"cached_at={envelope.get('cached_at')} "
        f"age_seconds={envelope.get('age_seconds')} "
        f"uid={internal_uid}"
    )

    if fresh:
        cache_player(gid, player)
        return player

    if not internal_uid:
        print(f"[MIGHTPULSE] Stale player gid={gid} has no internal UID; refusing stale update")
        return None

    site_player = await refresh_site_player(int(internal_uid), force=force_refresh)
    if site_player:
        print(f"[MIGHTPULSE] Site refresh returned player uid={internal_uid} force={int(force_refresh)}")
        cache_player(gid, site_player)
        return site_player

    envelope2 = await fetch_player_envelope(gid)
    if isinstance(envelope2, dict) and envelope2.get("fresh") and isinstance(envelope2.get("player"), dict):
        cache_player(gid, envelope2["player"])
        return envelope2["player"]

    age = envelope.get("age_seconds") or 0
    print(
        f"[MIGHTPULSE] gid={gid} public data is stale ({age / 3600:.1f}h old); "
        f"site refresh did not produce trustworthy data; Discord state unchanged"
    )
    return None


def _extract_ranking_rows(payload) -> list[dict]:
    """Best-effort extraction for MightPulse kingdom rank responses."""
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]

    if not isinstance(payload, dict):
        return []

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
    url = (
        f"https://api.mightpulse.com/v1/kingdoms/{kid}/ranks"
        f"?board=alliance_power&limit={limit}"
    )
    headers = {"Authorization": f"Bearer {_config.api_key}"}

    try:
        timeout = aiohttp.ClientTimeout(total=100)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url, headers=headers) as resp:
                body = await resp.text()

                if resp.status == 429:
                    print("[NAP] Kingdom ranking API rate limited.")
                    return None

                if resp.status != 200:
                    print(f"[NAP] Kingdom alliance ranking HTTP {resp.status}: {body[:1000]}")
                    return None

                try:
                    payload = json.loads(body)
                except json.JSONDecodeError:
                    print(f"[NAP] Invalid kingdom ranking JSON: {body[:1000]}")
                    return None

        rows = _extract_ranking_rows(payload)
        normalized = []
        seen_aids = set()

        for row in rows:
            aid = row.get("aid")
            tag = row.get("abbr") or row.get("tag")
            name = row.get("name") or tag
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

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[NAP] Kingdom alliance ranking request failed: {e}")
        return None


async def fetch_alliance_data(
        kid: int,
        tag: str,
        *,
        bypass_local_cache: bool = False,
) -> dict | None:
    """Fetch alliance info from the public MightPulse API (1-hour local cache)."""
    if not bypass_local_cache:
        cached = get_cached_alliance(kid=kid, tag=tag)
        if cached:
            print(f"[LOCAL CACHE] alliance {kid}/{tag} HIT")
            return cached

    print(f"[LOCAL CACHE] alliance {kid}/{tag} MISS")
    url = f"https://api.mightpulse.com/v1/alliances/{kid}/{tag}?include=info"
    headers = {"Authorization": f"Bearer {_config.api_key}"}

    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=headers) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    alliance = data.get("alliance") if data.get("ok") else None
                    if isinstance(alliance, dict):
                        cache_alliance(alliance, kid=kid, tag=tag)
                    return alliance
                body = await resp.text()
                print(f"[MIGHTPULSE] Alliance GET HTTP {resp.status} for {kid}/{tag}: {body[:500]}")
                return None
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[MIGHTPULSE] Alliance GET failed for {kid}/{tag}: {e}")
        return None


async def get_alliance_refresh_status(session, aid: int, referer: str) -> dict | None:
    url = f"{MIGHTPULSE_SITE_BASE}/api/alliances/{aid}/refresh/status"
    try:
        async with session.get(url, headers=api_site_headers(referer)) as resp:
            body = await resp.text()
            if resp.status != 200:
                print(f"[MP ALLIANCE STATUS] aid={aid} HTTP {resp.status}: {body[:500]}")
                return None
            return json.loads(body)
    except (aiohttp.ClientError, asyncio.TimeoutError, json.JSONDecodeError) as e:
        print(f"[MP ALLIANCE STATUS] aid={aid} failed: {e}")
        return None


def alliance_refresh_state(data: dict) -> str:
    """Classify MightPulse alliance refresh status like the player scraper."""
    if data.get("queued") is True or data.get("started") is True:
        return "running"
    try:
        cooldown = float(data.get("cooldown_remaining_sec") or 0)
    except (TypeError, ValueError):
        cooldown = 0.0
    if cooldown > 0:
        return "recent"
    return "stale"


async def fetch_current_alliance(
        aid: int,
        *,
        kid: int,
        tag: str | None,
) -> tuple[dict | None, str]:
    """Get current alliance data without needlessly forcing a fresh refresh."""
    if not tag:
        print(f"[MP ALLIANCE CURRENT] aid={aid} has no tag; cannot query official alliance API")
        return None, "missing-tag"

    page_url = f"{MIGHTPULSE_SITE_BASE}/{kid}/{tag}"
    timeout = aiohttp.ClientTimeout(total=120)
    cookie_jar = aiohttp.CookieJar(unsafe=True)

    try:
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=cookie_jar) as session:
            await _open_site_page(session, page_url, "MP ALLIANCE PAGE")
            status = await get_alliance_refresh_status(session, aid, page_url)
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[MP ALLIANCE CURRENT] aid={aid} status check failed: {e}")
        status = None

    if isinstance(status, dict):
        state = alliance_refresh_state(status)
        print(
            f"[MP ALLIANCE STATUS] aid={aid} state={state} "
            f"cooldown={status.get('cooldown_remaining_sec')} "
            f"queued={status.get('queued')} started={status.get('started')}"
        )

        if state in ("recent", "running"):
            alliance = await fetch_alliance_data(kid, tag, bypass_local_cache=True)
            return alliance, f"official-api-{state}"

        alliance = await refresh_site_alliance(aid, force=True, tag=tag, bypass_local_cache=True)
        return alliance, "site-refresh"

    alliance = await fetch_alliance_data(kid, tag, bypass_local_cache=True)
    if isinstance(alliance, dict):
        return alliance, "official-api-status-unknown"

    alliance = await refresh_site_alliance(aid, force=True, tag=tag, bypass_local_cache=True)
    return alliance, "site-refresh-status-unknown"


async def refresh_site_alliance(
        aid: int,
        *,
        force: bool = False,
        tag: str | None = None,
        bypass_local_cache: bool = False,
) -> dict | None:
    """Mirror MightPulse browser flow for an alliance."""
    kid = _config.allowed_kingdom_id
    if not kid:
        print("[MP ALLIANCE REFRESH] allowed kingdom is not configured")
        return None

    if not force and not bypass_local_cache:
        cached = get_cached_alliance(aid=aid, kid=kid, tag=tag)
        if cached:
            print(f"[LOCAL CACHE] alliance aid={aid} tag={tag!r} HIT")
            return cached

    print(
        f"[LOCAL CACHE] alliance aid={aid} tag={tag!r} "
        f"{'BYPASS' if force or bypass_local_cache else 'MISS'}"
    )

    page_url = f"{MIGHTPULSE_SITE_BASE}/{kid}/{tag}" if tag else f"{MIGHTPULSE_SITE_BASE}/{kid}"
    refresh_url = (
        f"{MIGHTPULSE_SITE_BASE}/api/alliances/{aid}/refresh"
        f"?force={1 if force else 0}&kid={kid}"
    )

    timeout = aiohttp.ClientTimeout(total=120)
    cookie_jar = aiohttp.CookieJar(unsafe=True)

    try:
        async with aiohttp.ClientSession(timeout=timeout, cookie_jar=cookie_jar) as session:
            await _open_site_page(session, page_url, "MP ALLIANCE PAGE")

            cookies = session.cookie_jar.filter_cookies(MIGHTPULSE_SITE_BASE)
            print(f"[MP ALLIANCE COOKIES] aid={aid} count={len(cookies)} names={list(cookies.keys())}")

            async with session.post(
                    refresh_url,
                    headers=api_site_headers(page_url),
                    data=b"",
            ) as resp:
                body = await resp.text()
                print(
                    f"[MP ALLIANCE REFRESH] aid={aid} force={int(force)} "
                    f"HTTP={resp.status} URL={resp.url} BODY={body[:1000]}"
                )

                if resp.status != 200:
                    return None

                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    return None

            raw_alliance = data.get("alliance")
            if isinstance(raw_alliance, dict):
                alliance = normalize_site_alliance(raw_alliance)
                cache_alliance(alliance, aid=aid, kid=kid, tag=tag)
                return alliance

            refresh_completed = False
            if data.get("queued") or data.get("accepted") or data.get("started"):
                for _ in range(SITE_REFRESH_MAX_POLLS):
                    await asyncio.sleep(SITE_REFRESH_POLL_SECONDS)
                    status = await get_alliance_refresh_status(session, aid, page_url)
                    if not status:
                        continue
                    print(
                        f"[MP ALLIANCE STATUS] aid={aid} queued={status.get('queued')} "
                        f"started={status.get('started')} position={status.get('position')} "
                        f"wait={status.get('wait_sec')} cooldown={status.get('cooldown_remaining_sec')}"
                    )
                    if not status.get("queued") and not status.get("started"):
                        refresh_completed = True
                        break

            if refresh_completed and tag:
                for attempt in range(1, 4):
                    await asyncio.sleep(1.0 if attempt == 1 else 2.0)
                    refreshed = await fetch_alliance_data(kid, tag, bypass_local_cache=True)
                    if isinstance(refreshed, dict):
                        refreshed["aid"] = refreshed.get("aid") or aid
                        refreshed["kid"] = refreshed.get("kid") or kid
                        cache_alliance(refreshed, aid=aid, kid=kid, tag=tag)
                        print(
                            f"[MP ALLIANCE REFRESH] aid={aid} completed; "
                            f"public API re-read succeeded on attempt {attempt}."
                        )
                        return refreshed

                print(
                    f"[MP ALLIANCE REFRESH] aid={aid} completed, but public API "
                    f"did not return alliance data after retries."
                )

            return None

    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        print(f"[MP ALLIANCE REFRESH] aid={aid} failed: {e}")
        return None
