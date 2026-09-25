"""Pure role / nickname / whitelist logic for the identity cogs.

Kept free of Discord I/O so it can be unit-tested directly; the cogs orchestrate
these helpers around ``discord.Member`` objects.
"""

import re


def parse_fids(text):
    """Parse a comma/space-separated list of FIDs.

    Returns [] for blank input, a list of ints for valid input, or None when any
    token is not a non-negative integer.
    """
    if text is None:
        return []
    text = str(text).strip()
    if not text:
        return []
    fids = []
    for token in re.split(r"[,\s]+", text):
        if not token:
            continue
        if not token.isdigit():
            return None
        fids.append(int(token))
    return fids


def formatted_nickname(abbr, nick_name, limit=32):
    """``[TAG] Name`` (or just ``Name``), truncated to Discord's 32-char cap."""
    name = (nick_name or "Unknown").strip()
    if abbr:
        return f"[{abbr}] {name}"[:limit]
    return name[:limit]


def rank_role_ids(rank, *, role_r5_id=None, role_r4_id=None):
    """(add, remove) role ids for an alliance rank (5 = R5, 4 = R4)."""
    add, remove = [], []
    if rank == 5:
        if role_r5_id:
            add.append(role_r5_id)
        if role_r4_id:
            remove.append(role_r4_id)
    elif rank == 4:
        if role_r4_id:
            add.append(role_r4_id)
        if role_r5_id:
            remove.append(role_r5_id)
    else:
        if role_r5_id:
            remove.append(role_r5_id)
        if role_r4_id:
            remove.append(role_r4_id)
    return add, remove


def council_role_ids(rank, alliance_aid, top_aids, *, role_council_id=None):
    """(add, remove) for the council role: an R5 in a top-N alliance."""
    if not role_council_id:
        return [], []
    top = {str(a) for a in top_aids}
    if rank == 5 and alliance_aid is not None and str(alliance_aid) in top:
        return [role_council_id], []
    return [], [role_council_id]


def whitelist_denial(kid, aid, *, whitelist_kids=None, whitelist_alliances=None):
    """Human-readable denial reason, or None when the player is allowed.

    Empty whitelists allow everyone. Non-empty whitelists require membership.
    """
    kids = [int(k) for k in (whitelist_kids or [])]
    alliances = [int(a) for a in (whitelist_alliances or [])]

    def _int(value):
        try:
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    if kids:
        if _int(kid) not in kids:
            return f"Kingdom #{kid} is not on the whitelist"
    if alliances:
        if _int(aid) not in alliances:
            return f"Alliance #{aid} is not on the whitelist"
    return None


def player_to_user_fields(gid, player):
    """Map a MightPulse player dict to the users/alliance_list storage fields."""
    alliance = player.get("alliance") or {}
    return {
        "fid": int(gid),
        "nickname": player.get("nick_name"),
        "kid": player.get("kid"),
        "alliance_aid": alliance.get("aid"),
        "rank": alliance.get("rank"),
        "power": player.get("power"),
        "abbr": alliance.get("abbr"),
        "alliance_name": alliance.get("name") or alliance.get("abbr"),
    }


def compute_sync(*, nickname, abbr=None, rank=None, alliance_aid=None, top_aids=None,
                 alliance_role_id=None, verified_role_id=None, role_r5_id=None,
                 role_r4_id=None, role_council_id=None):
    """Compute (target_nickname, roles_to_add, roles_to_remove) for a member."""
    target_nick = formatted_nickname(abbr, nickname)

    add, remove = rank_role_ids(rank, role_r5_id=role_r5_id, role_r4_id=role_r4_id)
    c_add, c_remove = council_role_ids(rank, alliance_aid, top_aids or [],
                                       role_council_id=role_council_id)
    add += c_add
    remove += c_remove

    if verified_role_id:
        add.append(verified_role_id)
    if alliance_role_id:
        add.append(alliance_role_id)

    # De-dupe while preserving order.
    add = list(dict.fromkeys(add))
    remove = list(dict.fromkeys(remove))
    return target_nick, add, remove
