"""Scheduled tasks: nightly maintenance + daily NAP post (MightPulse-gated)."""
import logging
from datetime import time, timezone

from discord import app_commands
from discord.ext import commands, tasks
from cogs.permission_handler import PermissionManager

from .. import storage
from ..providers import mightpulse
from ..settings import SettingsStore

logger = logging.getLogger("extension")


def _settings(bot):
    return getattr(bot, "extension_settings", None) or SettingsStore()


class NightlyTasks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        if mightpulse.available():
            self.daily_alliance_maintenance.start()
            self.daily_nap_post.start()

    def cog_unload(self):
        self.daily_alliance_maintenance.cancel()
        self.daily_nap_post.cancel()

    # Runs before the 00:05 NAP post (same order as the monolith) so the daily
    # post always reflects the freshly rebuilt ranking snapshot.
    @tasks.loop(time=time(hour=0, minute=0, second=30, tzinfo=timezone.utc))
    async def daily_alliance_maintenance(self):
        await self.bot.wait_until_ready()
        try:
            await self._run_nightly_maintenance(triggered_by="scheduler")
        except Exception as e:
            logger.exception("Nightly maintenance failed: %s", e)

    @tasks.loop(time=time(hour=0, minute=5, second=0, tzinfo=timezone.utc))
    async def daily_nap_post(self):
        await self.bot.wait_until_ready()
        try:
            await self._post_nap_ranking(triggered_by="scheduler")
        except Exception as e:
            logger.exception("Daily NAP post failed: %s", e)

    async def _primary_kid(self):
        kids = _settings(self.bot).get_json("whitelist_kids", [])
        return int(kids[0]) if kids else None

    async def _discover_candidates(self):
        kid = await self._primary_kid()
        if not kid:
            return 0
        count = _settings(self.bot).get_int("nap_alliances_count", 10)
        try:
            mult = float(_settings(self.bot).get("nap_candidate_multiplier") or "2")
        except ValueError:
            mult = 2.0
        candidate_count = min(max(count, int(round(count * max(1.0, mult)))), 100)
        candidates = await mightpulse.fetch_kingdom_alliance_power_ranking(kid, limit=candidate_count)
        if not candidates:
            return 0
        for alliance in candidates[:candidate_count]:
            storage.upsert_alliance(
                alliance.get("aid"), abbr=alliance.get("abbr"), name=alliance.get("name"),
                kid=kid, power=alliance.get("power"),
            )
        return min(len(candidates), candidate_count)

    async def _run_nightly_maintenance(self, *, triggered_by="scheduler"):
        async with self.bot.maintenance_lock:
            discovered = await self._discover_candidates()
            for row in storage.all_alliances():
                aid = row["alliance_id"]
                tag = row["abbr"]
                kid = row["kid"] or await self._primary_kid()
                if not tag or not kid:
                    continue
                alliance, _source = await mightpulse.fetch_current_alliance(int(aid), kid=int(kid), tag=tag)
                if isinstance(alliance, dict):
                    storage.upsert_alliance(
                        aid, abbr=alliance.get("abbr") or tag, name=alliance.get("name") or tag,
                        kid=alliance.get("kid") or kid, power=alliance.get("power"),
                    )
            logger.info("Nightly maintenance done (trigger=%s, discovered=%s)", triggered_by, discovered)

    async def _post_nap_ranking(self, *, triggered_by="scheduler"):
        from ..cogs.nap import post_nap_ranking
        return await post_nap_ranking(self.bot, triggered_by=triggered_by)

    @app_commands.command(name="admin_nightly_refresh", description="Admin: run the nightly maintenance now.")
    async def admin_nightly_refresh(self, interaction):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        if self.bot.maintenance_lock.locked():
            await interaction.followup.send("⚠️ Nightly maintenance is already running.", ephemeral=True)
            return
        await interaction.followup.send("Starting nightly maintenance...", ephemeral=True)
        try:
            await self._run_nightly_maintenance(triggered_by=f"admin:{interaction.user.id}")
        except Exception as e:
            await interaction.followup.send(f"Nightly maintenance failed: {e}", ephemeral=True)
            return
        await interaction.followup.send("Nightly maintenance finished.", ephemeral=True)
