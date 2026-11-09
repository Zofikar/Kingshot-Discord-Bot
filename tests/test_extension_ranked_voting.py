"""Ranked policy, transactional lifecycle and exhaustive small elections."""
import itertools
import json
import sqlite3

import pytest

from extension.voting import ranked_tally, parse_ranking, validate_options
from extension.vote_storage import VoteStore


def test_nap_transfers():
    ballots = [[1, 2, 3]] * 3 + [[2, 1, 3]] * 2 + [[3, 2, 1]] * 4
    result = ranked_tally(9, 3, ballots, 0)
    assert result.status == "PASSED" and result.winner == 1
    assert result.rounds[0]["counts"] == {1: 3, 2: 2, 3: 4}
    assert result.rounds[0]["eliminated"] == 2
    assert result.rounds[1]["counts"] == {1: 5, 3: 4}


@pytest.mark.parametrize("n,ballots,a,status", [
    (20, [[1, 2]] * 8, 0, "VOIDED"),
    (20, [[1, 2]] * 8, 1, "PASSED"),
    (20, [], 9, "FAILED"),
    (20, [], 8, "VOIDED"),
    (2, [[1, 2], [2, 1]], 0, "TIED"),
])
def test_quorum_and_abstentions(n, ballots, a, status):
    result = ranked_tally(n, 2, ballots, a)
    assert result.status == status
    assert result.did_not_vote == n - len(ballots) - a


def test_partial_completion_preserves_explicit_choices():
    result = ranked_tally(3, 5, [[3, 1, 2], [5, 4, 3, 2, 1], [5, 4, 1, 2, 3]], 0)
    assert result.completed[0] == (3, 1, 2, 5, 4)


def test_completion_fallback_and_single_voter():
    result = ranked_tally(1, 5, [[3]], 0)
    assert result.completed == ((3, 1, 2, 4, 5),)
    assert result.winner == 3


def test_equal_averages_do_not_arbitrarily_eliminate():
    result = ranked_tally(3, 3, [[1, 2, 3], [2, 3, 1], [3, 1, 2]], 0)
    assert result.status == "TIED"
    assert result.tied == (1, 2, 3)


def test_final_count_tie_uses_average_completed_rank():
    result = ranked_tally(4, 3, [[1, 2, 3]] * 2 + [[2, 3, 1]] * 2, 0)
    assert result.winner == 2
    assert result.rounds[-1]["counts"] == {1: 2, 2: 2}
    assert result.rounds[-1]["tiebreak"]


def test_elimination_tie_removes_worse_average():
    result = ranked_tally(4, 3, [[1, 2, 3]] * 2 + [[2, 1, 3], [3, 1, 2]], 0)
    assert result.rounds[0]["eliminated"] == 3
    assert result.rounds[0]["tiebreak"]
    assert result.winner == 1


def test_tied_finalization_releases_pending_slot(tmp_path):
    store = VoteStore(str(tmp_path / "tie.sqlite"))
    vote = store.submit(1, "Tie", 1, 100, 10, vote_type="RANKED", options=["A", "B"], now=1)
    store.open_next(1, [{"aid": 1, "abbr": "A"}, {"aid": 2, "abbr": "B"}], now=2)
    store.cast(vote, 1, [1, 2], 100, 10, now=3)
    store.cast(vote, 2, [2, 1], 200, 20, now=3)
    assert store.conclude(vote, 99, now=86402) == "TIED"
    assert store.pending(1) is None
    assert json.loads(store.get(vote)["result"])["tied"] == [1, 2]


@pytest.mark.parametrize("count,expected", [(5, False), (6, True)])
def test_early_majority_uses_entire_electorate(count, expected):
    assert ranked_tally(10, 2, [[1]] * count, 0).early_conclusion is expected


@pytest.mark.parametrize("text", ["", "1,1", "0", "4", "one", "1,", "1.0", "-1"])
def test_invalid_ranking_text(text):
    with pytest.raises(ValueError):
        parse_ranking(text, 3)


def test_parse_ranking():
    assert parse_ranking("3 > 1 > 2", 3) == [3, 1, 2]


@pytest.mark.parametrize("options", [[], ["one"], ["a", " A "], ["a", ""], ["a", "x" * 81], [str(i) for i in range(21)]])
def test_invalid_options(options):
    with pytest.raises(ValueError):
        validate_options(options)


@pytest.mark.parametrize("ranking", [[], [True], [1, 1], [0], [4], "1,2", [1.0]])
def test_invalid_policy_ballots(ranking):
    with pytest.raises(ValueError):
        ranked_tally(1, 3, [ranking], 0)


