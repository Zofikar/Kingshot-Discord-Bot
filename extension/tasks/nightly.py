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
            # Historical tag -> aid aliases so a renamed alliance stays resolvable
            # (lookback protection, /admin_academy_set, exclusions) exactly like
            # the monolith's nightly loop. The tags are snapshotted *before*
            # discovery, because discovery already rewrites `abbr` from the live
            # kingdom board and would otherwise hide the rename. NAP state is
            # loaded lazily: no DB read at all when nothing was renamed.
            previous_tags = {row["alliance_id"]: row["abbr"] for row in storage.all_alliances()}
            rename_state = None
            discovered = await self._discover_candidates()
            for row in storage.all_alliances():
                aid = row["alliance_id"]
                tag = row["abbr"]
                kid = row["kid"] or await self._primary_kid()
                # No stored tag is fine now: the refresh is keyed on the alliance
                # id, which is what survives a rename.
                if not kid:
                    continue
                alliance, source = await mightpulse.fetch_current_alliance(
                    int(aid), kid=int(kid), tag=tag)
                if not isinstance(alliance, dict):
                    logger.warning(
                        "Alliance update failed for aid=%s tag=%r source=%s; keeping previous snapshot value.",
                        aid, tag, source)
                    continue
                new_tag = alliance.get("abbr")
                old_tag = previous_tags.get(aid) or tag
                if new_tag and old_tag and str(old_tag).strip().upper() != str(new_tag).strip().upper():
                    if rename_state is None:
                        from ..cogs.nap import _nap_context
                        rename_state = _nap_context(self.bot)
                    logic, nap_state = rename_state
                    if logic.record_tag_alias(old_tag, aid):
                        nap_state["nap_tag_aliases"] = logic.nap_tag_aliases
                        storage.Storage().save(nap_state)
                    logger.info(
                        "Alliance aid=%s renamed [%s] -> [%s]; alias recorded.",
                        aid, old_tag, new_tag)
                storage.upsert_alliance(
                    aid, abbr=new_tag or tag, name=alliance.get("name") or tag,
                    kid=alliance.get("kid") or kid, power=alliance.get("power"),
                )
            # The rows above are fresh; make Discord agree with them (role names
            # and every member's `[TAG]` nickname prefix).
            resynced = await self._sync_alliance_discord_state()
            logger.info(
                "Nightly maintenance done (trigger=%s, discovered=%s, members_resynced=%s)",
                triggered_by, discovered, resynced)

    async def _sync_alliance_discord_state(self) -> int:
        """Re-tag alliance roles + member nicknames from the stored alliance rows.

        The refresh above only rewrites ``alliance_list``. Discord state (the
        ``[TAG] Name`` role and the ``[TAG]`` nickname prefix) was previously
        touched only when a member ran /register or /refresh, so a renamed
        alliance (MNX -> TKO) kept its retired tag on its role and on every
        member who did not happen to re-sync. The monolith avoids this by
        re-applying every player right after its alliance snapshot changes; here
        we reconcile the alliances whose Discord state drifted.

        Idempotent: a role whose name already matches and members whose nickname
        already carries the current tag are left untouched.

        Returns the number of members re-applied.
        """
        from ..cogs.identity import _ensure_alliance_role, sync_member

        resynced = 0
        for guild in list(getattr(self.bot, "guilds", None) or []):
            for row in storage.all_alliances():
                aid = row["alliance_id"]
                abbr = row["abbr"]
                # Metadata-only rows (no role yet, e.g. NAP discovery) have no
                # Discord state to fix; a role is created on first registration.
                if not row["role_id"] or not abbr:
                    continue
                expected = f"[{abbr}] {row['name'] or abbr}"[:100]
                role = guild.get_role(int(row["role_id"]))
                stale = self._stale_tagged(self._linked_members(guild, aid), abbr)
                if role is not None and role.name == expected and not stale:
                    continue
                await _ensure_alliance_role(self.bot, guild, aid)
                for member in stale:
                    await sync_member(self.bot, member)
                    resynced += 1
        return resynced

    @staticmethod
    def _stale_tagged(members, abbr) -> list:
        """Members whose nickname carries an alliance tag that is no longer theirs.

        Only *tagged* nicknames are considered (``[XYZ] Name``); a member who has
        no nickname, or one without a tag, is deliberately left alone so a rename
        cannot turn into a mass re-nickname of people who never opted in.
        """
        prefix = f"[{abbr}]"
        return [m for m in members
                if (m.nick or "").startswith("[") and not m.nick.startswith(prefix)]

    @staticmethod
    def _linked_members(guild, aid) -> list:
        """Online members owning any FID in this alliance (main account drives)."""
        members = []
        for discord_id in storage.discord_ids_for_alliances([aid]):
            member = guild.get_member(discord_id)
            if member is not None:
                members.append(member)
        return members

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
