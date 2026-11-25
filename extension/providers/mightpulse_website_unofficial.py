"""UNOFFICIAL website-only nightly recovery. Not part of the official API.

These undocumented browser routes can change without notice. Never trust their
embedded alliance payload: callers must reread and verify a dated API snapshot.
No Bearer credentials are sent to the website.
"""
import asyncio
import json
import logging
from urllib.parse import quote

import aiohttp

MIGHTPULSE_SITE_BASE = "https://mightpulse.com"
logger = logging.getLogger("extension")

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


async def _open_site_page(session, url, label):
    async with session.get(url, headers=navigation_headers(), allow_redirects=True) as response:
        await response.read()
        return response.status == 200


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


async def force_alliance_refresh(aid, *, kid, tag):
    """Trigger the legacy browser refresh and await its bounded status poll."""
    page = f"{MIGHTPULSE_SITE_BASE}/{int(kid)}"
    if tag:
        page += "/" + quote(str(tag), safe="")
    url = f"{MIGHTPULSE_SITE_BASE}/api/alliances/{int(aid)}/refresh?force=1&kid={int(kid)}"
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=60),
                cookie_jar=aiohttp.CookieJar(unsafe=True)) as session:
            if not await _open_site_page(session, page, "UNOFFICIAL ALLIANCE PAGE"):
                return False
            async with session.post(url, headers=api_site_headers(page), data=b"") as response:
                if response.status != 200:
                    return False
                data = json.loads(await response.text())
            if not isinstance(data, dict):
                return False
            if data.get("queued") or data.get("accepted") or data.get("started"):
                for _ in range(20):
                    await asyncio.sleep(2)
                    status = await get_alliance_refresh_status(session, aid, page)
                    if isinstance(status, dict) and not status.get("queued") and not status.get("started"):
                        return True
                return False
            return isinstance(data.get("alliance"), dict)
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError):
        logger.warning("Unofficial website refresh failed for alliance %s", aid, exc_info=True)
        return False
