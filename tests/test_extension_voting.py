"""Executable council voting and queue policy specifications."""
import pytest

from extension.voting import QueueEntry, normalize_queue, tally


def test_persistent_vote_deadline_manual_adjustment_and_finalization(tmp_path):
    from extension.vote_storage import VoteStore
    store = VoteStore(str(tmp_path / "votes.sqlite"))
    vote_id = store.submit(1, "Agreement?", 7, 100, 42, now=1000)
    assert store.queue(1)[0]["status"] == "DEFINED"
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}, {"aid": 8, "abbr": "BBC"}], now=2000)
    store.cast(vote_id, 7, "YES", 100, 42, now=2001)
    # The electorate is durable, including vacant seats.
    store = VoteStore(str(tmp_path / "votes.sqlite"))
    assert store.participation(vote_id)["DID_NOT_VOTE"] == ["BBC"]
    with pytest.raises(ValueError, match="closed"):
        store.cast(vote_id, 8, "NO", 200, 43, now=2000 + 86400)
    assert store.get(vote_id)["status"] == "AWAITS_APPROVAL"
    store.cast(vote_id, 8, "ABSTAINED", 200, 99, now=90000,
               manual=True, reason="Confirmed in game")
    assert store.conclude(vote_id, 99, now=90001) == "PASSED"
    assert len(store.audit(vote_id)) == 2
    assert store.audit(vote_id)[1]["fid"] == 200
    with pytest.raises(ValueError, match="closed"):
        store.cast(vote_id, 7, "NO", 100, 99, now=90002,
                   manual=True, reason="Too late")


def test_queue_penalty_removal_and_pending_guard(tmp_path):
    from extension.vote_storage import VoteStore
    store = VoteStore(str(tmp_path / "votes.sqlite"))
    first = store.submit(1, "First", 7, 100, 42, now=1)
    second = store.submit(1, "Second", 7, 100, 42, now=2)
    assert [v["priority"] for v in store.queue(1)] == [0, -1]
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=3)
    with pytest.raises(ValueError, match="pending"):
        store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=4)
    with pytest.raises(ValueError, match="DEFINED"):
        store.remove(first, 42, "", now=4)
    store.remove(second, 42, "Withdrawn", now=4)
    assert store.queue(1) == []
    assert store.get(second)["status"] == "REMOVED"


def test_ballot_validation_and_early_conclusion(tmp_path):
    from extension.vote_storage import VoteStore
    store = VoteStore(str(tmp_path / "votes.sqlite"))
    vote_id = store.submit(1, "Question", 7, 100, 42, now=1)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}], now=2)
    with pytest.raises(ValueError, match="majority"):
        store.conclude(vote_id, 99, now=3)
    with pytest.raises(ValueError, match="eligible"):
        store.cast(vote_id, 8, "YES", 100, 42, now=3)
    with pytest.raises(ValueError, match="reason"):
        store.cast(vote_id, 7, "YES", 100, 99, now=3, manual=True)
    store.cast(vote_id, 7, "NO", 100, 42, now=3)
    store.cast(vote_id, 7, "YES", 100, 42, now=4)
    assert store.participation(vote_id)["YES"] == ["BBQ"]
    assert store.participation(vote_id)["NO"] == []
    assert len(store.audit(vote_id)) == 2
    assert store.conclude(vote_id, 99, now=5) == "PASSED"


def test_urgency_restore_and_durable_delivery_metadata(tmp_path):
    from extension.vote_storage import VoteStore
    store = VoteStore(str(tmp_path / "votes.sqlite"))
    first = store.submit(1, "First", 7, 100, 42)
    second = store.submit(1, "Second", 8, 200, 43)
    with pytest.raises(ValueError, match="reason"):
        store.set_urgent(second, True, 99, "")
    store.set_urgent(second, True, 99, "Emergency")
    assert [v["id"] for v in store.queue(1)] == [second, first]
    store.set_urgent(second, False, 99, "Resolved")
    assert [v["id"] for v in store.queue(1)] == [first, second]
    store.set_identity(first, "BBQ", "Alice", 42)
    store.open_next(1, [{"aid": 7, "abbr": "BBQ"}])
    store.update_delivery(first, channel_id=123, message_id=456)
    assert store.pending(1)["message_id"] == 456
    assert store.get(first)["submitter_name"] == "Alice"


@pytest.mark.parametrize("eligible,yes,no,abstained,status,early", [
    (20, 4, 4, 0, "VOIDED", False),
    (20, 5, 4, 0, "PASSED", False),
    (20, 4, 4, 1, "FAILED", False),
    (20, 1, 0, 8, "PASSED", False),
    (20, 10, 0, 0, "PASSED", False),
    (20, 11, 0, 0, "PASSED", True),
    (1, 0, 0, 1, "FAILED", False),
])
def test_tally_boundaries(eligible, yes, no, abstained, status, early):
    result = tally(eligible, yes, no, abstained)
    assert result.status == status
    assert result.early_conclusion is early
    assert result.did_not_vote == eligible - yes - no - abstained


@pytest.mark.parametrize("counts", [(0, 0, 0, 0), (2, 2, 1, 0), (2, -1, 0, 0)])
def test_tally_rejects_invalid_counts(counts):
    with pytest.raises(ValueError):
        tally(*counts)


def test_queue_repeats_passes_and_increments_displaced_votes():
    entries = [QueueEntry(1, -2), QueueEntry(2, -1), QueueEntry(3, 0)]
    assert [(v.vote_id, v.priority) for v in normalize_queue(entries)] == [
        (3, 0), (2, 0), (1, 0),
    ]
    # Normalization must not mutate caller-owned objects before persistence.
    assert [v.priority for v in entries] == [-2, -1, 0]


def test_equal_priorities_preserve_current_order():
    entries = [QueueEntry(3, 0), QueueEntry(1, 0), QueueEntry(2, 0)]
    assert normalize_queue(entries) == entries


def test_urgent_votes_follow_submission_order_without_affecting_base_priority():
    entries = [QueueEntry(1, -2), QueueEntry(3, -5, urgent=True),
               QueueEntry(2, -3, urgent=True), QueueEntry(4, 0)]
    result = normalize_queue(entries)
    assert [(v.vote_id, v.priority) for v in result] == [
        (2, -3), (3, -5), (4, 0), (1, -1),
    ]