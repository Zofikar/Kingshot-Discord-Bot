"""NAP cog: exclusions, academies, protected list, ranking post."""
import asyncio
import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.permission_handler import PermissionManager

from .. import nap as nap_mod
from .. import storage
from ..providers import mightpulse
from ..settings import SettingsStore

logger = logging.getLogger("extension")


def _settings(bot):
    return getattr(bot, "extension_settings", None) or SettingsStore()


def _nap_context(bot):
    """(NapLogic, nap_state) loaded from alliance_list + verification.sqlite."""
    nap_state = storage.Storage().load()
    alliances = {}
    for row in storage.all_alliances():
        alliances[str(row["alliance_id"])] = {
            "abbr": row["abbr"], "name": row["name"],
            "power": row["power"], "role_id": row["role_id"],
        }
    logic = nap_mod.NapLogic(
        nap_alliances_count=_settings(bot).get_int("nap_alliances_count", 10),
        alliances=alliances,
        nap_tag_aliases=nap_state["nap_tag_aliases"],
        academies=nap_state["academies"],
        academy_tags=nap_state["academy_tags"],
        nap_exclusions=nap_state["nap_exclusions"],
    )
    return logic, nap_state


def _save_nap(logic, nap_state):
    nap_state["nap_tag_aliases"] = logic.nap_tag_aliases
    nap_state["academies"] = logic.academies
    nap_state["academy_tags"] = logic.academy_tags
    nap_state["nap_exclusions"] = logic.nap_exclusions
    storage.Storage().save(nap_state)


async def _resolve_aid_from_gid(gid):
    """Resolve an alliance aid from a member GID (or return an error string)."""
    player = await mightpulse.fetch_player_data(gid, force_refresh=True)
    if not isinstance(player, dict):
        return f"Could not fetch Governor ID `{gid}`."
    alliance = player.get("alliance") or {}
    aid = alliance.get("aid")
    if aid is None:
        return f"Governor `{gid}` is not in any alliance."
    storage.upsert_alliance(aid, abbr=alliance.get("abbr"), name=alliance.get("name"),
                            kid=player.get("kid"), power=player.get("power"))
    return int(aid)


async def _resolve_academy_tag(logic, main_aid, tag):
    """Resolve a current academy tag only within the main alliance's kingdom."""
    main = storage.alliance_row(main_aid)
    kid = main["kid"] if main else None
    if not kid:
        return "Your main alliance's kingdom is unknown; use the GID-based admin command."
    kid = int(kid)
    tag = tag.strip()
    if not tag:
        return "Academy tag cannot be blank. Omit it to remove the academy."
    candidates = [row for row in storage.all_alliances()
                  if row["kid"] is not None and int(row["kid"]) == kid
                  and str(row["abbr"] or "").strip().casefold() == tag.casefold()]
    exact = [row for row in candidates if str(row["abbr"]).strip() == tag]
    matches = exact or candidates
    if len(matches) == 1:
        row = dict(matches[0])
        aid = int(row["alliance_id"])
    else:
        fetched = await mightpulse.fetch_alliance_data(kid, tag, bypass_local_cache=True)
        if not isinstance(fetched, dict) or not fetched.get("aid"):
            return f"Could not resolve academy tag `{tag}` in kingdom `{kid}`."
        if fetched.get("kid") is not None and int(fetched["kid"]) != kid:
            return f"Academy tag `{tag}` did not resolve to kingdom `{kid}`."
        aid = int(fetched["aid"])
        row = {"abbr": fetched.get("abbr") or tag, "name": fetched.get("name"),
               "kid": kid, "power": fetched.get("power")}
        storage.upsert_alliance(aid, **row)
    # Keep this command's snapshot current, including tags used by NAP output.
    logic.alliances[str(aid)] = row
    return aid


