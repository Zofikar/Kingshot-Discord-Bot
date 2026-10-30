from extension.config import Config
from extension.settings import SettingsStore, seed_from_config


def test_notification_settings_migrate_without_overwriting_new_names(tmp_path):
    store = SettingsStore(str(tmp_path / "settings.sqlite"))
    store.set("vote_admin_channel_id", 123)
    store.set("vote_admin_role_id", 456)
    store.set("vote_approval_notification_role_id", 789)
    seed_from_config(store, Config())
    assert store.get_int("vote_approval_notification_channel_id") == 123
    assert store.get_int("vote_approval_notification_role_id") == 789
    assert "vote_admin_channel_id" not in store.all_keys()
    assert "vote_admin_role_id" not in store.all_keys()
    seed_from_config(store, Config())
    assert store.get_int("vote_approval_notification_channel_id") == 123
