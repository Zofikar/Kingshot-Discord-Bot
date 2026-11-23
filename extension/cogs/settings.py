"""Extension-owned settings UI contributed to the original /settings menu."""
import discord
from discord import app_commands
from discord.ext import commands

from cogs.permission_handler import PermissionManager

from ..settings import SettingsStore
from ..settings_schema import CATEGORIES, SETTING_NAMES, validate_setting

MENU_KEY = "ext:settings"


async def require_global(interaction):
    is_admin, is_global = PermissionManager.is_admin(interaction.user.id)
    if is_admin and is_global:
        return True
    await interaction.response.send_message("Only global administrators can manage extension settings.", ephemeral=True)
    return False


class SettingModal(discord.ui.Modal, title="Edit extension setting"):
    def __init__(self, cog, name):
        super().__init__()
        self.cog = cog
        self.name = name
        self.value = discord.ui.TextInput(label="Value (blank clears optional IDs)",
                                         default=cog.store.get(name, ""), required=False,
                                         max_length=2000, style=discord.TextStyle.paragraph)
        self.add_item(self.value)

    async def on_submit(self, interaction):
        if not await require_global(interaction):
            return
        try:
            value = validate_setting(self.name, self.value.value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.cog.store.set(self.name, value)
        await interaction.response.send_message(f"Saved `{self.name}`.", ephemeral=True)


class SettingsView(discord.ui.View):
    def __init__(self, cog):
        super().__init__(timeout=300)
        for row, (category, names) in enumerate(CATEGORIES.items()):
            select = discord.ui.Select(placeholder=category, row=row, options=[
                discord.SelectOption(label=name, value=name) for name in names
            ])

            async def selected(interaction, control=select):
                await interaction.response.send_modal(SettingModal(cog, control.values[0]))

            select.callback = selected
            self.add_item(select)

    async def interaction_check(self, interaction):
        return await require_global(interaction)


class Settings(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.store = getattr(bot, "extension_settings", None) or SettingsStore()
        self.entry = {"label": "Extension Settings",
                      "description": "Identity, council, voting, NAP and whitelists (Global Admin only).",
                      "callback": self.show_settings}

    async def cog_load(self):
        if not hasattr(self.bot, "settings_menu_entries"):
            self.bot.settings_menu_entries = {}
        self.bot.settings_menu_entries[MENU_KEY] = self.entry

    async def cog_unload(self):
        entries = getattr(self.bot, "settings_menu_entries", {})
        if entries.get(MENU_KEY) is self.entry:
            del entries[MENU_KEY]

    async def show_settings(self, interaction):
        if not await require_global(interaction):
            return
        embed = discord.Embed(title="Extension Settings", description=(
            "Bot-wide configuration. Select a setting to edit.\n"
            "Optional role/channel IDs: blank disables. Whitelists: JSON ID lists; [] allows all.\n"
            "Counts must be positive; lookback may be zero; multiplier must be at least 1."))
        for category, names in CATEGORIES.items():
            lines = [f"`{name}`: {str(self.store.get(name, '') or '(unset)')[:80]}" for name in names]
            embed.add_field(name=category, value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed, view=SettingsView(self), ephemeral=True)

    @app_commands.command(name="admin_setting", description="Extension settings (also available through /settings).")
    @app_commands.describe(name="Setting name (omit to open the menu)", value="New value")
    async def admin_setting(self, interaction: discord.Interaction, name: str | None = None, value: str | None = None):
        if name is None:
            await self.show_settings(interaction)
            return
        if not await require_global(interaction):
            return
        if name not in SETTING_NAMES:
            await interaction.response.send_message("Unknown setting. Open /settings → Extension Settings.", ephemeral=True)
            return
        if value is None:
            await interaction.response.send_message(f"`{name}` = `{self.store.get(name)}`", ephemeral=True)
            return
        try:
            validated = validate_setting(name, value)
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return
        self.store.set(name, validated)
        await interaction.response.send_message(f"Saved `{name}`.", ephemeral=True)
