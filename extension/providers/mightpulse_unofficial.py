"""Undocumented routes already used by the bot; not a website/browser client.

Alliance-ID lookup is rename-safe and was verified live, but is not listed
in the official API documentation reviewed on 2026-11-25.
"""
from .mightpulse_api import MightPulseAPI


def alliance_id_path(aid):
    return f"alliances/{int(aid)}"


class MightPulseUnofficialAPI(MightPulseAPI):
    async def alliance_by_id(self, aid, *, include=None, wait=None, max_age=None, session=None):
        return await self.request(alliance_id_path(aid), include=include, wait=wait, max_age=max_age, session=session)