def test_exhaustive_three_option_elections():
    permutations = list(itertools.permutations([1, 2, 3]))
    for ballots in itertools.product(permutations, repeat=4):
        result = ranked_tally(4, 3, ballots, 0)
        assert result.status in ("PASSED", "TIED")
        assert result == ranked_tally(4, 3, ballots, 0)
        reversed_result = ranked_tally(4, 3, list(reversed(ballots)), 0)
        assert (result.status, result.winner, result.rounds) == (
            reversed_result.status, reversed_result.winner, reversed_result.rounds)
        assert all(sum(r["counts"].values()) == 4 for r in result.rounds)
        assert all(sorted(r) == [1, 2, 3] for r in result.completed)
        if result.winner:
            assert result.winner in result.rounds[-1]["counts"]


@pytest.fixture
def opened(tmp_path):
    store = VoteStore(str(tmp_path / "votes.sqlite"))
    vote = store.submit(1, "NAP size", 1, 100, 10, now=1,
                        vote_type="RANKED", options=["8", "10", "12"])
    store.open_next(1, [{"aid": i, "abbr": str(i)} for i in range(1, 11)], now=2)
    return store, vote


def test_explicit_abstention_replacement_and_restart(opened):
    store, vote = opened
    assert len(store.participation(vote)["DID_NOT_VOTE"]) == 10
    store.cast(vote, 1, [3, 1], 100, 10, now=3)
    store.cast(vote, 1, "ABSTAINED", 100, 10, now=4)
    assert store.participation(vote)["RANKED"] == []
    assert store.participation(vote)["ABSTAINED"] == ["1"]
    store.cast(vote, 1, [2], 100, 10, now=5)
    restored = VoteStore(store.path)
    assert restored.participation(vote)["RANKED"] == ["1"]
    assert restored.audit(vote)[-1]["previous_answer"] == "ABSTAINED"
    assert len(restored.audit(vote)) == 3
    assert restored.preview(vote).completed == ((2, 1, 3),)


def test_deadline_manual_corrections_and_frozen_result(opened):
    store, vote = opened
    for aid in range(1, 6):
        store.cast(vote, aid, [1], 100, 10, now=3)
    with pytest.raises(ValueError, match="majority"):
        store.conclude(vote, 99, now=4)
    with pytest.raises(ValueError, match="closed"):
        store.cast(vote, 6, [1], 100, 10, now=86402)
    store.cast(vote, 6, [2], 100, 99, now=86403, manual=True, reason="In game")
    preview = store.preview(vote)
    assert store.conclude(vote, 99, now=86404) == "PASSED"
    saved = json.loads(store.get(vote)["result"])
    assert saved["winner"] == preview.winner == 1
    with pytest.raises(ValueError, match="closed"):
        store.cast(vote, 6, [3], 100, 99, manual=True, reason="Late")
    assert json.loads(VoteStore(store.path).get(vote)["result"]) == saved


@pytest.mark.parametrize("answer", [[], [1, 1], [4], "YES", "NO"])
def test_invalid_storage_ballots_are_atomic(opened, answer):
    store, vote = opened
    with pytest.raises(ValueError):
        store.cast(vote, 1, answer, 100, 10, now=3)
    assert store.audit(vote) == []


def test_eligibility_and_manual_reason(opened):
    store, vote = opened
    with pytest.raises(ValueError, match="eligible"):
        store.cast(vote, 100, [1], 100, 10, now=3)
    with pytest.raises(ValueError, match="reason"):
        store.cast(vote, 1, [1], 100, 99, manual=True, now=3)


def test_early_conclusion_revalidates_replaced_ballots(opened):
    store, vote = opened
    for aid in range(1, 7):
        store.cast(vote, aid, [1], 100, 10, now=3)
    assert store.preview(vote).early_conclusion
    store.cast(vote, 6, "ABSTAINED", 100, 10, now=4)
    with pytest.raises(ValueError, match="majority"):
        store.conclude(vote, 99, now=5)


def test_legacy_schema_migration(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    original = VoteStore(path)
    vote = original.submit(1, "Old", 1, 100, 10)
    with sqlite3.connect(path) as conn:
        for column in ("vote_type", "options", "result"):
            conn.execute(f"ALTER TABLE votes DROP COLUMN {column}")
    restored = VoteStore(path)
    assert restored.get(vote)["vote_type"] == "STANDARD"
    assert restored.get(vote)["options"] == "[]"