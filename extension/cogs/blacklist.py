"""Admin blacklist management and reset-time NAP alerts."""
import asyncio
import logging
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.permission_handler import PermissionManager
from .. import blacklist as store
from ..providers import mightpulse
from .nap_breaking import _settings, _protection, _alliance_role, _nap_logic

logger = logging.getLogger("extension")


def describe(record):
    player = record.get("player") or {}
    alliance = player.get("alliance") or {}
    end = record.get("ends_at")
    expiry = f"<t:{int(datetime.fromisoformat(end).timestamp())}:F>" if end else "Indefinite"
    name = discord.utils.escape_markdown(str(player.get("nick_name") or record["fid"]))
    reason = discord.utils.escape_mentions(record["reason"])
    return (f"**{name}** — FID `{record['fid']}`\n"
            f"KD: {player.get('kid', 'Unknown')} | Alliance: "
            f"{discord.utils.escape_mentions(str(alliance.get('abbr') or 'None'))}\n"
            f"Reason: {reason}\nEnds: {expiry}\n"
            f"Last successful refresh: {record.get('refreshed_at') or 'Never'}"
            + ("\n⚠️ Refresh failed; showing last known data." if record.get("refresh_failed") else ""))


class Blacklist(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.lock = asyncio.Lock()

    async def _admin(self, interaction):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return False
        return True

    @app_commands.command(name="blacklist", description="Admin: blacklist a player by FID or stored KD + exact name.")
    @app_commands.describe(ends_at="Optional ISO UTC end, e.g. 2027-01-01 00:00; omitted means indefinite")
    async def blacklist(self, interaction: discord.Interaction, reason: str,
                        fid: str | None = None, kd: int | None = None,
                        player_name: str | None = None, ends_at: str | None = None):
        if not await self._admin(interaction):
            return
        try:
            expiry = store.parse_end(ends_at)
            if not reason.strip() or len(reason) > 1000:
                raise ValueError("Provide a reason between 1 and 1000 characters.")
            if fid is not None:
                fid = fid.strip()
                if not fid.isascii() or not fid.isdigit() or int(fid) <= 0:
                    raise ValueError("FID must be a positive numeric ID.")
                fid = str(int(fid))
            else:
                if kd is None or not player_name or not player_name.strip():
                    raise ValueError("Provide a FID or both KD and player_name.")
                matches = store.find_fids(kd, player_name)
                if len(matches) != 1:
                    raise ValueError("No unique stored KD + name match. Please provide the player's FID.")
                fid = matches[0]
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return
        async with self.lock:
            record = {"fid": fid, "reason": reason.strip(), "ends_at": expiry,
                      "added_by": interaction.user.id, "added_at": datetime.now(timezone.utc).isoformat()}
            await self._refresh(record)
            store.save(record)
        await interaction.followup.send(describe(record), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())

    @app_commands.command(name="unblacklist", description="Admin: remove a player's blacklist entry by FID.")
    async def unblacklist(self, interaction: discord.Interaction, fid: str):
        if not await self._admin(interaction):
            return
        fid = fid.strip()
        if fid.isascii() and fid.isdigit():
            fid = str(int(fid))
        async with self.lock:
            removed = store.remove(fid)
        await interaction.followup.send("Player unblacklisted." if removed else "No blacklist entry found.", ephemeral=True)

    async def _refresh(self, record):
        player = None
        try:
            if mightpulse.available():
                player = await mightpulse.fetch_player_data(record["fid"], force_refresh=True, bypass_local_cache=True)
        except Exception:
            logger.exception("Blacklist refresh failed for FID %s", record["fid"])
        record["refresh_failed"] = not isinstance(player, dict)
        if isinstance(player, dict):
            record["player"] = player
            record["refreshed_at"] = datetime.now(timezone.utc).isoformat()

    async def refresh(self, *, notify=False):
        async with self.lock:
            result = []
            for record in store.records():
                if not store.active(record):
                    continue
                await self._refresh(record)
                store.save(record)
                result.append(record)
            if notify:
                for record in result:
                    if not store.active(record) or record.get("refresh_failed"):
                        continue
                    alliance = (record.get("player") or {}).get("alliance") or {}
                    aid, tag = alliance.get("aid"), alliance.get("abbr")
                    if _protection(self.bot, aid, tag)[0]:
                        try:
                            await self._notify(record, aid, tag)
                        except Exception:
                            logger.exception("Blacklist NAP notification failed for FID %s", record["fid"])
            return [record for record in result if store.active(record)]

    async def _notify(self, record, aid, tag):
        settings = _settings(self.bot)
        channel_id = settings.get_int("nap_channel_id")
        if not channel_id:
            logger.error("Cannot publish blacklist alert: nap_channel_id is not configured")
            return
        channel = self.bot.get_channel(channel_id) or await self.bot.fetch_channel(channel_id)
        if aid is None and tag:
            logic = _nap_logic(self.bot)
            aid = logic._resolve_aid_for_tag(tag, exact_only=True)
        role = _alliance_role(channel.guild, aid)
        council = settings.get_int("role_council_id")
        pings = ([f"<@&{council}>"] if council else []) + ([role.mention] if role else [])
        await channel.send(" ".join(pings) + "\n⚠️ Blacklisted player in a NAP-protected alliance:\n" + describe(record),
                           allowed_mentions=discord.AllowedMentions(users=False, everyone=False,
                               roles=[discord.Object(id=rid) for rid in {council, role.id if role else None} if rid]))

    @app_commands.command(name="blacklist_list", description="Refresh and list all active blacklisted players.")
    async def blacklist_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        records = await self.refresh()
        if not records:
            await interaction.followup.send("No active blacklisted players.", ephemeral=True)
        for record in records:
            await interaction.followup.send(describe(record), ephemeral=True, allowed_mentions=discord.AllowedMentions.none())