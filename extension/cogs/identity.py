"""Account / identity cogs.

``IdentityBase`` is always loaded and owns /unregister, /transfer, /set_main_fid,
/userdata and the admin variants. ``IdentityMightpulse`` loads only when a
MightPulse key is configured, overriding upstream /register with a live-data link
plus /refresh. The extension entrypoint wires both and removes the upstream
commands they replace.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

from cogs.permission_handler import PermissionManager

from .. import roles, storage
from ..providers import mightpulse
from ..settings import SettingsStore

logger = logging.getLogger("extension")

_LOOKUP_ERRORS = {
    "NOT_FOUND": "was not found on MightPulse",
    "RATE_LIMITED": "could not be refreshed because MightPulse is rate-limited",
    None: "could not be refreshed with trustworthy current data",
}


def _settings(bot) -> SettingsStore:
    return getattr(bot, "extension_settings", None) or SettingsStore()


def _top_aids(bot) -> set:
    n = _settings(bot).get_int("council_top_alliances_count", 20)
    return {str(aid) for aid in storage.top_alliances_by_power(n)}


def _alliance_row(bot, aid):
    return storage.alliance_row(aid) if aid is not None else None


def _alliance_abbr(bot, aid):
    row = _alliance_row(bot, aid)
    return row["abbr"] if row else None


def _alliance_role_id(bot, aid):
    row = _alliance_row(bot, aid)
    return row["role_id"] if row else None


def _managed_role_ids(bot) -> list:
    ids = [
        _settings(bot).get_int("verified_role_id"),
        _settings(bot).get_int("role_r5_id"),
        _settings(bot).get_int("role_r4_id"),
        _settings(bot).get_int("role_council_id"),
    ]
    ids += storage.all_alliance_role_ids()
    return [i for i in ids if i is not None]


async def _apply(bot, member, nick, add, remove) -> None:
    """Apply computed nickname + role changes to a member, best-effort."""
    guild = member.guild
    add_roles = [guild.get_role(i) for i in add]
    add_roles = [r for r in add_roles if r is not None and r not in member.roles]
    remove_roles = [guild.get_role(i) for i in remove]
    remove_roles = [r for r in remove_roles if r is not None and r in member.roles]
    try:
        if add_roles:
            await member.add_roles(*add_roles, reason="Player sync")
        if remove_roles:
            await member.remove_roles(*remove_roles, reason="Player sync")
        if member.nick != nick:
            await member.edit(nick=nick[:32], reason="Player sync")
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("Could not sync member %s: %s", member.id, e)


async def _ensure_alliance_role(bot, guild, aid):
    """Return the alliance's Discord role id, creating/renaming it as needed."""
    if aid is None:
        return None
    row = storage.alliance_row(aid)
    if not row:
        return None
    abbr = row["abbr"] or f"aid {aid}"
    name = row["name"] or abbr
    expected_name = f"[{abbr}] {name}"[:100]
    if row["role_id"]:
        role = guild.get_role(int(row["role_id"]))
        if role:
            if role.name != expected_name:
                try:
                    await role.edit(name=expected_name, reason="Alliance data sync")
                except (discord.Forbidden, discord.HTTPException) as e:
                    logger.warning("Could not rename alliance role for aid=%s: %s", aid, e)
            return role.id
    try:
        role = await guild.create_role(name=expected_name, mentionable=True,
                                       reason="Auto-created alliance role")
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("Could not create alliance role for aid=%s: %s", aid, e)
        return None
    storage.upsert_alliance(aid, role_id=role.id)
    await _reorder_alliance_roles(bot, guild)
    return role.id


async def _reorder_alliance_roles(bot, guild):
    """Stack alliance roles below ROLE_BOUND_TOP, highest power first."""
    top_id = _settings(bot).get_int("role_bound_top")
    if not top_id:
        return
    top_anchor = guild.get_role(top_id)
    if not top_anchor:
        return
    active = []
    for row in storage.all_alliances():
        if row["role_id"]:
            role = guild.get_role(int(row["role_id"]))
            if role:
                active.append({"role": role, "power": row["power"] or 0})
    if not active:
        return
    active.sort(key=lambda x: x["power"], reverse=True)
    start = max(top_anchor.position - 1, len(active) - 1)
    payload = {item["role"]: start - i for i, item in enumerate(active)}
    try:
        await guild.edit_role_positions(positions=payload)
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning("Role reorder failed: %s", e)


