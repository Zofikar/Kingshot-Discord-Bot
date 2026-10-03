"""Data access for the extension, unified with the upstream bot's storage.

The extension reuses the upstream tables instead of keeping its own copies:

    players    -> db/users.sqlite     ``users``           (fid = in-game ID)
    alliances  -> db/alliance.sqlite  ``alliance_list``
    settings   -> db/settings.sqlite  ``bot_global_settings`` (see settings.py)

The only extension-owned state is NAP-specific, kept in
``db/verification.sqlite``: nap breakings, tag aliases, academies, exclusions.

This module owns:
  * ``ensure_schema()``     — create the NAP tables plus the additive columns the
    extension needs on ``users``/``alliance_list`` (idempotent ALTERs, matching
    the upstream migration pattern so upstream cogs are unaffected).
  * ``Storage``             — load/save the extension-owned NAP state.
  * ``import_from_json()``  — one-time migration of the pre-migration data.json.
"""

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone

VERIFICATION_FILE = "verification.sqlite"
USERS_FILE = "users.sqlite"
ALLIANCE_FILE = "alliance.sqlite"
SETTINGS_FILE = "settings.sqlite"

NAP_SECTIONS = ("nap_breakings", "nap_tag_aliases", "academies", "academy_tags", "nap_exclusions")

# Columns added on top of the base schemas below. This includes BOTH the columns
# upstream's create_tables() adds via ALTER (so a standalone migration produces a
# complete table) AND the extension's own columns. Idempotent via PRAGMA + ALTER.
USERS_COLUMNS = {
    # upstream-added
    "power": "INTEGER",
    "power_updated_at": "TEXT",
    "combat_power": "INTEGER",
    "combat_power_updated_at": "TEXT",
    "discord_id": "INTEGER",
    "discord_server_id": "INTEGER",
    "discord_id_updated_at": "TEXT",
    "state_mismatch_at": "TEXT",
    # extension-added
    "rank": "INTEGER",
    "is_main": "INTEGER DEFAULT 0",
}
ALLIANCE_COLUMNS = {
    # upstream-added
    "multistate": "INTEGER DEFAULT 0",
    "state_locked": "INTEGER DEFAULT 0",
    # extension-added
    "abbr": "TEXT",       # in-game tag, e.g. "MNX"
    "role_id": "INTEGER",  # Discord role created for this alliance
    "power": "INTEGER",    # last known alliance power
}

