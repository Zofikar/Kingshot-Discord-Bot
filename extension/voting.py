"""Pure council voting policies; no Discord or database side effects."""
from dataclasses import dataclass, replace
from fractions import Fraction


def validate_options(options):
    options = list(options)
    if (not 2 <= len(options) <= 20 or
            any(not isinstance(x, str) or not x.strip() or len(x.strip()) > 80 for x in options)):
        raise ValueError("Ranked votes require 2–20 options, each 1–80 characters.")
    options = [x.strip() for x in options]
    if len({x.casefold() for x in options}) != len(options):
        raise ValueError("Options must be unique.")
    return options


def validate_ranking(ranking, count):
    if (not isinstance(ranking, (list, tuple)) or not ranking or
            any(type(i) is not int or not 1 <= i <= count for i in ranking) or
            len(set(ranking)) != len(ranking)):
        raise ValueError("Rank at least one valid option number, without duplicates.")
    return list(ranking)


def parse_ranking(text, count):
    try:
        ranking = [int(x.strip()) for x in text.replace(">", ",").split(",")]
    except (ValueError, AttributeError):
        raise ValueError("Enter option numbers in preference order, e.g. 3, 1, 2.") from None
    return validate_ranking(ranking, count)


@dataclass(frozen=True)
class RankedTally:
    status: str
    early_conclusion: bool
    did_not_vote: int
    winner: int | None
    completed: tuple
    rank_sums: tuple
    rounds: tuple
    tied: tuple = ()


def ranked_tally(eligible, option_count, rankings, abstained):
    """Exact-arithmetic IRV with simultaneous, leave-one-out completion.

    IDs are the immutable 1-based submission order. Equal completion averages
    fall back to that order; equal elimination averages produce a final tie.
    """
    if (type(eligible) is not int or type(abstained) is not int or
            type(option_count) is not int or not 2 <= option_count <= 20):
        raise ValueError("Invalid electorate, abstentions or option count.")
    ballots = [validate_ranking(r, option_count) for r in rankings]
    voters = len(ballots)
    if eligible <= 0 or abstained < 0 or voters + abstained > eligible:
        raise ValueError("Invalid electorate or ballot counts.")
    options = tuple(range(1, option_count + 1))
    reference = [{i: Fraction(r.index(i) + 1) if i in r else
                  Fraction(len(r) + 1 + option_count, 2) for i in options} for r in ballots]
    totals = {i: sum((q[i] for q in reference), Fraction()) for i in options}
    completed = tuple(tuple(r + sorted((i for i in options if i not in r),
                      key=lambda i: (totals[i] - reference[a][i], i)))
                      for a, r in enumerate(ballots))
    sums = tuple(sum(r.index(i) + 1 for r in completed) for i in options)
    early = any(2 * sum(r[0] == i for r in ballots) > eligible for i in options)
    missing = eligible - voters - abstained
    def result(status, winner=None, rounds=(), tied=()):
        return RankedTally(status, early, missing, winner, completed, sums, tuple(rounds), tuple(tied))
    if 100 * (voters + abstained) < 45 * eligible:
        return result("VOIDED")
    if not voters:
        return result("FAILED")
    remaining = set(options)
    rounds = []
    while remaining:
        counts = {i: 0 for i in sorted(remaining)}
        for r in completed:
            counts[next(i for i in r if i in remaining)] += 1
        record = {"counts": counts, "eliminated": None, "tiebreak": False}
        rounds.append(record)
        winner = next((i for i, n in counts.items() if 2 * n > voters), None)
        if winner is not None:
            return result("PASSED", winner, rounds)
        if len(remaining) == 2:
            best = min(sums[i - 1] for i in remaining)
            winners = [i for i in sorted(remaining) if sums[i - 1] == best]
            record["tiebreak"] = True
            return result("PASSED", winners[0], rounds) if len(winners) == 1 else result("TIED", rounds=rounds, tied=winners)
        lowest = min(counts.values())
        candidates = [i for i in remaining if counts[i] == lowest]
        worst = max(sums[i - 1] for i in candidates)
        losers = [i for i in sorted(candidates) if sums[i - 1] == worst]
        if len(losers) > 1:
            return result("TIED", rounds=rounds, tied=losers)
        record["eliminated"] = losers[0]
        record["tiebreak"] = len(candidates) > 1
        remaining.remove(losers[0])


@dataclass(frozen=True)
class Tally:
    status: str
    early_conclusion: bool
    did_not_vote: int


def tally(eligible: int, yes: int, no: int, abstained: int) -> Tally:
    """Calculate quorum first, then the strict Yes/No majority.

    Integer arithmetic deliberately avoids rounding at the 45% boundary.
    """
    counts = (eligible, yes, no, abstained)
    if any(type(value) is not int for value in counts):
        raise ValueError("Vote counts must be integers.")
    cast = yes + no + abstained
    if eligible <= 0 or min(yes, no, abstained) < 0 or cast > eligible:
        raise ValueError("Invalid electorate or ballot counts.")
    status = "VOIDED" if 100 * cast < 45 * eligible else (
        "PASSED" if yes > no else "FAILED")
    remaining = eligible - cast
    # A tie fails too: even every outstanding ballot voting Yes cannot pass.
    failure_locked = status == "FAILED" and yes + remaining <= no
    return Tally(status, 2 * yes > eligible or failure_locked, remaining)


@dataclass(frozen=True)
class QueueEntry:
    # Monotonically assigned IDs also encode original submission order.
    vote_id: int
    priority: int
    urgent: bool = False


def normalize_queue(entries: list[QueueEntry]) -> list[QueueEntry]:
    """Resolve all priority inversions, aging each displaced normal vote.

    Urgency is an independent overlay and never alters the base priority.
    Equal priorities preserve current order. Call once per logical mutation,
    and persist both the resulting order and updated priorities atomically.
    """
    urgent = sorted((entry for entry in entries if entry.urgent),
                    key=lambda entry: entry.vote_id)
    normal = [entry for entry in entries if not entry.urgent]
    while True:
        changed = False
        for index in range(len(normal) - 2, -1, -1):
            front, back = normal[index:index + 2]
            if front.priority < back.priority:
                normal[index:index + 2] = [
                    back, replace(front, priority=front.priority + 1)]
                changed = True
        if not changed:
            return urgent + normal