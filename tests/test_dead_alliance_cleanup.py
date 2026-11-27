"""Dead-alliance retention and nightly orchestration, using isolated databases."""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from extension import storage
from extension.tasks import nightly


def test_cleanup_preserves_every_protection_and_player_records(tmp_path):
    db = str(tmp_path)
    storage.ensure_schema(db)
    start = datetime(2026, 11, 27, tzinfo=timezone.utc)
    with storage._connect(os.path.join(db, storage.ALLIANCE_FILE)) as conn:
        conn.executemany("INSERT INTO alliance_list (alliance_id) VALUES (?)", [(i,) for i in range(1, 10)])
    with storage._connect(os.path.join(db, storage.VERIFICATION_FILE)) as conn:
        conn.execute("INSERT INTO academies VALUES ('2', 3, 'AC')")
        conn.execute("INSERT INTO nap_exclusions VALUES ('4', '{}')")
        conn.execute("INSERT INTO player_blacklist VALUES ('100', ?)",
                     (json.dumps({'fid': '100', 'player': {'alliance': {'aid': 5}}}),))
        conn.execute("INSERT INTO player_blacklist VALUES ('101', ?)", (json.dumps({'fid': '101'}),))
    with storage._connect(os.path.join(db, storage.USERS_FILE)) as conn:
        conn.executemany("INSERT INTO users (fid, alliance, discord_id, is_main) VALUES (?, ?, ?, ?)",
                         [(101, '6', None, 0), (102, '7', 999, 0), (103, '8', None, 0)])
    assert storage.cleanup_dead_alliances([1], db_dir=db, now=start) == []
    assert storage.cleanup_dead_alliances([1], db_dir=db, now=start + timedelta(days=3)) == []
    assert storage.cleanup_dead_alliances([1], db_dir=db, now=start + timedelta(days=3, seconds=1)) == [8, 9]
    with storage._connect(os.path.join(db, storage.USERS_FILE)) as conn:
        assert conn.execute("SELECT alliance FROM users WHERE fid = 103").fetchone() == ('8',)


def test_candidate_reentry_resets_retention_and_empty_board_does_not_delete(tmp_path):
    db = str(tmp_path)
    storage.ensure_schema(db)
    start = datetime(2026, 11, 27, tzinfo=timezone.utc)
    with storage._connect(os.path.join(db, storage.ALLIANCE_FILE)) as conn:
        conn.executemany("INSERT INTO alliance_list (alliance_id) VALUES (?)", [(1,), (2,)])
    assert storage.cleanup_dead_alliances([1, 2], db_dir=db, now=start) == []
    assert storage.cleanup_dead_alliances([2], db_dir=db, now=start + timedelta(days=2)) == []
    assert storage.cleanup_dead_alliances([], db_dir=db, now=start + timedelta(days=10)) == []
    assert storage.cleanup_dead_alliances([1], db_dir=db, now=start + timedelta(days=4)) == []
    assert storage.cleanup_dead_alliances([1], db_dir=db, now=start + timedelta(days=6)) == [2]


@pytest.mark.parametrize('discovered', [0, 1])
def test_nightly_cleanup_runs_only_after_successful_discovery(monkeypatch, discovered):
    bot = SimpleNamespace(maintenance_lock=asyncio.Lock())
    cog = nightly.NightlyTasks(bot)
    events = []

    async def discover():
        cog._discovered_power = {'42': 100} if discovered else {}
        events.append('discover')
        return discovered

    def cleanup(aids):
        assert aids == {'42': 100}
        events.append('cleanup')
        return [99]

    monkeypatch.setattr(cog, '_discover_candidates', discover)
    monkeypatch.setattr(cog, '_sync_alliance_discord_state', AsyncMock(return_value=0))
    monkeypatch.setattr(cog, '_sync_linked_players', AsyncMock(return_value={'synced': 0, 'skipped': 0}))
    monkeypatch.setattr(nightly.storage, 'all_alliances', lambda: [])
    monkeypatch.setattr(nightly.storage, 'cleanup_dead_alliances', cleanup)
    asyncio.run(cog._run_nightly_maintenance())
    assert events == (['discover', 'cleanup'] if discovered else ['discover'])