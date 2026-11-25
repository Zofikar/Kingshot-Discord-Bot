"""Raw documented MightPulse API. No caching or freshness policy.

Contract reference: https://api.mightpulse.com/ (reviewed 2026-11-25).
Member expansion is opt-in: members_base/full consume extra quota.
"""
import asyncio
import time
from datetime import datetime
from dataclasses import dataclass
from urllib.parse import urlencode, quote

import aiohttp

API_BASE = "https://api.mightpulse.com/v1"
API_WAIT_SECONDS = 30
API_MAX_ATTEMPTS = 3
API_MAX_RETRY_SECONDS = 60


class AccountLimits:
    """Shared process-local quota state; server allowances override tier defaults."""

    def __init__(self):
        self.mighty = None
        self.last_usage = None
        now = time.time()
        self.buckets = {"minute": (60, (int(now) // 60 + 1) * 60),
                        "daily": (5000, (int(now) // 86400 + 1) * 86400)}
        self.capacities = {"minute": 60, "daily": 5000}
        self.blocked_until = 0
        self.lock = asyncio.Lock()

    async def acquire(self, cost=1):
        async with self.lock:
            now = time.time()
            for name, (_, reset) in list(self.buckets.items()):
                if reset <= now:
                    period = 60 if name == "minute" else 86400
                    self.buckets[name] = (self.capacities[name], (int(now) // period + 1) * period)
            delay = max([self.blocked_until - now] + [
                reset - now for remaining, reset in self.buckets.values()
                if remaining < cost and reset > now
            ])
            if delay > 0:
                # Do not hold a nightly job asleep until tomorrow.
                if delay > API_MAX_RETRY_SECONDS:
                    return False
                await asyncio.sleep(delay)
            now = time.time()
            for name, (remaining, reset) in list(self.buckets.items()):
                if reset <= now:
                    period = 60 if name == "minute" else 86400
                    remaining, reset = self.capacities[name], (int(now) // period + 1) * period
                if reset > now:
                    self.buckets[name] = (max(0, remaining - cost), reset)
            return True

    def observe(self, response):
        headers = {k.lower(): v for k, v in response.headers.items()}
        for name, suffix in (("minute", "Minute"), ("daily", "Day")):
            try:
                remaining = int(headers[f"x-ratelimit-remaining-{suffix.lower()}"])
                reset = float(headers[f"x-ratelimit-reset-{suffix.lower()}"])
                self.buckets[name] = (remaining, reset)
                self.capacities[name] = int(headers[f"x-ratelimit-limit-{suffix.lower()}"])
            except (KeyError, ValueError, TypeError):
                pass
        if response.status == 429:
            try:
                self.blocked_until = time.time() + max(0, float(headers["retry-after"]))
            except (KeyError, ValueError, TypeError):
                pass

    def usage(self, data):
        if isinstance(data.get("mighty"), bool):
            self.mighty = data["mighty"]
        self.last_usage = dict(data)
        for name in ("minute", "daily"):
            try:
                bucket = data[name]
                reset = datetime.fromisoformat(bucket["resets_at"].replace("Z", "+00:00")).timestamp()
                self.buckets[name] = (int(bucket["remaining"]), reset)
                self.capacities[name] = int(bucket["limit"])
            except (KeyError, ValueError, TypeError):
                pass


_ACCOUNT_LIMITS = {}


def _retry_delay(value):
    try:
        delay = float(value)
        if 0 <= delay <= API_MAX_RETRY_SECONDS:
            return max(1.0, delay)
    except (TypeError, ValueError):
        pass
    return None


@dataclass(frozen=True)
class ApiResponse:
    status: int
    headers: dict
    data: object


class MightPulseAPI:
    """One raw GET per call; errors and pending states remain visible to callers.

    mighty is unknown until usage is fetched successfully. Recheck usage to
    observe subscription changes; no paid endpoint is silently skipped.
    """

    def __init__(self, api_key, *, base_url=API_BASE):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self._limits = _ACCOUNT_LIMITS.setdefault((self.base_url, api_key), AccountLimits())

    @property
    def mighty(self):
        return self._limits.mighty

    @property
    def last_usage(self):
        return self._limits.last_usage

    def open(self, session, path, **params):
        query = urlencode({key: value for key, value in params.items() if value is not None}, safe=",")
        url = f"{self.base_url}/{path}"
        if query:
            url += "?" + query
        return session.get(url, headers={"Authorization": f"Bearer {self.api_key}"})

    async def request(self, path, *, session=None, **params):
        if session is None:
            try:
                async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75)) as owned_session:
                    return await self.request(path, session=owned_session, **params)
            except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                return ApiResponse(503, {}, None)
        cost = len(str(params.get("ids", "")).split(",")) if path == "players" else 1
        expansion = any(section in str(params.get("include", "")).split(",")
                        for section in ("members_base", "members_full"))
        if path != "usage" and not await self._limits.acquire(cost):
            return ApiResponse(429, {}, {"ok": False, "error": "local_quota_exhausted"})
        async with self.open(session, path, **params) as response:
            # Non-success responses may be HTML or empty (e.g. a proxy error).
            try:
                data = await response.json()
            except (ValueError, aiohttp.ContentTypeError):
                data = None
            result = ApiResponse(response.status, dict(response.headers), data)
        self._limits.observe(result)
        if expansion and result.status in (200, 202):
            # Expansion consumes the remaining minute even without quota headers.
            _, reset = self._limits.buckets["minute"]
            self._limits.buckets["minute"] = (0, reset)
        if path == "usage" and result.status == 200 and isinstance(data, dict) and data.get("ok"):
            self._limits.usage(data)
        return result

    async def await_envelope(self, operation, *args, usable, **params):
        """Execute a named endpoint, awaiting pending updates with bounded retries."""
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=75)) as session:
            for attempt in range(API_MAX_ATTEMPTS):
                delay = 2.0
                try:
                    response = await operation(*args, session=session, **params)
                    if response.status == 404:
                        return "NOT_FOUND"
                    if response.status == 429:
                        delay = _retry_delay(response.headers.get("Retry-After"))
                        if delay is None or attempt == API_MAX_ATTEMPTS - 1:
                            return "RATE_LIMITED"
                    elif response.status in (200, 202):
                        data = response.data
                        if not isinstance(data, dict):
                            return None
                        if response.status == 200 and usable(data):
                            return data
                        pending = response.status == 202 or data.get("pending") or data.get("refresh") in ("queued", "running", "skipped")
                        if not pending:
                            return None
                        delay = _retry_delay(data.get("retry_after", response.headers.get("Retry-After", 2)))
                        if delay is None:
                            return None
                    elif response.status < 500:
                        return None
                except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
                    pass
                if attempt < API_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(delay)
        return None

    async def usage(self):
        return await self.request("usage")

    async def player(self, player_id, *, include=None, id_type=None, wait=None, max_age=None, session=None):
        return await self.request(f"players/{quote(str(player_id), safe='')}", include=include, id_type=id_type, wait=wait, max_age=max_age, session=session)

    async def players(self, ids, *, include=None, id_type=None, wait=None, max_age=None):
        ids = list(ids)
        if not 1 <= len(ids) <= 50:
            raise ValueError("Expected 1 to 50 player IDs")
        return await self.request("players", ids=",".join(map(str, ids)), include=include, id_type=id_type, wait=wait, max_age=max_age)

    async def alliance(self, kid, tag, *, include=None, wait=None, max_age=None, session=None):
        return await self.request(f"alliances/{int(kid)}/{quote(str(tag), safe='')}", include=include, wait=wait, max_age=max_age, session=session)

    async def kingdoms(self, *, page=None, size=None):
        return await self.request("kingdoms", page=page, size=size)

    async def kingdom(self, kid, *, include=None, limit=None):
        return await self.request(f"kingdoms/{int(kid)}", include=include, limit=limit)

    async def kingdom_ranks(self, kid, *, board=None, limit=None):
        return await self.request(f"kingdoms/{int(kid)}/ranks", board=board, limit=limit)

    async def kingdom_events(self, kid):
        return await self.request(f"kingdoms/{int(kid)}/events")

    async def kingdom_kvk(self, kid):
        return await self.request(f"kingdoms/{int(kid)}/kvk")

    async def kingdom_kvk_scores(self, kid):
        return await self.request(f"kingdoms/{int(kid)}/kvk/scores")

    async def kvk_matchups(self):
        return await self.request("kvk/matchups")
