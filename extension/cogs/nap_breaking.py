"""NAP breaking report: interactive 24-hour compliance flow."""
import asyncio
import logging
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands

from cogs.permission_handler import PermissionManager

from .. import nap as nap_mod, roles, storage
from ..providers import mightpulse
from ..settings import SettingsStore

logger = logging.getLogger("extension")


def _settings(bot):
    return getattr(bot, "extension_settings", None) or SettingsStore()


def _nap_state():
    return storage.Storage().load()


def _get_breaking(breaking_id):
    return _nap_state()["nap_breakings"].get(breaking_id)


def _save_breaking(breaking_id, record):
    state = _nap_state()
    state["nap_breakings"][breaking_id] = record
    storage.Storage().save(state)


def _alliances_dict():
    return {
        str(row["alliance_id"]): {"abbr": row["abbr"], "name": row["name"],
                                  "power": row["power"], "role_id": row["role_id"]}
        for row in storage.all_alliances()
    }


def _nap_logic(bot):
    state = _nap_state()
    return nap_mod.NapLogic(
        nap_alliances_count=_settings(bot).get_int("nap_alliances_count", 10),
        alliances=_alliances_dict(),
        nap_tag_aliases=state["nap_tag_aliases"],
        academies=state["academies"],
        nap_exclusions=state["nap_exclusions"],
    )


def _protection(bot, aid=None, tag=None):
    """(protected, status) from snapshot + exclusions + academy + lookback."""
    logic = _nap_logic(bot)
    settings = _settings(bot)
    days = settings.get_int("nap_snapshot_lookback_protected", 0)
    lookback = storage.nap_lookback_protected(days)
    return nap_mod.nap_protection_for_alliance(logic, lookback, days, aid=aid, tag=tag)


def _member_for_fid(guild, fid):
    discord_id = storage.discord_for_fid(fid)
    if not discord_id:
        return None
    return guild.get_member(discord_id)


def _alliance_role(guild, aid):
    if aid is None:
        return None
    row = storage.alliance_row(aid)
    return guild.get_role(int(row["role_id"])) if row and row["role_id"] else None


def _discord_timestamp(dt):
    return f"<t:{int(dt.timestamp())}:F>"


def _nap_display(gid, player, member):
    name = player.get("nick_name") or f"Governor {gid}"
    who = member.mention if member else discord.utils.escape_markdown(str(name))
    alliance = player.get("alliance") or {}
    tag = alliance.get("abbr")
    alliance_name = alliance.get("name") or tag
    alliance_text = f"[{tag}] {alliance_name}" if tag else "No alliance"
    return f"{who}\nGID: `{gid}`\nAlliance: **{discord.utils.escape_markdown(str(alliance_text))}**"


class NAPBreakingView(discord.ui.View):
    def __init__(self, cog, breaking_id, disabled=False):
        super().__init__(timeout=None)
        self.cog = cog
        self.breaking_id = breaking_id
        comp = discord.ui.Button(label="Compensation sent", style=discord.ButtonStyle.success,
                                 emoji="💰", custom_id=f"nap_breaking:{breaking_id}:compensation_sent",
                                 disabled=disabled)
        agree = discord.ui.Button(label="Agreement reached", style=discord.ButtonStyle.primary,
                                  emoji="🤝", custom_id=f"nap_breaking:{breaking_id}:agreement_reached",
                                  disabled=disabled)
        comp.callback = self.compensation_callback
        agree.callback = self.agreement_callback
        self.add_item(comp)
        self.add_item(agree)

    async def compensation_callback(self, interaction):
        await self.cog.resolve_nap_breaking(interaction, self.breaking_id, "compensation_sent")

    async def agreement_callback(self, interaction):
        await self.cog.resolve_nap_breaking(interaction, self.breaking_id, "agreement_reached")