async def post_nap_ranking(bot, *, triggered_by="scheduler"):
    """Post the NAP ranking, lookback/exclusion notices and leaders tag list. Returns bool."""
    # Wait for any in-progress nightly maintenance so the posted snapshot always
    # reflects the completed persisted ranking state (mirrors the monolith's lock).
    async with bot.maintenance_lock:
        settings = _settings(bot)
        channel_id = settings.get_int("nap_channel_id")
        if not channel_id:
            logger.error("Cannot post NAP ranking: ext.nap_channel_id is not configured.")
            return False
        logic, _ = _nap_context(bot)
        ranking = await logic.get_nap_ranking()
        if not ranking:
            logger.error(
                "Cannot post NAP ranking: stored alliance snapshot does not contain "
                "enough valid, non-excluded alliances with positive power."
            )
            return False

        academy_tags = {}
        for main_key, academy_val in logic.academies.items():
            ac_tag = logic.get_tag_for_aid(academy_val)
            if ac_tag:
                academy_tags[str(main_key)] = ac_tag

        message = nap_mod.NapLogic.build_nap_message(ranking, academy_tags)

        days = settings.get_int("nap_snapshot_lookback_protected", 0)
        lookback = storage.nap_lookback_protected(days)
        fallen = nap_mod.get_nap_protected_alliances(logic, lookback, days)["fallen_protected"]
        if days > 0 and fallen:
            parts = [f"[{e['abbr']}] (last ranked {e['last_seen_utc'].strftime('%Y-%m-%d')} UTC)" for e in fallen[:15]]
            footer = f"🛡️ Still NAP-protected via {days}-day lookback:\n" + "\n".join(parts)
            if len(fallen) > 15:
                footer += f" … and {len(fallen) - 15} more"
            message = message + "\n\n" + footer

        exclusions = logic.get_nap_exclusion_lines()
        if exclusions:
            message += "\n\n" + "\n".join(exclusions)

        try:
            channel = bot.get_channel(channel_id)
            if channel is None:
                channel = await bot.fetch_channel(channel_id)
            await channel.send(message)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            logger.warning("Could not post NAP ranking to channel %s: %s", channel_id, e)
            return False
        storage.record_nap_ranking(ranking)

        await _post_nap_tag_list(bot, logic, ranking, academy_tags, fallen, triggered_by=triggered_by)
        logger.info("Posted NAP ranking (trigger=%s)", triggered_by)
        return True


async def _post_nap_tag_list(bot, logic, ranking, academy_tags, fallen, *, triggered_by="scheduler"):
    leaders_channel_id = _settings(bot).get_int("leaders_nap_post_channel")
    if not leaders_channel_id:
        return False
    tags = logic.build_nap_tag_list(ranking, academy_tags, fallen)
    if not tags:
        return False
    message = nap_mod.NapLogic.build_nap_tag_message(tags)
    try:
        channel = bot.get_channel(leaders_channel_id)
        if channel is None:
            channel = await bot.fetch_channel(leaders_channel_id)
        await channel.send(message)
    except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
        logger.warning("Could not post NAP tag list to channel %s: %s", leaders_channel_id, e)
        return False
    logger.info("Posted NAP tag list (trigger=%s)", triggered_by)
    return True


