"""Persistent blacklist records, separate from wipe-and-rewrite NAP state."""
import json
import os
from datetime import datetime, timezone

from . import storage


def records():
    store = storage.Storage()
    store.init_schema()
    with store.connect() as conn:
        return [json.loads(row[0]) for row in conn.execute(
            "SELECT record_json FROM player_blacklist ORDER BY fid")]


def save(record):
    store = storage.Storage()
    store.init_schema()
    with store.connect() as conn:
        conn.execute("INSERT OR REPLACE INTO player_blacklist VALUES (?, ?)",
                     (record["fid"], json.dumps(record, ensure_ascii=False)))


def remove(fid):
    store = storage.Storage()
    store.init_schema()
    with store.connect() as conn:
        return conn.execute("DELETE FROM player_blacklist WHERE fid = ?", (str(fid),)).rowcount > 0


def active(record):
    return not record.get("ends_at") or datetime.fromisoformat(record["ends_at"]) > datetime.now(timezone.utc)


def parse_end(value):
    if not value:
        return None
    end = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    if end <= datetime.now(timezone.utc):
        raise ValueError("End time must be in the future.")
    return end.astimezone(timezone.utc).isoformat()


def find_fids(kid, name):
    # Python casefold handles non-ASCII names unlike SQLite's built-in NOCASE.
    with storage._connect(os.path.join("db", storage.USERS_FILE)) as conn:
        return [str(fid) for fid, nickname in conn.execute(
            "SELECT fid, nickname FROM users WHERE kid = ?", (kid,))
                if str(nickname or "").strip().casefold() == name.strip().casefold()]