async def sync_member(bot, member) -> None:
    """Recompute + apply nickname and roles from the member's main FID."""
    settings = _settings(bot)
    fid = storage.main_fid_for_discord(member.id)

    if fid is None:
        # No linked accounts: strip every extension-managed role.
        await _apply(bot, member, member.nick or member.display_name, [], _managed_role_ids(bot))
        return

    row = storage.user_row(fid)
    aid = row["alliance"] if row else None
    alliance_role_id = await _ensure_alliance_role(bot, member.guild, aid)
    nick, add, remove = roles.compute_sync(
        nickname=row["nickname"] if row else None,
        abbr=_alliance_abbr(bot, aid),
        rank=row["rank"] if row else None,
        alliance_aid=aid,
        top_aids=_top_aids(bot),
        alliance_role_id=alliance_role_id,
        verified_role_id=settings.get_int("verified_role_id"),
        role_r5_id=settings.get_int("role_r5_id"),
        role_r4_id=settings.get_int("role_r4_id"),
        role_council_id=settings.get_int("role_council_id"),
    )
    # Remove any other alliance role (e.g. the member switched alliances).
    for other_role_id in storage.all_alliance_role_ids():
        if other_role_id != alliance_role_id and other_role_id not in remove:
            remove.append(other_role_id)
    await _apply(bot, member, nick, add, remove)


def _owned_fids(caller_id: int) -> list:
    return [fid for fid, _ in storage.fids_for_discord(caller_id)]