class Nap(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    async def _exclusion_choices(self, interaction, current, *, excluded):
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            return []

        def read():
            logic, _ = _nap_context(self.bot)
            aids = logic.nap_exclusions if excluded else logic.alliances
            query = current.strip().casefold()
            choices = []
            for aid in aids:
                if not excluded and str(aid) in logic.nap_exclusions:
                    continue
                info = logic.alliances.get(str(aid)) or {}
                record = logic.nap_exclusions.get(str(aid)) or {}
                tag = info.get("abbr") or record.get("tag") or "Unknown alliance"
                name = info.get("name") or ""
                label = f"[{tag}] {name}".strip()
                if query not in f"{label} {aid}".casefold():
                    continue
                suffix = f" (ID: {aid})"
                choices.append(app_commands.Choice(name=label[:100 - len(suffix)] + suffix, value=int(aid)))
                if len(choices) == 25:
                    break
            return choices

        return await asyncio.to_thread(read)

    async def exclude_aid_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._exclusion_choices(interaction, current, excluded=False)

    async def unexclude_aid_autocomplete(self, interaction: discord.Interaction, current: str):
        return await self._exclusion_choices(interaction, current, excluded=True)

    @app_commands.command(name="nap_exclude", description="Admin: exclude an alliance from NAP.")
    @app_commands.autocomplete(aid=exclude_aid_autocomplete)
    @app_commands.describe(aid="Alliance id", reason="Reason for exclusion")
    async def nap_exclude(self, interaction, aid: int, reason: str = "no reason given"):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        logic, nap_state = _nap_context(self.bot)
        if logic.set_nap_exclusion(aid, reason=reason, added_by=str(interaction.user.id)):
            _save_nap(logic, nap_state)
            await interaction.followup.send(f"Excluded alliance `{aid}` from NAP.", ephemeral=True)
        else:
            await interaction.followup.send("That alliance is already excluded.", ephemeral=True)

    @app_commands.command(name="nap_unexclude", description="Admin: remove a NAP exclusion.")
    @app_commands.autocomplete(aid=unexclude_aid_autocomplete)
    @app_commands.describe(aid="Alliance id")
    async def nap_unexclude(self, interaction, aid: int):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        logic, nap_state = _nap_context(self.bot)
        if logic.remove_nap_exclusion(aid):
            _save_nap(logic, nap_state)
            await interaction.followup.send(f"Removed NAP exclusion for `{aid}`.", ephemeral=True)
        else:
            await interaction.followup.send("That alliance wasn't excluded.", ephemeral=True)

    @app_commands.command(name="nap_protected", description="List NAP-protected alliances.")
    async def nap_protected(self, interaction):
        await interaction.response.defer(ephemeral=True)
        logic, _ = _nap_context(self.bot)
        settings = _settings(self.bot)
        days = settings.get_int("nap_snapshot_lookback_protected", 0)
        lookback = storage.nap_lookback_protected(days)
        state = nap_mod.get_nap_protected_alliances(logic, lookback, days)
        if not state["current"]:
            await interaction.followup.send("No NAP-protected alliances.", ephemeral=True)
            return
        lines = logic.build_nap_protected_lines(state["current"])
        if days > 0:
            if state["fallen_protected"]:
                lines.append("")
                lines.append(f"🛡️ Still protected via {days}-day lookback (UTC):")
                for entry in state["fallen_protected"]:
                    lines.append(f"• [{entry['abbr']}] {entry['name']} — last ranked {entry['last_seen_utc'].strftime('%Y-%m-%d %H:%M')} UTC")
            else:
                lines.append("")
                lines.append(f"🛡️ No fallen alliances within the {days}-day lookback window.")
        exclusions = logic.get_nap_exclusion_lines()
        if exclusions:
            lines.append("")
            lines.extend(exclusions)
        await interaction.followup.send("\n".join(lines), ephemeral=True)

    @app_commands.command(name="admin_post_nap", description="Admin: post the current NAP ranking.")
    async def admin_post_nap(self, interaction):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        ok = await post_nap_ranking(self.bot, triggered_by=f"admin:{interaction.user.id}")
        if ok:
            await interaction.followup.send("Posted the NAP ranking.", ephemeral=True)
        else:
            await interaction.followup.send("Could not build/post the NAP ranking.", ephemeral=True)

    @app_commands.command(name="academy_set", description="Register/replace/remove your alliance's academy by tag.")
    @app_commands.describe(academy_tag="Academy alliance tag (omit to remove)")
    async def academy_set(self, interaction, academy_tag: str | None = None):
        await interaction.response.defer(ephemeral=True)
        main_fid = storage.main_fid_for_discord(interaction.user.id)
        if main_fid is None:
            await interaction.followup.send("You have no linked account.", ephemeral=True)
            return
        row = storage.user_row(main_fid)
        main_aid = row["alliance"] if row else None
        if main_aid is None:
            await interaction.followup.send("Your account has no alliance.", ephemeral=True)
            return
        logic, nap_state = _nap_context(self.bot)
        ac_aid = await _resolve_academy_tag(logic, main_aid, academy_tag) if academy_tag is not None else None
        if isinstance(ac_aid, str):
            await interaction.followup.send(ac_aid, ephemeral=True)
            return
        if logic.set_alliance_academy(main_aid, ac_aid):
            _save_nap(logic, nap_state)
            await interaction.followup.send("Academy updated.", ephemeral=True)
        else:
            await interaction.followup.send("No change.", ephemeral=True)

    @app_commands.command(name="admin_academy_set", description="Admin: set any alliance's academy by tag.")
    @app_commands.describe(main_tag="Tag of the main alliance", academy_tag="Tag of the academy (omit to remove)")
    async def admin_academy_set(self, interaction, main_tag: str, academy_tag: str | None = None):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        logic, nap_state = _nap_context(self.bot)
        main_aid = logic._resolve_aid_for_tag(main_tag)
        if main_aid is None:
            await interaction.followup.send(f"Unknown alliance tag `{main_tag}`.", ephemeral=True)
            return
        academy_aid = None
        if academy_tag is not None:
            academy_aid = await _resolve_academy_tag(logic, main_aid, academy_tag)
            if isinstance(academy_aid, str):
                await interaction.followup.send(academy_aid, ephemeral=True)
                return
        if logic.set_alliance_academy(main_aid, academy_aid):
            _save_nap(logic, nap_state)
            await interaction.followup.send("Academy updated.", ephemeral=True)
        else:
            await interaction.followup.send("No change.", ephemeral=True)

    @app_commands.command(name="admin_academy_set_by_gid", description="Admin: set an academy via member GIDs.")
    @app_commands.describe(main_gid="GID of a player in the main alliance",
                           academy_gid="GID of a player in the academy (omit to remove)")
    async def admin_academy_set_by_gid(self, interaction, main_gid: str, academy_gid: str | None = None):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        main_aid = await _resolve_aid_from_gid(main_gid.strip())
        if isinstance(main_aid, str):
            await interaction.followup.send(main_aid, ephemeral=True)
            return
        academy_aid = None
        if academy_gid and academy_gid.strip():
            academy_aid = await _resolve_aid_from_gid(academy_gid.strip())
            if isinstance(academy_aid, str):
                await interaction.followup.send(academy_aid, ephemeral=True)
                return
        logic, nap_state = _nap_context(self.bot)
        if logic.set_alliance_academy(main_aid, academy_aid):
            _save_nap(logic, nap_state)
            await interaction.followup.send("Academy updated.", ephemeral=True)
        else:
            await interaction.followup.send("No change.", ephemeral=True)