# Base schemas mirror the upstream ``main.py`` create_tables() so that, if this
# module runs before the bot's own schema setup, the upstream ALTER loops still
# find their base columns and can add the rest without divergence.
USERS_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    fid INTEGER PRIMARY KEY,
    nickname TEXT,
    furnace_lv INTEGER DEFAULT 0,
    kid INTEGER,
    stove_lv_content TEXT,
    alliance TEXT
);
"""

ALLIANCE_LIST_SCHEMA = """
CREATE TABLE IF NOT EXISTS alliance_list (
    alliance_id INTEGER PRIMARY KEY,
    name TEXT,
    discord_server_id INTEGER,
    kid INTEGER
);
"""

NAP_SCHEMA = """
CREATE TABLE IF NOT EXISTS nap_breakings (
    breaking_id TEXT PRIMARY KEY,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nap_tag_aliases (
    tag TEXT PRIMARY KEY,
    aid INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS academies (
    main_aid     TEXT PRIMARY KEY,
    academy_aid  INTEGER NOT NULL,
    academy_abbr TEXT
);
CREATE TABLE IF NOT EXISTS nap_exclusions (
    aid         TEXT PRIMARY KEY,
    record_json TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS nap_ranking_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    aid INTEGER NOT NULL,
    abbr TEXT,
    name TEXT,
    power INTEGER,
    posted_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_nap_history_posted_at ON nap_ranking_history(posted_at);
"""


def _dump(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _load(text: str):
    return json.loads(text)


def _connect(db_path: str) -> sqlite3.Connection:
    directory = os.path.dirname(db_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    return sqlite3.connect(db_path, timeout=30.0)


def _ensure_columns(conn, table: str, columns: dict) -> None:
    """Idempotently add columns to a table (PRAGMA + ALTER)."""
    existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, ctype in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ctype}")


def ensure_schema(db_dir: str = "db") -> None:
    """Create the NAP tables and add the extension's columns to upstream tables."""
    with _connect(os.path.join(db_dir, VERIFICATION_FILE)) as conn:
        conn.executescript(NAP_SCHEMA)
        _ensure_columns(conn, "academies", {"academy_abbr": "TEXT"})

    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        conn.executescript(USERS_SCHEMA)
        _ensure_columns(conn, "users", USERS_COLUMNS)

    with _connect(os.path.join(db_dir, ALLIANCE_FILE)) as conn:
        conn.executescript(ALLIANCE_LIST_SCHEMA)
        _ensure_columns(conn, "alliance_list", ALLIANCE_COLUMNS)


class Storage:
    """Extension-owned NAP state, backed by ``db/verification.sqlite``."""

    def __init__(self, db_dir: str = "db"):
        self.db_path = os.path.join(db_dir, VERIFICATION_FILE)

    def connect(self) -> sqlite3.Connection:
        return _connect(self.db_path)

    def init_schema(self) -> None:
        with self.connect() as conn:
            conn.executescript(NAP_SCHEMA)

    def load(self) -> dict:
        """Read the NAP state, reconstructed as the original dicts."""
        self.init_schema()
        result = {section: {} for section in NAP_SECTIONS}

        with self.connect() as conn:
            conn.row_factory = sqlite3.Row

            for row in conn.execute("SELECT * FROM nap_breakings"):
                result["nap_breakings"][row["breaking_id"]] = _load(row["record_json"])

            for row in conn.execute("SELECT * FROM nap_tag_aliases"):
                result["nap_tag_aliases"][row["tag"]] = row["aid"]

            for row in conn.execute("SELECT * FROM academies"):
                result["academies"][row["main_aid"]] = row["academy_aid"]
                if row["academy_abbr"]:
                    result["academy_tags"][str(row["academy_aid"])] = row["academy_abbr"]

            for row in conn.execute("SELECT * FROM nap_exclusions"):
                result["nap_exclusions"][row["aid"]] = _load(row["record_json"])

        return result

    def save(self, data: dict) -> None:
        """Persist the NAP state (wipe-and-rewrite, like the old save_data)."""
        self.init_schema()

        with self.connect() as conn:
            with conn:  # single transaction
                for table in ("nap_breakings", "nap_tag_aliases", "academies", "nap_exclusions"):
                    conn.execute(f"DELETE FROM {table}")

                for breaking_id, record in data.get("nap_breakings", {}).items():
                    conn.execute(
                        "INSERT INTO nap_breakings (breaking_id, record_json) VALUES (?, ?)",
                        (str(breaking_id), _dump(record)),
                    )

                for tag, aid in data.get("nap_tag_aliases", {}).items():
                    conn.execute(
                        "INSERT INTO nap_tag_aliases (tag, aid) VALUES (?, ?)",
                        (str(tag), int(aid)),
                    )

                academy_tags = data.get("academy_tags", {})
                for main_aid, academy_aid in data.get("academies", {}).items():
                    abbr = academy_tags.get(str(academy_aid))
                    conn.execute(
                        "INSERT INTO academies (main_aid, academy_aid, academy_abbr) VALUES (?, ?, ?)",
                        (str(main_aid), int(academy_aid), abbr),
                    )

                for aid, record in data.get("nap_exclusions", {}).items():
                    conn.execute(
                        "INSERT INTO nap_exclusions (aid, record_json) VALUES (?, ?)",
                        (str(aid), _dump(record)),
                    )


def import_from_json(json_path: str, *, db_dir: str = "db") -> list:
    """Import the pre-migration data.json into the unified tables (idempotent).

    players/alliances are UPSERTed into upstream ``users``/``alliance_list`` so
    an already-populated install merges rather than wipes; NAP state is written
    into ``verification.sqlite``. Returns a list of human-readable summary lines.
    """
    with open(json_path, "r", encoding="utf-8") as f:
        source = json.load(f)

    # Legacy layout: a bare {aid: {...}} map.
    if "alliances" not in source:
        source = {"alliances": source}

    players = source.get("players", {})
    alliances = source.get("alliances", {})
    nap_state = {section: source.get(section, {}) for section in NAP_SECTIONS}
    settings = source.get("settings", {})

    ensure_schema(db_dir)
    users_db = os.path.join(db_dir, USERS_FILE)
    alliance_db = os.path.join(db_dir, ALLIANCE_FILE)

    with _connect(users_db) as conn:
        for gid, info in players.items():
            conn.execute(
                "INSERT INTO users (fid, nickname, kid, alliance, power, discord_id, rank) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(fid) DO UPDATE SET nickname=excluded.nickname, "
                "kid=excluded.kid, alliance=excluded.alliance, power=excluded.power, "
                "discord_id=excluded.discord_id, rank=excluded.rank",
                (int(gid), info.get("nick_name"), info.get("kid"),
                 info.get("alliance_aid"), info.get("power"),
                 info.get("discord_id"), info.get("rank")),
            )
        conn.commit()

        # First=main rule: give each discord_id exactly one primary (earliest
        # fid wins). Applies to freshly imported rows and any pre-existing ones.
        for _discord_id, _earliest in conn.execute(
            "SELECT discord_id, MIN(fid) FROM users "
            "WHERE discord_id IS NOT NULL GROUP BY discord_id"
        ).fetchall():
            conn.execute("UPDATE users SET is_main = 0 WHERE discord_id = ?", (_discord_id,))
            conn.execute("UPDATE users SET is_main = 1 WHERE fid = ?", (_earliest,))
        conn.commit()

    with _connect(alliance_db) as conn:
        for aid, info in alliances.items():
            conn.execute(
                "INSERT INTO alliance_list (alliance_id, name, kid, abbr, role_id, power) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(alliance_id) DO UPDATE SET name=excluded.name, "
                "kid=excluded.kid, abbr=excluded.abbr, role_id=excluded.role_id, "
                "power=excluded.power",
                (int(aid), info.get("name"), info.get("kid"),
                 info.get("abbr"), info.get("role_id"), info.get("power")),
            )
        conn.commit()

    Storage(db_dir).save(nap_state)

    if settings:
        from .settings import SettingsStore
        store = SettingsStore(os.path.join(db_dir, SETTINGS_FILE))
        for key, value in settings.items():
            store.set(key, value)

    return [
        f"players -> users: {len(players)}",
        f"alliances -> alliance_list: {len(alliances)}",
        f"nap_breakings: {len(nap_state['nap_breakings'])}",
        f"nap_tag_aliases: {len(nap_state['nap_tag_aliases'])}",
        f"academies: {len(nap_state['academies'])}",
        f"nap_exclusions: {len(nap_state['nap_exclusions'])}",
        f"settings: {len(settings)}",
    ]


# ── Identity / ownership helpers (multi-FID with a primary) ─────────────────

def fids_for_discord(discord_id: int, *, db_dir: str = "db") -> list:
    """[(fid, is_main), ...] for a Discord user, primary first."""
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        rows = conn.execute(
            "SELECT fid, COALESCE(is_main, 0) FROM users "
            "WHERE discord_id = ? ORDER BY COALESCE(is_main, 0) DESC, fid ASC",
            (discord_id,),
        ).fetchall()
    return [(fid, bool(is_main)) for fid, is_main in rows]


def discord_for_fid(fid, *, db_dir: str = "db"):
    """Owning Discord user id for a FID, or None if unbound."""
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        row = conn.execute("SELECT discord_id FROM users WHERE fid = ?", (fid,)).fetchone()
    return row[0] if row else None


def main_fid_for_discord(discord_id: int, *, db_dir: str = "db"):
    """Primary FID for a Discord user (explicit main, else earliest)."""
    fids = fids_for_discord(discord_id, db_dir=db_dir)
    for fid, is_main in fids:
        if is_main:
            return fid
    return fids[0][0] if fids else None


def linked_main_accounts(*, db_dir: str = "db") -> list:
    """One ``(fid, discord_id, discord_server_id)`` row per linked member.

    Explicit main accounts win; legacy rows with no main flag fall back to the
    lowest FID, matching :func:`main_fid_for_discord`.
    """
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        rows = conn.execute(
            "SELECT fid, discord_id, discord_server_id, COALESCE(is_main, 0) "
            "FROM users WHERE discord_id IS NOT NULL "
            "ORDER BY discord_id, COALESCE(is_main, 0) DESC, fid ASC"
        ).fetchall()
    result = []
    seen = set()
    for fid, discord_id, server_id, _is_main in rows:
        if discord_id in seen:
            continue
        seen.add(discord_id)
        result.append((fid, discord_id, server_id))
    return result


def discord_ids_for_alliances(aids, *, db_dir: str = "db") -> list:
    """Distinct Discord user ids owning any FID in one of these alliances.

    Used to re-tag members after an alliance rename (the ``nickname`` prefix and
    the alliance role are derived from the alliance's current tag).
    """
    keys = [str(a) for a in aids if a not in (None, "")]
    if not keys:
        return []
    marks = ",".join("?" for _ in keys)
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        rows = conn.execute(
            f"SELECT DISTINCT discord_id FROM users WHERE discord_id IS NOT NULL "
            f"AND CAST(alliance AS TEXT) IN ({marks})",
            keys,
        ).fetchall()
    return [row[0] for row in rows]


def user_row(fid, *, db_dir: str = "db"):
    """Full users row for a FID (sqlite3.Row) or None."""
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM users WHERE fid = ?", (fid,)).fetchone()


def _promote_earliest_main(discord_id, conn) -> None:
    """Give a Discord user a primary if they have none (earliest fid wins)."""
    has = conn.execute(
        "SELECT 1 FROM users WHERE discord_id = ? AND COALESCE(is_main, 0) = 1",
        (discord_id,),
    ).fetchone()
    if has:
        return
    earliest = conn.execute(
        "SELECT MIN(fid) FROM users WHERE discord_id = ?", (discord_id,)
    ).fetchone()
    if earliest and earliest[0] is not None:
        conn.execute("UPDATE users SET is_main = 1 WHERE fid = ?", (earliest[0],))


def set_main_fid(discord_id: int, fid, *, db_dir: str = "db") -> bool:
    """Promote an owned FID to primary. Returns True on success."""
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        with conn:
            owner = conn.execute("SELECT discord_id FROM users WHERE fid = ?", (fid,)).fetchone()
            if not owner or owner[0] != discord_id:
                return False
            conn.execute("UPDATE users SET is_main = 0 WHERE discord_id = ?", (discord_id,))
            conn.execute("UPDATE users SET is_main = 1 WHERE fid = ?", (fid,))
    return True


def detach_fids(fids, *, db_dir: str = "db") -> set:
    """Clear Discord ownership from FIDs (keep rows + game data).

    Returns the set of affected Discord user ids (for role recompute).
    """
    affected = set()
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        with conn:
            for fid in fids:
                row = conn.execute("SELECT discord_id FROM users WHERE fid = ?", (fid,)).fetchone()
                if row and row[0] is not None:
                    affected.add(row[0])
                conn.execute(
                    "UPDATE users SET discord_id = NULL, discord_server_id = NULL, "
                    "is_main = 0 WHERE fid = ?",
                    (fid,),
                )
            for discord_id in affected:
                _promote_earliest_main(discord_id, conn)
    return affected


def transfer_fids(fids, to_discord_id: int, *, to_server_id=None, db_dir: str = "db") -> set:
    """Reassign ownership of FIDs to another Discord user (keep rows + game data).

    Returns the set of previous-owner Discord ids (for role recompute).
    """
    old_owners = set()
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        with conn:
            target_had_main = bool(conn.execute(
                "SELECT 1 FROM users WHERE discord_id = ? AND COALESCE(is_main, 0) = 1",
                (to_discord_id,),
            ).fetchone())
            for fid in fids:
                row = conn.execute("SELECT discord_id FROM users WHERE fid = ?", (fid,)).fetchone()
                if row and row[0] is not None:
                    old_owners.add(row[0])
                conn.execute(
                    "UPDATE users SET discord_id = ?, discord_server_id = ?, is_main = 0 "
                    "WHERE fid = ?",
                    (to_discord_id, to_server_id, fid),
                )
            for discord_id in old_owners:
                _promote_earliest_main(discord_id, conn)
            if not target_had_main:
                _promote_earliest_main(to_discord_id, conn)
    return old_owners


def upsert_alliance(aid, *, abbr=None, name=None, kid=None, role_id=None, power=None,
                    db_dir: str = "db") -> None:
    """Insert/update an alliance_list row with the extension's columns."""
    ensure_schema(db_dir)
    with _connect(os.path.join(db_dir, ALLIANCE_FILE)) as conn:
        with conn:
            conn.execute(
                "INSERT INTO alliance_list (alliance_id, name, kid, abbr, role_id, power) "
                "VALUES (?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(alliance_id) DO UPDATE SET "
                "name=COALESCE(excluded.name, alliance_list.name), "
                "kid=COALESCE(excluded.kid, alliance_list.kid), "
                "abbr=COALESCE(excluded.abbr, alliance_list.abbr), "
                "role_id=COALESCE(excluded.role_id, alliance_list.role_id), "
                "power=COALESCE(excluded.power, alliance_list.power)",
                (aid, name, kid, abbr, role_id, power),
            )


def register_player(*, fid, discord_id, discord_server_id=None, nickname=None, kid=None,
                    alliance_aid=None, rank=None, power=None, abbr=None, alliance_name=None,
                    db_dir: str = "db") -> None:
    """Upsert a user row (and its alliance), promoting to main if first.

    Re-registering a fid already owned by the same user keeps its existing main
    flag (no accidental demotion).
    """
    ensure_schema(db_dir)
    with _connect(os.path.join(db_dir, USERS_FILE)) as conn:
        with conn:
            existing = conn.execute(
                "SELECT discord_id, COALESCE(is_main, 0) FROM users WHERE fid = ?", (fid,)
            ).fetchone()
            if existing and existing[0] == discord_id:
                is_main = existing[1]
            else:
                has_main = conn.execute(
                    "SELECT 1 FROM users WHERE discord_id = ? AND COALESCE(is_main, 0) = 1",
                    (discord_id,),
                ).fetchone()
                is_main = 0 if has_main else 1

            conn.execute(
                "INSERT INTO users (fid, discord_id, discord_server_id, nickname, kid, "
                "alliance, power, rank, is_main) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(fid) DO UPDATE SET discord_id=excluded.discord_id, "
                "discord_server_id=excluded.discord_server_id, nickname=excluded.nickname, "
                "kid=excluded.kid, alliance=excluded.alliance, power=excluded.power, "
                "rank=excluded.rank, is_main=excluded.is_main",
                (fid, discord_id, discord_server_id, nickname, kid, alliance_aid,
                 power, rank, is_main),
            )

    if alliance_aid is not None:
        upsert_alliance(alliance_aid, abbr=abbr, name=alliance_name, kid=kid, power=power,
                        db_dir=db_dir)


def alliance_row(aid, *, db_dir: str = "db"):
    """Full alliance_list row for an aid (sqlite3.Row) or None."""
    with _connect(os.path.join(db_dir, ALLIANCE_FILE)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute(
            "SELECT * FROM alliance_list WHERE alliance_id = ?", (aid,)
        ).fetchone()


def top_alliances_by_power(n: int, *, db_dir: str = "db") -> list:
    """Alliance ids ordered by power desc, top n (for the council tier)."""
    with _connect(os.path.join(db_dir, ALLIANCE_FILE)) as conn:
        rows = conn.execute(
            "SELECT alliance_id FROM alliance_list "
            "WHERE power IS NOT NULL ORDER BY power DESC, alliance_id ASC LIMIT ?",
            (max(0, int(n)),),
        ).fetchall()
    return [r[0] for r in rows]


def all_alliance_role_ids(*, db_dir: str = "db") -> list:
    """Every per-alliance Discord role id the extension has created."""
    with _connect(os.path.join(db_dir, ALLIANCE_FILE)) as conn:
        return [r[0] for r in conn.execute(
            "SELECT role_id FROM alliance_list WHERE role_id IS NOT NULL"
        ).fetchall()]


def all_alliances(*, db_dir: str = "db") -> list:
    """All alliance_list rows as sqlite3.Row objects."""
    with _connect(os.path.join(db_dir, ALLIANCE_FILE)) as conn:
        conn.row_factory = sqlite3.Row
        return conn.execute("SELECT * FROM alliance_list").fetchall()


def record_nap_ranking(ranked, *, posted_at=None, db_dir="db"):
    """Persist a posted NAP ranking snapshot (one row per ranked alliance)."""
    if not ranked:
        return
    if posted_at is None:
        posted_at = datetime.now(timezone.utc).isoformat()
    ensure_schema(db_dir)
    with _connect(os.path.join(db_dir, VERIFICATION_FILE)) as conn:
        with conn:
            for row in ranked:
                conn.execute(
                    "INSERT INTO nap_ranking_history (aid, abbr, name, power, posted_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (row.get("aid"), row.get("abbr"), row.get("name"), row.get("power"), posted_at),
                )


def nap_lookback_protected(days, *, db_dir="db"):
    """Merged lookback entries (aid, abbr, name, power, last_seen_utc) within `days`."""
    if days <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    ensure_schema(db_dir)
    with _connect(os.path.join(db_dir, VERIFICATION_FILE)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT aid, abbr, name, power, posted_at FROM nap_ranking_history "
            "WHERE posted_at >= ? ORDER BY posted_at ASC, id ASC",
            (cutoff,),
        ).fetchall()
    merged = {}
    for row in rows:
        entry = merged.get(row["aid"])
        if entry is None:
            merged[row["aid"]] = {
                "aid": row["aid"], "abbr": row["abbr"], "name": row["name"],
                "power": row["power"], "last_seen_utc": datetime.fromisoformat(row["posted_at"]),
            }
        else:
            entry["power"] = max(entry["power"], row["power"] or 0)
            entry["abbr"] = row["abbr"] or entry["abbr"]
            entry["name"] = row["name"] or entry["name"]
            seen = datetime.fromisoformat(row["posted_at"])
            if seen > entry["last_seen_utc"]:
                entry["last_seen_utc"] = seen
    return sorted(merged.values(), key=lambda e: e["power"], reverse=True)
