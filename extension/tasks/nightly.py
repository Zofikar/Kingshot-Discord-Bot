"""Scheduled tasks: nightly maintenance + daily NAP post (MightPulse-gated)."""
import logging
import asyncio
import math
import time as time_module
from datetime import datetime, time, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks
from cogs.permission_handler import PermissionManager

from .. import storage
from ..providers import mightpulse
from ..settings import SettingsStore

logger = logging.getLogger("extension")


def _settings(bot):
    return getattr(bot, "extension_settings", None) or SettingsStore()


def _leading_tag(nick):
    """``(tag, trailer)`` of a bot-managed ``[TAG] Name`` nickname.

    ``trailer`` is everything after the closing bracket, untouched, so a rewrite
    keeps the member's own separator (``[TAG] Name`` / ``[TAG]Name``). Returns
    ``(None, None)`` for nicknames that carry no leading tag at all — those are
    never touched, so a rename cannot turn into a mass re-nickname.
    """
    if not nick or not nick.startswith("["):
        return None, None
    close = nick.find("]")
    if close <= 1:
        return None, None
    return nick[1:close], nick[close + 1:]


class NightlyTasks(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        # Last `_sync_alliance_discord_state` counts, so /admin_nightly_refresh can
        # report what actually reached Discord.
        self.last_tag_sync = {"linked": 0, "retagged": 0}
        self.last_player_sync = {"synced": 0, "skipped": 0}
        self.last_alliance_refresh_failures = None
        self.last_alliance_snapshot_date = None
        self.alliance_snapshot_times = {}

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
            posted = await self._post_nap_ranking(triggered_by="scheduler")
            if not posted:
                logger.error(
                    "Scheduled NAP post did not publish. Check nap_channel_id, "
                    "the stored alliance count/power snapshot, and channel permissions."
                )
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
        self._discovered_power = {}
        candidates = await mightpulse.fetch_kingdom_alliance_power_ranking(kid, limit=candidate_count)
        if not candidates:
            return 0
        previous_power = {str(row["alliance_id"]): row["power"] for row in storage.all_alliances()}
        for alliance in candidates[:candidate_count]:
            self._discovered_power[str(alliance["aid"])] = alliance["power"]
            storage.upsert_alliance(
                alliance.get("aid"), abbr=alliance.get("abbr"), name=alliance.get("name"),
                # Boards are discovery snapshots, not current alliance power.
                # Keep an existing value if the subsequent refresh fails.
                kid=kid, power=previous_power.get(str(alliance["aid"]), alliance.get("power")),
            )
        return min(len(candidates), candidate_count)

    async def _run_nightly_maintenance(self, *, triggered_by="scheduler"):
        # Only alliance snapshot work is protected by maintenance_lock. The NAP
        # post depends on that snapshot, but it must not wait behind the much
        # longer player-profile refresh that follows.
        async with self.bot.maintenance_lock:
            # Historical tag -> aid aliases so a renamed alliance stays resolvable
            # (lookback protection, /admin_academy_set, exclusions) exactly like
            # the monolith's nightly loop. The tags are snapshotted *before*
            # discovery, because discovery already rewrites `abbr` from the live
            # kingdom board and would otherwise hide the rename. NAP state is
            # loaded lazily: no DB read at all when nothing was renamed.
            previous_tags = {row["alliance_id"]: row["abbr"] for row in storage.all_alliances()}
            self._discovered_power = {}
            self.last_alliance_refresh_failures = None
            self.last_alliance_snapshot_date = None
            self.alliance_snapshot_times = {}
            refresh_failures = []
            rename_state = None
            discovered = await self._discover_candidates()
            for row in storage.all_alliances():
                aid = row["alliance_id"]
                tag = row["abbr"]
                kid = row["kid"] or await self._primary_kid()
                # No stored tag is fine now: the refresh is keyed on the alliance
                # id, which is what survives a rename.
                if not kid:
                    refresh_failures.append(str(aid))
                    continue
                alliance, source = await mightpulse.fetch_current_alliance(
                    int(aid), kid=int(kid), tag=tag)
                if not isinstance(alliance, dict):
                    refresh_failures.append(str(aid))
                    logger.warning(
                        "Alliance update failed for aid=%s tag=%r source=%s; keeping previous snapshot value.",
                        aid, tag, source)
                    continue
                new_tag = alliance.get("abbr")
                self.alliance_snapshot_times[str(aid)] = alliance.get("snapshot_cached_at")
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
                    kid=alliance.get("kid") or kid,
                    # The board may be hours old; the refreshed info is newer.
                    power=alliance.get("power"),
                )
            # Successful reads are recent; failed reads retain their old values.
            # Make Discord agree with the stored rows (role names
            # and every member's `[TAG]` nickname prefix).
            resynced = await self._sync_alliance_discord_state()
            self.last_alliance_refresh_failures = refresh_failures
            self.last_alliance_snapshot_date = datetime.now(timezone.utc).date()
            logger.info(
                "Nightly alliance snapshot done (trigger=%s, discovered=%s, members_resynced=%s)",
                triggered_by, discovered, resynced)

        # Player names/roles do not affect the NAP ranking. Run them after the
        # alliance lock is released so the 00:05 UTC post can proceed as soon as
        # its authoritative alliance snapshot is complete.
        blacklist = self.bot.get_cog("Blacklist") if hasattr(self.bot, "get_cog") else None
        if blacklist is not None:
            await blacklist.refresh(notify=triggered_by == "scheduler")
        player_sync = await self._sync_linked_players()
        logger.info(
            "Nightly player sync done (trigger=%s, players_synced=%s, players_skipped=%s)",
            triggered_by, player_sync["synced"], player_sync["skipped"])

    async def _sync_linked_players(self) -> dict:
        """Refresh every linked main FID and reconcile its Discord member.

        Nightly alliance maintenance alone cannot see an in-game player rename.
        This mirrors the pre-migration monolith: bypass the process-local cache,
        persist trustworthy current player data, then apply nickname and roles.
        """
        from ..cogs.identity import sync_member
        from ..roles import player_to_user_fields

        stats = {"synced": 0, "skipped": 0}
        guilds = list(getattr(self.bot, "guilds", None) or [])

        for fid, discord_id, server_id in storage.linked_main_accounts():
            player = await mightpulse.fetch_player_data(
                fid, force_refresh=False, bypass_local_cache=True)
            if not isinstance(player, dict):
                stats["skipped"] += 1
                logger.warning("Player update failed for fid=%s; keeping previous profile.", fid)
                continue

            fields = player_to_user_fields(fid, player)
            storage.register_player(
                fid=fields["fid"], discord_id=discord_id,
                discord_server_id=server_id, nickname=fields["nickname"],
                kid=fields["kid"], alliance_aid=fields["alliance_aid"],
                rank=fields["rank"], power=fields["power"],
                abbr=fields["abbr"], alliance_name=fields["alliance_name"],
            )

            candidate_guilds = [g for g in guilds if server_id is None or g.id == server_id]
            member = None
            for guild in candidate_guilds:
                member = guild.get_member(int(discord_id))
                if member is None:
                    try:
                        member = await guild.fetch_member(int(discord_id))
                    except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                        member = None
                if member is not None:
                    break

            if member is None:
                stats["skipped"] += 1
                continue

            await sync_member(self.bot, member)
            stats["synced"] += 1

        self.last_player_sync = stats
        return stats

    async def _sync_alliance_discord_state(self) -> int:
        """Re-tag alliance roles + member nicknames from the stored alliance rows.

        The refresh above only rewrites ``alliance_list``. Discord state (the
        ``[TAG] Name`` role and the ``[TAG]`` nickname prefix) was previously
        touched only when a member ran /register or /refresh, so a renamed
        alliance (MNX -> TKO) kept its retired tag on its role and on every
        member who did not happen to re-sync. The monolith avoids this by
        re-applying every player right after its alliance snapshot changes; here
        we reconcile the alliances whose Discord state drifted.

        Two passes per guild:

        1. linked members — for every tracked alliance, rename the role and hand
           the members whose nickname carries a stale tag back to ``sync_member``
           (authoritative: name + roles come from their MightPulse snapshot);
        2. everyone else — a member who never linked a FID still advertises the
           retired tag in their nickname, and no amount of storage lookups will
           find them. If that tag is one we know (a tracked alliance's current
           tag, or a historical alias recorded when the rename was detected) the
           nickname's tag is rewritten in place, keeping the rest of the name.

        Idempotent: a role whose name already matches and members whose nickname
        already carries the current tag are left untouched.

        Returns the number of members re-applied.
        """
        from ..cogs.identity import _ensure_alliance_role, sync_member

        rows = [row for row in storage.all_alliances() if row["role_id"] and row["abbr"]]
        if not rows:
            return 0
        # Exact-case keys: MNX (main) and MNx (academy) are different alliances.
        abbr_by_aid = {str(row["alliance_id"]): row["abbr"] for row in rows}
        current_tags = {str(row["abbr"]): row["abbr"] for row in rows}
        aliases = None            # lazily read NAP state: no DB read when unused

        resynced = 0
        retagged = 0
        for guild in list(getattr(self.bot, "guilds", None) or []):
            touched = set()
            for row in rows:
                abbr = row["abbr"]
                aid = row["alliance_id"]
                expected = f"[{abbr}] {row['name'] or abbr}"[:100]
                role = guild.get_role(int(row["role_id"]))
                stale = self._stale_tagged(await self._linked_members(guild, aid), abbr)
                if role is not None and role.name == expected and not stale:
                    continue
                await _ensure_alliance_role(self.bot, guild, aid)
                for member in stale:
                    await sync_member(self.bot, member)
                    touched.add(member.id)
                    resynced += 1
            for member in list(getattr(guild, "members", None) or []):
                if member.id in touched or getattr(member, "bot", False):
                    continue
                tag, trailer = _leading_tag(getattr(member, "nick", None))
                if tag is None or tag in current_tags:
                    continue
                if aliases is None:
                    aliases = self._alias_targets(abbr_by_aid)
                target = aliases.get(tag)
                if target is None or target == tag:
                    continue
                try:
                    await member.edit(nick=f"[{target}]{trailer}"[:32],
                                      reason="Alliance rename sync")
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning("Could not re-tag member %s: %s", member.id, e)
                    continue
                retagged += 1
        if resynced or retagged:
            logger.info(
                "Alliance tag sync: %s linked member(s) re-applied, %s stale tag(s) rewritten.",
                resynced, retagged)
        self.last_tag_sync = {"linked": resynced, "retagged": retagged}
        return resynced

    def _alias_targets(self, abbr_by_aid) -> dict:
        """{retired tag: that alliance's current abbr} from the recorded aliases."""
        from ..cogs.nap import _nap_context
        try:
            logic, _state = _nap_context(self.bot)
        except Exception as e:
            logger.warning("Could not read tag aliases for the Discord re-tag: %s", e)
            return {}
        targets = {}
        for tag, aid in (logic.nap_tag_aliases or {}).items():
            abbr = abbr_by_aid.get(str(aid))
            if abbr:
                targets.setdefault(str(tag), abbr)
        return targets

    @staticmethod
    def _stale_tagged(members, abbr) -> list:
        """Members whose nickname carries an alliance tag that is no longer theirs.

        Only *tagged* nicknames are considered (``[XYZ] Name``); a member who has
        no nickname, or one without a tag, is deliberately left alone so a rename
        cannot turn into a mass re-nickname of people who never opted in.
        """
        stale = []
        for member in members:
            tag, _trailer = _leading_tag(getattr(member, "nick", None))
            if tag is not None and tag != abbr:
                stale.append(member)
        return stale

    async def _linked_members(self, guild, aid) -> list:
        """Members owning any FID in this alliance (cache first, then a fetch)."""
        members = []
        for discord_id in storage.discord_ids_for_alliances([aid]):
            try:
                member = guild.get_member(int(discord_id))
            except (TypeError, ValueError):
                continue
            if member is None:
                # Not in the member cache (big guild, no chunk yet): ask directly
                # instead of skipping, otherwise those tags silently stay stale.
                try:
                    member = await guild.fetch_member(int(discord_id))
                except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
                    logger.debug("Could not fetch linked member %s: %s", discord_id, e)
                    continue
            if member is not None:
                members.append(member)
        return members

    def snapshot_ready_for_publication(self):
        """Fail closed for missing, future, nonfinite or expired capture times."""
        if (self.last_alliance_refresh_failures != []
                or self.last_alliance_snapshot_date != datetime.now(timezone.utc).date()):
            return False
        rows = storage.all_alliances()
        if not rows:
            return False
        now = time_module.time()
        for row in rows:
            try:
                age = now - float(self.alliance_snapshot_times[str(row["alliance_id"])])
                if not math.isfinite(age) or not -5 <= age <= mightpulse.NIGHTLY_PUBLICATION_MAX_AGE_SECONDS:
                    return False
            except (KeyError, TypeError, ValueError, OverflowError):
                return False
        return True

    async def _recover_publication_snapshot(self):
        """Retry unresolved/expired candidates, with a bounded recovery budget."""
        async with self.bot.maintenance_lock:
            failures = []
            deadline = time_module.monotonic() + 120
            for row in storage.all_alliances():
                aid = str(row["alliance_id"])
                try:
                    age = time_module.time() - float(self.alliance_snapshot_times.get(aid))
                    # Leave a minute of headroom for subsequent candidates.
                    if math.isfinite(age) and -5 <= age <= 540:
                        continue
                except (TypeError, ValueError, OverflowError):
                    pass
                remaining = deadline - time_module.monotonic()
                if remaining <= 0:
                    failures.append(aid)
                    continue
                kid = row["kid"] or await self._primary_kid()
                if not kid:
                    failures.append(aid)
                    continue
                try:
                    alliance, source = await asyncio.wait_for(
                        mightpulse.fetch_current_alliance(int(aid), kid=int(kid), tag=row["abbr"]),
                        timeout=remaining)
                except asyncio.TimeoutError:
                    alliance = None
                if not isinstance(alliance, dict):
                    failures.append(aid)
                    continue
                storage.upsert_alliance(
                    aid, abbr=alliance.get("abbr") or row["abbr"],
                    name=alliance.get("name") or row["name"], kid=alliance.get("kid") or kid,
                    power=alliance.get("power"))
                self.alliance_snapshot_times[aid] = alliance.get("snapshot_cached_at")
            self.last_alliance_refresh_failures = failures

    async def _post_nap_ranking(self, *, triggered_by="scheduler"):
        if triggered_by == "scheduler" and hasattr(self.bot, "maintenance_lock"):
            # Wait for the alliance phase before examining its completion state.
            async with self.bot.maintenance_lock:
                pass
        if (triggered_by == "scheduler"
                and self.last_alliance_snapshot_date == datetime.now(timezone.utc).date()):
            await self._recover_publication_snapshot()
        if triggered_by == "scheduler" and (
                self.last_alliance_refresh_failures != []
                or self.last_alliance_snapshot_date != datetime.now(timezone.utc).date()):
            logger.error(
                "Refusing scheduled NAP post: reset snapshot is unverified or incomplete; failed alliances=%s",
                self.last_alliance_refresh_failures)
            return False
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
        sync = self.last_tag_sync
        players = self.last_player_sync
        await interaction.followup.send(
            "Nightly maintenance finished.\n"
            f"Discord tag sync: {sync['linked']} linked member(s) re-applied, "
            f"{sync['retagged']} stale tag(s) rewritten "
            f"(alliances tracked: {len(storage.all_alliances())}).\n"
            f"Player profiles: {players['synced']} synced, {players['skipped']} skipped.",
            ephemeral=True)
