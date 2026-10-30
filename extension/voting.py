"""Pure council voting policies; no Discord or database side effects."""
from dataclasses import dataclass, replace


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
    return Tally(status, 2 * yes > eligible, eligible - cast)


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