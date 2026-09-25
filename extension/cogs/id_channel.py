"""Unified ID channel: post an ID, the bot detects the alliance and links it."""
import logging

from discord.ext import commands

from .. import roles, storage
from ..providers import mightpulse
from ..settings import SettingsStore

logger = logging.getLogger("extension")


class IDChannel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or message.guild is None or not mightpulse.available():
            return
        settings = getattr(self.bot, "extension_settings", None) or SettingsStore()
        channel_id = settings.get_int("unified_id_channel_id")
        if not channel_id or message.channel.id != channel_id:
            return

        content = (message.content or "").strip()
        if not content.isdigit():
            return
        gid = int(content)

        player = await mightpulse.fetch_player_data(gid)
        if not isinstance(player, dict):
            return
        alliance = player.get("alliance") or {}
        denial = roles.whitelist_denial(
            player.get("kid"), alliance.get("aid"),
            whitelist_kids=settings.get_json("whitelist_kids", []),
            whitelist_alliances=settings.get_json("whitelist_alliances", []),
        )
        if denial:
            await message.reply(f"{denial}.", mention_author=True)
            return

        fields = roles.player_to_user_fields(gid, player)
        storage.register_player(
            fid=fields["fid"], discord_id=message.author.id,
            discord_server_id=message.guild.id, nickname=fields["nickname"],
            kid=fields["kid"], alliance_aid=fields["alliance_aid"],
            rank=fields["rank"], power=fields["power"],
            abbr=fields["abbr"], alliance_name=fields["alliance_name"],
        )
        from .identity import sync_member
        await sync_member(self.bot, message.author)
        await message.reply(f"Linked ID `{gid}` (detected [{fields['abbr']}]).", mention_author=True)
