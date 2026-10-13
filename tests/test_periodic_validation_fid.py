"""Periodic validation must not run every code against an ID with no state.

A test ID saved without a state made every periodic check return NO_STATE, so
codes stayed pending and never auto-redeemed.
"""
import asyncio
import importlib
import sqlite3
from types import SimpleNamespace

gr = importlib.import_module("cogs.gift_redemption")
gs = importlib.import_module("cogs.gift_settings")


def _cog(primary):
    async def get_validation_fid():
        return primary, "test_fid"
    logs = []
    logger = SimpleNamespace(warning=logs.append, info=logs.append)
    return SimpleNamespace(get_validation_fid=get_validation_fid, logger=logger), logs


def _patch(monkeypatch, kids, members):
    async def get_user_kid(cog, fid):
        return kids.get(str(fid))

    async def get_alt_validation_fids(cog, exclude, limit=3):
        return [m for m in members if m not in {str(e) for e in exclude}][:limit]

    monkeypatch.setattr(gr, "get_user_kid", get_user_kid)
    monkeypatch.setattr(gr, "get_alt_validation_fids", get_alt_validation_fids)


def test_id_with_state_is_kept(monkeypatch):
    _patch(monkeypatch, {"111": 2704}, ["222"])
    cog, _ = _cog("111")
    assert asyncio.run(gr.resolve_periodic_validation_fid(cog)) == ("111", "test_fid")


def test_stateless_id_falls_back_to_member_with_state(monkeypatch):
    _patch(monkeypatch, {"222": 2704}, ["222"])
    cog, logs = _cog("111")
    assert asyncio.run(gr.resolve_periodic_validation_fid(cog)) == ("222", "alliance_member")
    assert any("no state on file" in line for line in logs)


def test_no_id_with_state_skips_the_run(monkeypatch):
    _patch(monkeypatch, {}, [])
    cog, _ = _cog("111")
    fid, _ = asyncio.run(gr.resolve_periodic_validation_fid(cog))
    assert fid is None


def _settings_cog(saved_fid):
    conn = sqlite3.connect(":memory:")
    gs.ensure_test_fid_settings(conn)
    conn.execute("DELETE FROM test_fid_settings")
    conn.execute("INSERT INTO test_fid_settings (test_fid, kid) VALUES (?, 259)", (saved_fid,))
    logger = SimpleNamespace(info=lambda *a: None, exception=lambda *a: None)
    return SimpleNamespace(settings_cursor=conn.cursor(), logger=logger)


def test_configured_test_id_is_used_without_a_player_lookup():
    cog = _settings_cog("12345678")
    assert asyncio.run(gs.get_validation_fid(cog)) == ("12345678", "test_fid")
    assert not hasattr(gs, "verify_test_fid")
