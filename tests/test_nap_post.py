"""Tests for the NAP ranking post path: shared maintenance lock + snapshot record.

``post_nap_ranking`` and the nightly maintenance share ``bot.maintenance_lock``
so the daily post always reads a completed alliance/ranking snapshot; the post
also persists the posted ranking so the DB-backed lookback can protect it later.
These tests pin that behaviour down without touching the real ``db/`` directory.
"""
import asyncio
import types

import discord

from extension import nap as nap_mod
from extension import storage
from extension.cogs import nap as nap_cog
from extension.tasks import nightly as nightly_mod


class _FakeSettings:
    """Duck-typed subset of extension.settings.SettingsStore."""

    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def get_int(self, key, default=0):
        try:
            return int(self._values.get(key, default))
        except (TypeError, ValueError):
            return default

    def get_json(self, key, default=None):
        return self._values.get(key, default)


class _FakeChannel:
    def __init__(self, error=None):
        self.messages = []
        self.error = error

    async def send(self, content):
        if self.error is not None:
            raise self.error
        self.messages.append(content)


class _FakeBot:
    """Minimal bot surface used by ``post_nap_ranking``."""

    def __init__(self, channel, settings):
        self.maintenance_lock = asyncio.Lock()
        self.extension_settings = _FakeSettings(settings)
        self._channel = channel

    def get_channel(self, channel_id):
        return self._channel

    async def fetch_channel(self, channel_id):
        return self._channel


class _StorageShim:
    """Redirects the two DB touchpoints of ``post_nap_ranking`` into tmp_path."""

    def __init__(self, db_dir):
        self._db_dir = db_dir

    def nap_lookback_protected(self, days):
        return storage.nap_lookback_protected(days, db_dir=self._db_dir)

    def record_nap_ranking(self, ranked, *, posted_at=None):
        return storage.record_nap_ranking(ranked, posted_at=posted_at, db_dir=self._db_dir)


def _logic():
    return nap_mod.NapLogic(
        nap_alliances_count=2,
        alliances={
            "10": {"abbr": "A10", "name": "N10", "power": 300},
            "11": {"abbr": "A11", "name": "N11", "power": 200},
        },
    )


def _prepare(monkeypatch, tmp_path, *, channel, settings=None):
    values = {"nap_channel_id": 123, "nap_snapshot_lookback_protected": 0}
    values.update(settings or {})
    bot = _FakeBot(channel, values)
    monkeypatch.setattr(nap_cog, "_nap_context", lambda _bot: (_logic(), {}))
    monkeypatch.setattr(nap_cog, "storage", _StorageShim(str(tmp_path)))
    return bot


def test_post_ranking_records_snapshot(tmp_path, monkeypatch):
    channel = _FakeChannel()
    bot = _prepare(monkeypatch, tmp_path, channel=channel)

    assert asyncio.run(nap_cog.post_nap_ranking(bot)) is True
    assert len(channel.messages) == 1
    assert "1. [A10] N10 - 300" in channel.messages[0]
    # The posted snapshot is persisted, so a later lookback can protect it.
    assert [e["aid"] for e in storage.nap_lookback_protected(7, db_dir=str(tmp_path))] == [10, 11]


def test_post_ranking_waits_for_maintenance_lock(tmp_path, monkeypatch):
    channel = _FakeChannel()
    bot = _prepare(monkeypatch, tmp_path, channel=channel)

    async def scenario():
        await bot.maintenance_lock.acquire()
        task = asyncio.create_task(nap_cog.post_nap_ranking(bot))
        await asyncio.sleep(0)  # let the task start and block on the lock
        await asyncio.sleep(0)
        posted_while_locked = list(channel.messages)
        bot.maintenance_lock.release()
        return posted_while_locked, await asyncio.wait_for(task, timeout=5)

    posted_while_locked, result = asyncio.run(scenario())
    assert posted_while_locked == []
    assert result is True
    assert len(channel.messages) == 1


def test_post_ranking_without_channel_is_noop(tmp_path, monkeypatch):
    channel = _FakeChannel()
    bot = _prepare(monkeypatch, tmp_path, channel=channel, settings={"nap_channel_id": 0})

    assert asyncio.run(nap_cog.post_nap_ranking(bot)) is False
    assert channel.messages == []
    assert storage.nap_lookback_protected(7, db_dir=str(tmp_path)) == []


def test_post_ranking_failure_does_not_record_snapshot(tmp_path, monkeypatch):
    error = discord.Forbidden(types.SimpleNamespace(status=403, reason="Forbidden"), "nope")
    channel = _FakeChannel(error=error)
    bot = _prepare(monkeypatch, tmp_path, channel=channel)

    assert asyncio.run(nap_cog.post_nap_ranking(bot)) is False
    assert storage.nap_lookback_protected(7, db_dir=str(tmp_path)) == []


def test_nightly_maintenance_waits_for_maintenance_lock(monkeypatch):
    bot = types.SimpleNamespace(maintenance_lock=asyncio.Lock())
    cog = nightly_mod.NightlyTasks(bot)

    async def _no_discovery():
        return 0

    monkeypatch.setattr(cog, "_discover_candidates", _no_discovery)
    monkeypatch.setattr(nightly_mod.storage, "all_alliances", lambda: [])
    monkeypatch.setattr(nightly_mod.storage, "linked_main_accounts", lambda: [])

    async def scenario():
        await bot.maintenance_lock.acquire()
        task = asyncio.create_task(cog._run_nightly_maintenance(triggered_by="test"))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        blocked_while_locked = not task.done()
        bot.maintenance_lock.release()
        await asyncio.wait_for(task, timeout=5)
        return blocked_while_locked

    assert asyncio.run(scenario()) is True


def test_nap_post_can_run_while_player_sync_is_still_running(tmp_path, monkeypatch):
    """00:05 posting waits for alliances, not the slower player-name refresh."""
    channel = _FakeChannel()
    bot = _prepare(monkeypatch, tmp_path, channel=channel)
    bot.guilds = []
    cog = nightly_mod.NightlyTasks(bot)
    player_sync_started = asyncio.Event()
    release_player_sync = asyncio.Event()

    async def _no_discovery():
        return 0

    async def _slow_player_sync():
        player_sync_started.set()
        await release_player_sync.wait()
        return {"synced": 0, "skipped": 0}

    monkeypatch.setattr(cog, "_discover_candidates", _no_discovery)
    monkeypatch.setattr(cog, "_sync_alliance_discord_state", lambda: asyncio.sleep(0, result=0))
    monkeypatch.setattr(cog, "_sync_linked_players", _slow_player_sync)
    monkeypatch.setattr(nightly_mod.storage, "all_alliances", lambda: [])

    async def scenario():
        maintenance = asyncio.create_task(cog._run_nightly_maintenance(triggered_by="test"))
        await asyncio.wait_for(player_sync_started.wait(), timeout=5)
        posted = await asyncio.wait_for(nap_cog.post_nap_ranking(bot), timeout=5)
        release_player_sync.set()
        await asyncio.wait_for(maintenance, timeout=5)
        return posted

    assert asyncio.run(scenario()) is True
    assert len(channel.messages) == 1
