"""Runtime settings cog: /admin_setting over the ext.* settings store."""
import discord
from discord import app_commands
from discord.ext import commands

from cogs.permission_handler import PermissionManager

from ..settings import SettingsStore


class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @app_commands.command(name="admin_setting", description="View or override extension settings.")
    @app_commands.describe(name="Setting name (omit to list)", value="New value")
    async def admin_setting(self, interaction, name: str | None = None, value: str | None = None):
        await interaction.response.defer(ephemeral=True)
        if not PermissionManager.is_admin(interaction.user.id)[0]:
            await interaction.followup.send("You don't have admin permission.", ephemeral=True)
            return
        store = getattr(self.bot, "extension_settings", None) or SettingsStore()
        if name is None:
            keys = store.all_keys()
            if not keys:
                await interaction.followup.send("No extension settings configured.", ephemeral=True)
                return
            lines = [f"`{k}` = `{store.get(k)}`" for k in keys]
            await interaction.followup.send("\n".join(lines), ephemeral=True)
            return
        if value is None:
            await interaction.followup.send(f"`{name}` = `{store.get(name)}`", ephemeral=True)
            return
        store.set(name, value)
        await interaction.followup.send(f"Set `{name}` = `{value}`.", ephemeral=True)
