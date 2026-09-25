"""Tests for the pure role/nick/whitelist helpers in extension.roles."""
from extension import roles


def test_parse_fids_blank():
    assert roles.parse_fids(None) == []
    assert roles.parse_fids("") == []
    assert roles.parse_fids("   ") == []


def test_parse_fids_mixed_separators():
    assert roles.parse_fids("123, 456 789") == [123, 456, 789]


def test_parse_fids_invalid():
    assert roles.parse_fids("abc") is None
    assert roles.parse_fids("12x") is None


def test_formatted_nickname_with_tag():
    assert roles.formatted_nickname("MNX", "Bob") == "[MNX] Bob"


def test_formatted_nickname_no_tag():
    assert roles.formatted_nickname(None, "Bob") == "Bob"


def test_formatted_nickname_truncates():
    assert len(roles.formatted_nickname("MNX", "x" * 40)) == 32


def test_rank_roles_r5():
    add, remove = roles.rank_role_ids(5, role_r5_id=1, role_r4_id=2)
    assert add == [1] and remove == [2]


def test_rank_roles_r4():
    add, remove = roles.rank_role_ids(4, role_r5_id=1, role_r4_id=2)
    assert add == [2] and remove == [1]


def test_rank_roles_other():
    add, remove = roles.rank_role_ids(3, role_r5_id=1, role_r4_id=2)
    assert add == [] and remove == [1, 2]


def test_council_role_in_top():
    add, remove = roles.council_role_ids(5, 7, {"7", "8"}, role_council_id=9)
    assert add == [9] and remove == []


def test_council_role_not_in_top():
    add, remove = roles.council_role_ids(5, 6, {"7", "8"}, role_council_id=9)
    assert add == [] and remove == [9]


def test_whitelist_empty_allows_all():
    assert roles.whitelist_denial(2464, 7) is None


def test_whitelist_kingdom_deny():
    assert roles.whitelist_denial(999, 7, whitelist_kids=[2464]) is not None


def test_whitelist_kingdom_allow():
    assert roles.whitelist_denial(2464, 7, whitelist_kids=[2464]) is None


def test_whitelist_alliance_deny():
    assert roles.whitelist_denial(2464, 7, whitelist_alliances=[8]) is not None


def test_whitelist_alliance_allow():
    assert roles.whitelist_denial(2464, 7, whitelist_alliances=[7]) is None


def test_player_to_user_fields():
    f = roles.player_to_user_fields(
        123, {"nick_name": "Bob", "kid": 2464, "power": 1000,
              "alliance": {"aid": 7, "abbr": "XYZ", "name": None, "rank": 4}},
    )
    assert f["fid"] == 123
    assert f["nickname"] == "Bob"
    assert f["kid"] == 2464
    assert f["power"] == 1000
    assert f["alliance_aid"] == 7
    assert f["rank"] == 4
    assert f["abbr"] == "XYZ"
    assert f["alliance_name"] == "XYZ"  # falls back to abbr


def test_player_to_user_fields_no_alliance():
    f = roles.player_to_user_fields(123, {"nick_name": "Bob", "kid": 2464, "power": 0})
    assert f["alliance_aid"] is None
    assert f["rank"] is None
    assert f["abbr"] is None


def test_compute_sync_r5_council():
    nick, add, remove = roles.compute_sync(
        nickname="Bob", abbr="MNX", rank=5, alliance_aid=7, top_aids={"7"},
        verified_role_id=10, alliance_role_id=11, role_r5_id=12, role_r4_id=13,
        role_council_id=14,
    )
    assert nick == "[MNX] Bob"
    assert add == [12, 14, 10, 11]
    assert remove == [13]


def test_compute_sync_unranked_strips_roles():
    nick, add, remove = roles.compute_sync(
        nickname="Bob", abbr=None, rank=3, alliance_aid=7, top_aids={"7"},
        role_r5_id=12, role_r4_id=13, role_council_id=14,
    )
    assert nick == "Bob"
    assert add == []
    assert remove == [12, 13, 14]
