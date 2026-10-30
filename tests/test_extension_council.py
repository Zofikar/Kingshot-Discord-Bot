"""Council mandate and electorate specifications."""
import pytest

from extension import storage


def test_latest_snapshot_is_not_lookback_union(tmp_path):
    db = str(tmp_path)
    storage.record_nap_ranking([{"aid": 1, "abbr": "OLD"}],
                               posted_at="2026-10-29T00:00:00+00:00", db_dir=db)
    storage.record_nap_ranking([{"aid": 2, "abbr": "NEW"}],
                               posted_at="2026-10-30T00:00:00+00:00", db_dir=db)
    assert storage.latest_nap_snapshot(db_dir=db) == [{"aid": 2, "abbr": "NEW"}]


def test_mandates_reject_cross_assignment_preserve_moves_and_yield_to_new_leadership(tmp_path):
    from extension.council import MandateStore
    store = MandateStore(str(tmp_path / "council.sqlite"))
    # Empty seats default to present leaders; an absent leader leaves a vacancy.
    store.reconcile(1, {7, 8}, {42, 43}, {7: 42})
    assert store.assignments(1) == {7: 42}
    store.assign(1, 7, 43, 42, "leader")
    store.reconcile(1, {7, 8}, {42, 43}, {7: 42})
    assert store.assignments(1) == {7: 43}  # no automatic handback
    with pytest.raises(ValueError, match="another alliance"):
        store.assign(1, 8, 43, 99, "admin")
    # Becoming B's leader overrides A's retained mandate and defaults A to R5.
    store.reconcile(1, {7, 8}, {42, 43}, {7: 42, 8: 43})
    assert store.assignments(1) == {7: 42, 8: 43}
    # Council departure suspends a mandate rather than destroying it.
    store.reconcile(1, {8}, {42, 43}, {7: 42, 8: 43})
    assert store.active(1) == {8: 43}
    assert store.assignments(1) == {7: 42, 8: 43}
    store.reconcile(1, {7, 8}, {42}, {7: 42})
    assert store.assignments(1) == {7: 42}  # departed member loses mandate


def test_relinquish_releases_unique_person_constraint(tmp_path):
    from extension.council import MandateStore
    store = MandateStore(str(tmp_path / "council.sqlite"))
    store.assign(1, 7, 42, 99, "admin")
    store.relinquish(1, 42)
    store.assign(1, 8, 42, 99, "admin")
    assert store.assignments(1) == {8: 42}
    assert len(store.history(1)) == 3