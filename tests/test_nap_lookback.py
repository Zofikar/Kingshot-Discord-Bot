"""Tests for the DB-backed NAP lookback (record + protect) + protection helpers."""
from datetime import datetime, timedelta, timezone

from extension import nap as nap_mod
from extension import storage


def _logic(**kw):
    return nap_mod.NapLogic(**kw)


def test_record_and_lookback_within_window(tmp_path):
    db = str(tmp_path)
    ranked = [
        {"aid": 10, "abbr": "A10", "name": "N10", "power": 250},
        {"aid": 11, "abbr": "A11", "name": "N11", "power": 240},
    ]
    storage.record_nap_ranking(ranked, posted_at=datetime.now(timezone.utc).isoformat(), db_dir=db)
    entries = storage.nap_lookback_protected(7, db_dir=db)
    assert {e["aid"] for e in entries} == {10, 11}
    assert entries[0]["aid"] == 10  # sorted by power desc


def test_lookback_excludes_old_snapshots(tmp_path):
    db = str(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    storage.record_nap_ranking([{"aid": 10, "abbr": "A10", "name": "N10", "power": 250}],
                               posted_at=old, db_dir=db)
    assert storage.nap_lookback_protected(7, db_dir=db) == []


def test_lookback_merges_max_power_and_latest(tmp_path):
    db = str(tmp_path)
    t1 = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
    t2 = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    storage.record_nap_ranking([{"aid": 10, "abbr": "OLD", "name": "N10", "power": 100}], posted_at=t1, db_dir=db)
    storage.record_nap_ranking([{"aid": 10, "abbr": "NEW", "name": "N10", "power": 200}], posted_at=t2, db_dir=db)
    entries = storage.nap_lookback_protected(7, db_dir=db)
    assert len(entries) == 1
    assert entries[0]["power"] == 200
    assert entries[0]["abbr"] == "NEW"


def test_lookback_disabled_when_zero_days(tmp_path):
    db = str(tmp_path)
    storage.record_nap_ranking([{"aid": 10, "abbr": "A10", "name": "N10", "power": 250}], db_dir=db)
    assert storage.nap_lookback_protected(0, db_dir=db) == []


def test_protection_snapshot_and_lookback():
    logic = _logic(alliances={"10": {"abbr": "A10", "name": "N10", "power": 250}})
    lookback = []
    protected, status = nap_mod.nap_protection_for_alliance(logic, lookback, 7, aid=10)
    assert protected and "snapshot" in status


def test_protection_lookback_for_fallen_alliance():
    logic = _logic(alliances={})  # fallen: no longer in current data
    now = datetime.now(timezone.utc)
    lookback = [{"aid": 20, "abbr": "A20", "name": "N20", "power": 50, "last_seen_utc": now - timedelta(days=1)}]
    protected, status = nap_mod.nap_protection_for_alliance(logic, lookback, 7, aid=20)
    assert protected and "lookback" in status


def test_protection_excluded_denies():
    logic = _logic(alliances={"10": {"abbr": "A10", "name": "N10", "power": 250}},
                   nap_exclusions={"10": {"reason": "broke", "added_utc": "", "added_by": "x", "tag": "A10"}})
    protected, status = nap_mod.nap_protection_for_alliance(logic, [], 7, aid=10)
    assert not protected and "excluded" in status
