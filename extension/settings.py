"""Extension runtime settings, stored the same way the upstream bot does.

Upstream keeps global settings in ``db/settings.sqlite`` under the
``bot_global_settings(setting_key, setting_value)`` table. The extension stores
its own configuration there too, namespaced under ``ext.`` keys, so there is a
single settings surface and a single database. ``.env`` only seeds first-run
defaults via :func:`seed_from_config`; after that the database is authoritative
and can be edited at runtime without a restart.
"""

import json
import os
import sqlite3

SETTINGS_DB = "db/settings.sqlite"
TABLE = "bot_global_settings"
PREFIX = "ext."


class SettingsStore:
    """Thin key/value wrapper over ``bot_global_settings`` (namespaced ``ext.``)."""

    def __init__(self, db_path: str = SETTINGS_DB):
        self.db_path = db_path

    def _connect(self) -> sqlite3.Connection:
        directory = os.path.dirname(self.db_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        return sqlite3.connect(self.db_path, timeout=30.0)

    def _ensure_table(self, conn) -> None:
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS {TABLE} "
            "(setting_key TEXT PRIMARY KEY, setting_value TEXT)"
        )

    @staticmethod
    def _key(name: str) -> str:
        return PREFIX + name

    def get(self, name: str, default=None) -> str | None:
        with self._connect() as conn:
            self._ensure_table(conn)
            row = conn.execute(
                f"SELECT setting_value FROM {TABLE} WHERE setting_key = ?",
                (self._key(name),),
            ).fetchone()
        return row[0] if row else default

    def get_int(self, name: str, default=None) -> int | None:
        raw = self.get(name)
        if raw is None or str(raw).strip() == "":
            return default
        try:
            return int(raw)
        except (TypeError, ValueError):
            return default

    def get_json(self, name: str, default=None):
        raw = self.get(name)
        if raw is None:
            return default
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return default

    def set(self, name: str, value) -> None:
        with self._connect() as conn:
            self._ensure_table(conn)
            conn.execute(
                f"INSERT INTO {TABLE} (setting_key, setting_value) VALUES (?, ?) "
                "ON CONFLICT(setting_key) DO UPDATE SET setting_value = excluded.setting_value",
                (self._key(name), str(value)),
            )
            conn.commit()

    def set_json(self, name: str, value) -> None:
        self.set(name, json.dumps(value))

    def all_keys(self) -> list:
        """All extension setting names (without the ``ext.`` prefix), sorted."""
        with self._connect() as conn:
            self._ensure_table(conn)
            rows = conn.execute(
                f"SELECT setting_key FROM {TABLE} WHERE setting_key LIKE ?",
                (PREFIX + "%",),
            ).fetchall()
        return sorted(r[0][len(PREFIX):] for r in rows)

    def seed(self, mapping: dict) -> None:
        """Insert defaults for keys that are not already present."""
        if not mapping:
            return
        with self._connect() as conn:
            self._ensure_table(conn)
            for name, value in mapping.items():
                conn.execute(
                    f"INSERT OR IGNORE INTO {TABLE} (setting_key, setting_value) VALUES (?, ?)",
                    (self._key(name), str(value)),
                )
            conn.commit()


def seed_from_config(store: SettingsStore, config) -> list:
    """Seed first-run extension settings from the parsed environment config.

    Returns the list of setting names seeded (for logging). Values that are None
    are stored as empty strings so the key still exists and can be overridden.
    """
    def _s(value) -> str:
        return "" if value is None else str(value)

    whitelist_kids = [config.allowed_kingdom_id] if config.allowed_kingdom_id else []

    mapping = {
        "verified_role_id": _s(config.verified_role_id),
        "role_bound_top": _s(config.role_bound_top),
        "role_r5_id": _s(config.role_r5_id),
        "role_r4_id": _s(config.role_r4_id),
        "role_council_id": _s(config.role_council_id),
        "nap_channel_id": _s(config.nap_channel_id),
        "leaders_nap_post_channel": _s(config.leaders_nap_post_channel),
        "unified_id_channel_id": _s(config.unified_id_channel_id),
        "whitelist_kids": json.dumps(whitelist_kids),
        "whitelist_alliances": json.dumps([]),
        "council_top_alliances_count": str(config.council_top_alliances_count),
        "nap_alliances_count": str(config.nap_alliances_count),
        "nap_candidate_multiplier": str(config.nap_candidate_multiplier),
        "nap_snapshot_lookback_protected": str(config.nap_snapshot_lookback_protected),
    }
    store.seed(mapping)
    return list(mapping)
