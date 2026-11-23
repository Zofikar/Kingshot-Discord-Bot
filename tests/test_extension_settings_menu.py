import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extension.cogs.settings import MENU_KEY, Settings, SettingModal, SettingsView
from extension.settings import SettingsStore, seed_from_config
from extension.settings_schema import SETTING_NAMES, validate_setting
from extension.config import Config
from cogs.permission_handler import PermissionManager
from cogs.bot_main_menu import MainMenu, MainMenuView


def interaction():
    return SimpleNamespace(user=SimpleNamespace(id=42), response=SimpleNamespace(
        send_message=AsyncMock(), send_modal=AsyncMock()))


@pytest.mark.parametrize("name,value", [
    ("unknown", "1"), ("verified_role_id", "-1"), ("nap_alliances_count", "0"),
    ("nap_candidate_multiplier", "nan"), ("nap_candidate_multiplier", "0.5"),
    ("whitelist_kids", "[true]"), ("whitelist_alliances", "{}"),
    ("nap_snapshot_lookback_protected", "-1"),
])
def test_invalid_settings(name, value):
    with pytest.raises(ValueError):
        validate_setting(name, value)


def test_registry_matches_seeded_settings(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.sqlite"))
    seed_from_config(store, Config())
    assert set(store.all_keys()) == set(SETTING_NAMES)
    for name in SETTING_NAMES:
        validate_setting(name, store.get(name))
    assert validate_setting("whitelist_kids", "[123, 123]") == "[123]"
    assert validate_setting("verified_role_id", " ") == ""


def test_hook_lifecycle_and_old_menu_click(tmp_path):
    async def run():
        bot = SimpleNamespace(extension_settings=SettingsStore(str(tmp_path / "settings.sqlite")))
        cog = Settings(bot)
        core = MainMenu(bot)
        assert not MainMenuView(core).children[-1].custom_id == MENU_KEY
        await cog.cog_load()
        view = MainMenuView(core)
        assert core.build_main_menu_embed().fields[-1].name == "Extension Settings"
        button = next(item for item in view.children if item.custom_id == MENU_KEY)
        replacement = AsyncMock()
        bot.settings_menu_entries[MENU_KEY] = {**cog.entry, "callback": replacement}
        click = interaction()
        await button.callback(click)
        replacement.assert_awaited_once_with(click)
        # Unloading an older cog must not unregister a newer contribution.
        await cog.cog_unload()
        assert MENU_KEY in bot.settings_menu_entries
        bot.settings_menu_entries[MENU_KEY] = cog.entry
        await cog.cog_unload()
        click = interaction()
        await button.callback(click)
        assert click.response.send_message.call_args.kwargs["ephemeral"]
        assert MENU_KEY not in bot.settings_menu_entries
    asyncio.run(run())


def test_modal_validation_and_save(tmp_path, monkeypatch):
    async def run():
        cog = Settings(SimpleNamespace(extension_settings=SettingsStore(str(tmp_path / "settings.sqlite"))))
        monkeypatch.setattr(PermissionManager, "is_admin", lambda uid: (True, True))
        modal = SettingModal(cog, "verified_role_id")
        modal.value._value = "123"
        await modal.on_submit(interaction())
        assert cog.store.get("verified_role_id") == "123"
        modal.value._value = "invalid"
        await modal.on_submit(interaction())
        assert cog.store.get("verified_role_id") == "123"
    asyncio.run(run())


def test_permissions_rechecked_for_menu_and_modal(tmp_path, monkeypatch):
    async def run():
        cog = Settings(SimpleNamespace(extension_settings=SettingsStore(str(tmp_path / "settings.sqlite"))))
        monkeypatch.setattr(PermissionManager, "is_admin", lambda uid: (True, True))
        click = interaction()
        await cog.show_settings(click)
        assert click.response.send_message.call_args.kwargs["ephemeral"]
        modal = SettingModal(cog, "verified_role_id")
        monkeypatch.setattr(PermissionManager, "is_admin", lambda uid: (True, False))
        assert not await SettingsView(cog).interaction_check(interaction())
        await modal.on_submit(interaction())
        assert cog.store.get("verified_role_id") is None
        await cog.admin_setting.callback(cog, interaction(), "verified_role_id", "123")
        assert cog.store.get("verified_role_id") is None
        monkeypatch.setattr(PermissionManager, "is_admin", lambda uid: (True, True))
        await cog.admin_setting.callback(cog, interaction(), "verified_role_id", "123")
        assert cog.store.get("verified_role_id") == "123"
        await cog.admin_setting.callback(cog, interaction(), "verified_role_id", "bad")
        assert cog.store.get("verified_role_id") == "123"
    asyncio.run(run())