class IdentityBase(commands.Cog):
    """Always-loaded account management (no MightPulse needed)."""

    def __init__(self, bot):
        self.bot = bot

    async def _resolve_fids(self, interaction, fids_text):
        """Given FIDs or all of the caller's; returns list or None (after replying)."""
        if fids_text:
            fids = roles.parse_fids(fids_text)
            if fids is None:
                await interaction.followup.send(
                    "Invalid FID list. Use comma/space-separated numeric IDs.", ephemeral=True)
                return None
            return fids
        return _owned_fids(interaction.user.id)

    @app_commands.command(name="unregister", description="Unlink accounts (blank = all of yours).")
    @app_commands.describe(fids="Comma/space-separated FIDs; leave empty to unlink all")
    async def unregister(self, interaction, fids: str | None = None):
        await interaction.response.defer(ephemeral=True)
        targets = await self._resolve_fids(interaction, fids)
        if targets is None:
            return
        owned = {fid for fid, _ in storage.fids_for_discord(interaction.user.id)}
        targets = [f for f in targets if f in owned]
        if not targets:
            await interaction.followup.send("You have no linked accounts to unregister.", ephemeral=True)
            return
        storage.detach_fids(targets)
        await sync_member(self.bot, interaction.user)
        await interaction.followup.send(f"Unlinked {len(targets)} account(s).", ephemeral=True)

    @app_commands.command(name="transfer", description="Move accounts to another user (blank = all).")
    @app_commands.describe(to="Discord member to receive the accounts",
                           fids="Comma/space-separated FIDs; leave empty for all")
    async def transfer(self, interaction, to: discord.Member, fids: str | None = None):
        await interaction.response.defer(ephemeral=True)
        targets = await self._resolve_fids(interaction, fids)
        if targets is None:
            return
        owned = {fid for fid, _ in storage.fids_for_discord(interaction.user.id)}
        targets = [f for f in targets if f in owned]
        if not targets:
            await interaction.followup.send("You have no linked accounts to transfer.", ephemeral=True)
            return
        if to.id == interaction.user.id:
            await interaction.followup.send("You can't transfer accounts to yourself.", ephemeral=True)
            return
        storage.transfer_fids(targets, to.id, to_server_id=interaction.guild_id)
        await sync_member(self.bot, interaction.user)
        await sync_member(self.bot, to)
        await interaction.followup.send(f"Transferred {len(targets)} account(s) to {to.mention}.", ephemeral=True)

    @app_commands.command(name="set_main_fid", description="Set which linked account drives your identity.")
    @app_commands.describe(fid="The FID to make primary")
    async def set_main_fid(self, interaction, fid: int):
        await interaction.response.defer(ephemeral=True)
        if not storage.set_main_fid(interaction.user.id, fid):
            await interaction.followup.send("That account isn't linked to you.", ephemeral=True)
            return
        await sync_member(self.bot, interaction.user)
        await interaction.followup.send(f"Set `{fid}` as your main account.", ephemeral=True)

    userdata = app_commands.Group(name="userdata", description="Look up linked accounts")

    @userdata.command(name="by_user", description="List a member's linked game accounts.")
    async def userdata_by_user(self, interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        fids = storage.fids_for_discord(member.id)
        if not fids:
            await interaction.followup.send(f"{member.mention} has no linked accounts.", ephemeral=True)
            return
        lines = []
        for fid, is_main in fids:
            row = storage.user_row(fid)
            name = row["nickname"] if row else None
            lines.append(f"`{fid}` {name or ''}{' (main)' if is_main else ''}")
        await interaction.followup.send(f"{member.mention} accounts:\n" + "\n".join(lines), ephemeral=True)

    @userdata.command(name="by_fid", description="Find the Discord user who owns a game account.")
    async def userdata_by_fid(self, interaction, fid: int):
        await interaction.response.defer(ephemeral=True)
        discord_id = storage.discord_for_fid(fid)
        if discord_id is None:
            await interaction.followup.send(f"FID `{fid}` is not linked to anyone.", ephemeral=True)
            return
        await interaction.followup.send(f"FID `{fid}` is linked to <@{discord_id}>.", ephemeral=True)

    @app_commands.command(name="admin_unbind", description="Admin: clear the Discord link from a FID.")
    async def admin_unbind(self, interaction, fid: int):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        affected = storage.detach_fids([fid])
        for discord_id in affected:
            member = interaction.guild.get_member(discord_id)
            if member:
                await sync_member(self.bot, member)
        await interaction.followup.send(f"Unbound FID `{fid}`.", ephemeral=True)

    @app_commands.command(name="admin_transfer", description="Admin: move accounts between users.")
    async def admin_transfer(self, interaction, from_user: discord.Member, to: discord.Member,
                             fids: str | None = None):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        if fids:
            targets = roles.parse_fids(fids)
            if targets is None:
                await interaction.followup.send("Invalid FID list.", ephemeral=True)
                return
        else:
            targets = _owned_fids(from_user.id)
        storage.transfer_fids(targets, to.id, to_server_id=interaction.guild_id)
        await sync_member(self.bot, from_user)
        await sync_member(self.bot, to)
        await interaction.followup.send(f"Transferred {len(targets)} account(s).", ephemeral=True)

    @app_commands.command(name="admin_set_main", description="Admin: set a member's main account.")
    @app_commands.describe(member="The Discord member", fid="The FID to make primary")
    async def admin_set_main(self, interaction, member: discord.Member, fid: int):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        if not storage.set_main_fid(member.id, fid):
            await interaction.followup.send("That account isn't linked to that member.", ephemeral=True)
            return
        await sync_member(self.bot, member)
        await interaction.followup.send(f"Set `{fid}` as {member.mention}'s main account.", ephemeral=True)


class IdentityMightpulse(commands.Cog):
    """Live-data commands, loaded only when a MightPulse key is configured."""

    def __init__(self, bot):
        self.bot = bot

    async def _verify(self, interaction, gid: str):
        await interaction.response.defer(ephemeral=True)
        settings = _settings(self.bot)
        player = await mightpulse.fetch_player_data(gid, force_refresh=True)
        if not isinstance(player, dict):
            reason = _LOOKUP_ERRORS.get(player, "could not be refreshed")
            await interaction.followup.send(f"That ID {reason}.", ephemeral=True)
            return
        alliance = player.get("alliance") or {}
        denial = roles.whitelist_denial(
            player.get("kid"), alliance.get("aid"),
            whitelist_kids=settings.get_json("whitelist_kids", []),
            whitelist_alliances=settings.get_json("whitelist_alliances", []),
        )
        if denial:
            await interaction.followup.send(f"{denial}.", ephemeral=True)
            return
        fields = roles.player_to_user_fields(gid, player)
        storage.register_player(
            fid=fields["fid"], discord_id=interaction.user.id,
            discord_server_id=interaction.guild_id, nickname=fields["nickname"],
            kid=fields["kid"], alliance_aid=fields["alliance_aid"],
            rank=fields["rank"], power=fields["power"],
            abbr=fields["abbr"], alliance_name=fields["alliance_name"],
        )
        await sync_member(self.bot, interaction.user)
        await interaction.followup.send(f"Linked and synced ID `{gid}`.", ephemeral=True)

    @app_commands.command(name="verify", description="Verify your Governor ID and sync roles from live data.")
    @app_commands.describe(gid="Your in-game Governor ID")
    async def verify(self, interaction, gid: str):
        await self._verify(interaction, str(gid).strip())

    @app_commands.command(name="register", description="Link your in-game ID and sync roles from live data.")
    @app_commands.describe(gid="Your in-game Governor ID")
    async def register(self, interaction, gid: str):
        """Compatibility alias for /verify."""
        await self._verify(interaction, str(gid).strip())

    @app_commands.command(name="refresh", description="Re-sync your roles and nickname from live data.")
    async def refresh(self, interaction):
        await interaction.response.defer(ephemeral=True)
        fid = storage.main_fid_for_discord(interaction.user.id)
        if fid is None:
            await interaction.followup.send("You have no linked account to refresh.", ephemeral=True)
            return
        player = await mightpulse.fetch_player_data(fid, force_refresh=True)
        if not isinstance(player, dict):
            reason = _LOOKUP_ERRORS.get(player, "could not be refreshed")
            await interaction.followup.send(f"Could not refresh: {reason}.", ephemeral=True)
            return
        fields = roles.player_to_user_fields(fid, player)
        storage.register_player(
            fid=fields["fid"], discord_id=interaction.user.id,
            discord_server_id=interaction.guild_id, nickname=fields["nickname"],
            kid=fields["kid"], alliance_aid=fields["alliance_aid"],
            rank=fields["rank"], power=fields["power"],
            abbr=fields["abbr"], alliance_name=fields["alliance_name"],
        )
        await sync_member(self.bot, interaction.user)
        await interaction.followup.send("Refreshed your roles and nickname.", ephemeral=True)

    @app_commands.command(name="admin_bind", description="Admin: link an ID to another member.")
    async def admin_bind(self, interaction, member: discord.Member, gid: str):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        settings = _settings(self.bot)
        player = await mightpulse.fetch_player_data(gid, force_refresh=True)
        if not isinstance(player, dict):
            reason = _LOOKUP_ERRORS.get(player, "could not be refreshed")
            await interaction.followup.send(f"That ID {reason}.", ephemeral=True)
            return
        alliance = player.get("alliance") or {}
        denial = roles.whitelist_denial(
            player.get("kid"), alliance.get("aid"),
            whitelist_kids=settings.get_json("whitelist_kids", []),
            whitelist_alliances=settings.get_json("whitelist_alliances", []),
        )
        if denial:
            await interaction.followup.send(f"{denial}.", ephemeral=True)
            return
        fields = roles.player_to_user_fields(gid, player)
        storage.register_player(
            fid=fields["fid"], discord_id=member.id, discord_server_id=interaction.guild_id,
            nickname=fields["nickname"], kid=fields["kid"], alliance_aid=fields["alliance_aid"],
            rank=fields["rank"], power=fields["power"], abbr=fields["abbr"],
            alliance_name=fields["alliance_name"],
        )
        await sync_member(self.bot, member)
        await interaction.followup.send(f"Linked `{gid}` to {member.mention}.", ephemeral=True)

    @app_commands.command(name="admin_refresh", description="Admin: re-sync another member.")
    async def admin_refresh(self, interaction, member: discord.Member):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        fid = storage.main_fid_for_discord(member.id)
        if fid is None:
            await interaction.followup.send(f"{member.mention} has no linked account.", ephemeral=True)
            return
        player = await mightpulse.fetch_player_data(fid, force_refresh=True)
        if isinstance(player, dict):
            fields = roles.player_to_user_fields(fid, player)
            storage.register_player(
                fid=fields["fid"], discord_id=member.id, discord_server_id=interaction.guild_id,
                nickname=fields["nickname"], kid=fields["kid"], alliance_aid=fields["alliance_aid"],
                rank=fields["rank"], power=fields["power"], abbr=fields["abbr"],
                alliance_name=fields["alliance_name"],
            )
        await sync_member(self.bot, member)
        await interaction.followup.send(f"Refreshed {member.mention}.", ephemeral=True)