class NapBreaking(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self._locks = {}

    async def cog_load(self):
        # Restore persistent buttons + timers for still-open incidents.
        for breaking_id, record in _nap_state()["nap_breakings"].items():
            if record.get("status") == "open" and record.get("message_id"):
                self.bot.add_view(NAPBreakingView(self, breaking_id), message_id=int(record["message_id"]))
                asyncio.create_task(self.watch_nap_breaking(breaking_id))

    @app_commands.command(name="nap_breaking", description="Report a NAP attack (24-hour deadline).")
    @app_commands.describe(attacker_gid="Governor ID of the attacker",
                           attacked_gid="Governor ID of the attacked player",
                           proof="Optional image attachment", proof_url="Optional proof link")
    async def nap_breaking(self, interaction, attacker_gid: str, attacked_gid: str,
                           proof: discord.Attachment | None = None, proof_url: str | None = None):
        await interaction.response.defer(ephemeral=True)
        settings = _settings(self.bot)
        attacker_gid = str(attacker_gid).strip()
        attacked_gid = str(attacked_gid).strip()
        proof_url = str(proof_url).strip() if proof_url else None

        if not settings.get_int("nap_channel_id"):
            await interaction.followup.send("`nap_channel_id` is not configured.", ephemeral=True)
            return
        if attacker_gid == attacked_gid:
            await interaction.followup.send("Attacker and attacked IDs must differ.", ephemeral=True)
            return
        if proof_url and not proof_url.startswith(("http://", "https://")):
            await interaction.followup.send("`proof_url` must start with http(s)://.", ephemeral=True)
            return
        if proof and proof_url:
            await interaction.followup.send("Provide either `proof` or `proof_url`, not both.", ephemeral=True)
            return

        attacker, attacked = await asyncio.gather(
            mightpulse.fetch_player_data(attacker_gid, force_refresh=True, bypass_local_cache=True),
            mightpulse.fetch_player_data(attacked_gid, force_refresh=True, bypass_local_cache=True),
        )
        errors = {"NOT_FOUND": "was not found", "RATE_LIMITED": "is rate-limited",
                  None: "could not be refreshed with trustworthy data"}
        for label, gid, result in (("Attacker", attacker_gid, attacker), ("Attacked", attacked_gid, attacked)):
            if not isinstance(result, dict):
                await interaction.followup.send(
                    f"{label} GID `{gid}` {errors.get(result, 'could not be refreshed')}.", ephemeral=True)
                return

        for label, result in (("Attacker", attacker), ("Attacked", attacked)):
            alliance = result.get("alliance") or {}
            denial = roles.whitelist_denial(
                result.get("kid"), alliance.get("aid"),
                whitelist_kids=settings.get_json("whitelist_kids", []),
                whitelist_alliances=settings.get_json("whitelist_alliances", []),
            )
            if denial:
                await interaction.followup.send(f"{label} {denial}.", ephemeral=True)
                return

        guild = interaction.guild
        attacker_member = _member_for_fid(guild, attacker_gid)
        attacked_member = _member_for_fid(guild, attacked_gid)
        a_all = attacker.get("alliance") or {}
        d_all = attacked.get("alliance") or {}
        attacker_role = _alliance_role(guild, a_all.get("aid"))
        attacked_role = _alliance_role(guild, d_all.get("aid"))
        attacker_prot = _protection(self.bot, aid=a_all.get("aid"), tag=a_all.get("abbr"))
        attacked_prot = _protection(self.bot, aid=d_all.get("aid"), tag=d_all.get("abbr"))

        now = datetime.now(timezone.utc)
        deadline = now + timedelta(hours=24)
        breaking_id = f"{int(now.timestamp())}-{attacker_gid}-{attacked_gid}"
        record = {
            "id": breaking_id, "status": "open", "created_at": now.isoformat(),
            "deadline": deadline.isoformat(), "created_by": interaction.user.id,
            "attacker_gid": attacker_gid, "attacked_gid": attacked_gid,
            "attacker_discord_id": attacker_member.id if attacker_member else None,
            "attacked_discord_id": attacked_member.id if attacked_member else None,
            "attacker_alliance_aid": a_all.get("aid"),
            "attacked_alliance_aid": d_all.get("aid"),
            "attacker_alliance_role_id": attacker_role.id if attacker_role else None,
            "attacked_alliance_role_id": attacked_role.id if attacked_role else None,
            "attacker_nap_status": attacker_prot[1],
            "attacked_nap_status": attacked_prot[1],
            "attacker_display": _nap_display(attacker_gid, attacker, attacker_member),
            "attacked_display": _nap_display(attacked_gid, attacked, attacked_member),
            "proof_url": proof_url,
        }
        upload = None
        if proof:
            try:
                upload = await proof.to_file()
                record["proof_url"] = f"attachment://{upload.filename}"
            except (discord.HTTPException, discord.Forbidden):
                await interaction.followup.send("Could not read the proof attachment.", ephemeral=True)
                return

        channel = self.bot.get_channel(settings.get_int("nap_channel_id"))
        if channel is None:
            channel = await self.bot.fetch_channel(settings.get_int("nap_channel_id"))
        view = NAPBreakingView(self, breaking_id)
        kwargs = {
            "content": self.nap_breaking_ping_content(record),
            "embed": self.build_nap_breaking_embed(record),
            "view": view,
            "allowed_mentions": discord.AllowedMentions(users=True, roles=True, everyone=False),
        }
        if upload:
            kwargs["file"] = upload
        message = await channel.send(**kwargs)
        record["message_id"] = message.id
        record["channel_id"] = channel.id
        _save_breaking(breaking_id, record)
        asyncio.create_task(self.watch_nap_breaking(breaking_id))
        await interaction.followup.send(f"✅ NAP breaking posted (24h deadline).\n{message.jump_url}", ephemeral=True)

    async def resolve_nap_breaking(self, interaction, breaking_id, resolution):
        record = _get_breaking(breaking_id)
        if not record:
            await interaction.response.send_message("❌ This NAP incident no longer exists.", ephemeral=True)
            return
        if not isinstance(interaction.user, discord.Member) or not self.can_resolve_nap_breaking(interaction.user, record):
            await interaction.response.send_message(
                "⛔ Only Council/Admin, the attacked player, or an R4/R5 of the attacked alliance may resolve this.",
                ephemeral=True)
            return
        lock = self._locks.setdefault(breaking_id, asyncio.Lock())
        async with lock:
            record = _get_breaking(breaking_id)
            if not record or record.get("status") != "open":
                await interaction.response.send_message("ℹ️ This incident is already closed.", ephemeral=True)
                return
            now = datetime.now(timezone.utc)
            if now >= datetime.fromisoformat(record["deadline"]):
                await interaction.response.send_message("⏰ The 24-hour window has already expired.", ephemeral=True)
                asyncio.create_task(self.expire_nap_breaking(breaking_id))
                return
            record["status"] = "resolved"
            record["resolution"] = resolution
            record["resolved_by"] = interaction.user.id
            record["resolved_at"] = now.isoformat()
            _save_breaking(breaking_id, record)
            await interaction.response.edit_message(
                embed=self.build_nap_breaking_embed(record),
                view=NAPBreakingView(self, breaking_id, disabled=True),
            )

    async def expire_nap_breaking(self, breaking_id):
        lock = self._locks.setdefault(breaking_id, asyncio.Lock())
        async with lock:
            record = _get_breaking(breaking_id)
            if not record or record.get("status") != "open":
                return
            now = datetime.now(timezone.utc)
            if now < datetime.fromisoformat(record["deadline"]):
                return
            record["status"] = "expired"
            record["expired_at"] = now.isoformat()
            _save_breaking(breaking_id, record)
            settings = _settings(self.bot)
            try:
                channel = self.bot.get_channel(record.get("channel_id"))
                if channel is None:
                    channel = await self.bot.fetch_channel(record.get("channel_id"))
                message = await channel.fetch_message(int(record["message_id"]))
                await message.edit(embed=self.build_nap_breaking_embed(record),
                                   view=NAPBreakingView(self, breaking_id, disabled=True))
                mentions = []
                if settings.get_int("role_council_id"):
                    mentions.append(f"<@&{settings.get_int('role_council_id')}>")
                for role_id in (record.get("attacker_alliance_role_id"), record.get("attacked_alliance_role_id")):
                    if role_id:
                        mentions.append(f"<@&{role_id}>")
                if record.get("attacked_discord_id"):
                    mentions.append(f"<@{record['attacked_discord_id']}>")
                await channel.send(
                    (" ".join(dict.fromkeys(mentions)) + "\n" if mentions else "")
                    + "🚨 **NAP RULE BREAK:** no compensation or agreement within 24 hours.",
                    reference=message,
                    allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
                )
            except (discord.NotFound, discord.Forbidden, discord.HTTPException, RuntimeError) as e:
                logger.warning("Could not publish expiry for %s: %s", breaking_id, e)

    async def watch_nap_breaking(self, breaking_id):
        await self.bot.wait_until_ready()
        record = _get_breaking(breaking_id)
        if not record or record.get("status") != "open":
            return
        delay = max(0.0, (datetime.fromisoformat(record["deadline"]) - datetime.now(timezone.utc)).total_seconds())
        if delay:
            await asyncio.sleep(delay)
        await self.expire_nap_breaking(breaking_id)

    def can_resolve_nap_breaking(self, member, record):
        if member.guild_permissions.administrator:
            return True
        settings = _settings(self.bot)
        council_id = settings.get_int("role_council_id")
        if council_id and any(r.id == council_id for r in member.roles):
            return True
        attacked_discord_id = record.get("attacked_discord_id")
        if attacked_discord_id and member.id == int(attacked_discord_id):
            return True
        attacked_alliance_role_id = record.get("attacked_alliance_role_id")
        if attacked_alliance_role_id and any(r.id == int(attacked_alliance_role_id) for r in member.roles):
            rank_ids = {rid for rid in (settings.get_int("role_r4_id"), settings.get_int("role_r5_id")) if rid}
            if any(r.id in rank_ids for r in member.roles):
                return True
        return False

    def build_nap_breaking_embed(self, record):
        created_at = datetime.fromisoformat(record["created_at"])
        deadline = datetime.fromisoformat(record["deadline"])
        status = record.get("status", "open")
        if status == "resolved":
            color, title = discord.Color.green(), "✅ NAP Incident Resolved"
        elif status == "expired":
            color, title = discord.Color.red(), "🚨 NAP Rule Break — Deadline Missed"
        else:
            color, title = discord.Color.orange(), "⚠️ NAP Breaking Report"

        embed = discord.Embed(title=title, color=color)
        embed.add_field(name="Attacker", value=record.get("attacker_display", "?"), inline=False)
        embed.add_field(name="Attacked player", value=record.get("attacked_display", "?"), inline=False)
        embed.add_field(name="Reported", value=f"{_discord_timestamp(created_at)} (UTC)", inline=True)
        embed.add_field(name="Deadline", value=f"{_discord_timestamp(deadline)} (UTC)", inline=True)

        nap_lines = []
        if record.get("attacker_nap_status"):
            nap_lines.append(f"Attacker alliance — {record['attacker_nap_status']}")
        if record.get("attacked_nap_status"):
            nap_lines.append(f"Attacked alliance — {record['attacked_nap_status']}")
        if nap_lines:
            embed.add_field(name="NAP protection (UTC)", value="\n".join(nap_lines), inline=False)

        if record.get("proof_url"):
            proof_url = record["proof_url"]
            if proof_url.startswith(("http://", "https://")):
                embed.add_field(name="Proof", value=f"[Open proof]({proof_url})", inline=False)
                embed.set_image(url=proof_url)
            elif proof_url.startswith("attachment://"):
                embed.set_image(url=proof_url)

        if status == "resolved":
            resolution = record.get("resolution", "resolved").replace("_", " ").title()
            resolver = f"<@{record['resolved_by']}>" if record.get("resolved_by") else "Unknown"
            resolved_at = datetime.fromisoformat(record["resolved_at"])
            embed.add_field(name="Resolution", value=f"**{resolution}** by {resolver}\n{_discord_timestamp(resolved_at)}",
                            inline=False)
        elif status == "expired":
            embed.add_field(name="Status", value="No compensation or agreement within 24 hours — rule break.", inline=False)
        else:
            embed.add_field(name="How to close this incident",
                            value="Use **Compensation sent** or **Agreement reached** below.", inline=False)

        embed.set_footer(text=f"NAP incident ID: {record['id']}")
        return embed

    def nap_breaking_ping_content(self, record):
        settings = _settings(self.bot)
        mentions = []
        council_id = settings.get_int("role_council_id")
        if council_id:
            mentions.append(f"<@&{council_id}>")
        for role_id in (record.get("attacker_alliance_role_id"), record.get("attacked_alliance_role_id")):
            if role_id:
                mention = f"<@&{role_id}>"
                if mention not in mentions:
                    mentions.append(mention)
        return " ".join(mentions) + ("\n" if mentions else "") + "A NAP breaking report has been opened."
