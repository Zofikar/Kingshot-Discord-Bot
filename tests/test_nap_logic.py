"""Tests for the pure NAP logic in extension.nap."""
import asyncio

from extension.nap import NapLogic


def _logic(**kw):
    return NapLogic(**kw)


def test_parse_plain_line():
    rows = NapLogic.parse_nap_ranking_message("1. [MNX] FAMILLY - 235,820,341")
    assert rows == [{"abbr": "MNX", "name": "FAMILLY", "power": 235820341, "academy_tag": None}]


def test_parse_annotated_academy():
    rows = NapLogic.parse_nap_ranking_message("1. [MNX] FAMILLY - 235 (ac: [A15])")
    assert rows[0]["academy_tag"] == "A15"


def test_parse_footer_ignored():
    content = "1. [MNX] FAMILLY - 235\n2. [BBQ] Ing - 230\n\nLookback footer text"
    rows = NapLogic.parse_nap_ranking_message(content)
    assert len(rows) == 2


def test_parse_rejects_garbage():
    assert NapLogic.parse_nap_ranking_message("") is None
    assert NapLogic.parse_nap_ranking_message("hello") is None
    assert NapLogic.parse_nap_ranking_message("2. [MNX] FAMILLY - 235") is None


def test_build_nap_message_roundtrip():
    ranked = [{"aid": 1, "abbr": "MNX", "name": "FAMILLY", "power": 235820341}]
    msg = NapLogic.build_nap_message(ranked)
    assert msg == "1. [MNX] FAMILLY - 235,820,341"
    assert NapLogic.parse_nap_ranking_message(msg)[0]["abbr"] == "MNX"


def test_build_nap_message_academy():
    ranked = [{"aid": 1, "abbr": "MNX", "name": "FAMILLY", "power": 235}]
    msg = NapLogic.build_nap_message(ranked, {"1": "A15"})
    assert "ac: [A15]" in msg


def test_record_tag_alias():
    logic = _logic()
    assert logic.record_tag_alias("OLD", 111) is True
    assert logic.record_tag_alias("OLD", 111) is False
    assert logic._resolve_aid_for_tag("OLD") == 111


def test_resolve_tag_alias_survives_removal():
    logic = _logic(nap_tag_aliases={"OLD": 111})
    assert logic._resolve_aid_for_tag("OLD") == 111


def test_resolve_tag_exact_then_case_insensitive():
    logic = _logic(alliances={"1": {"abbr": "MNX", "name": "X"}})
    assert logic._resolve_aid_for_tag("MNX") == 1
    assert logic._resolve_aid_for_tag("mnx") == 1


def test_resolve_tag_case_collision_refuses():
    logic = _logic(alliances={"1": {"abbr": "MNX"}, "2": {"abbr": "MNx"}})
    assert logic._resolve_aid_for_tag("mnx") is None


def test_academy_set_unset():
    logic = _logic()
    assert logic.set_alliance_academy(10, 20) is True
    assert logic.get_main_aid_for_academy(20) == "10"
    assert logic.set_alliance_academy(20, 20) is False
    assert logic.set_alliance_academy(10, None) is True
    assert logic.get_main_aid_for_academy(20) is None


def test_academy_uniqueness():
    logic = _logic()
    logic.set_alliance_academy(10, 20)
    logic.set_alliance_academy(30, 20)
    assert logic.get_main_aid_for_academy(20) == "30"
    assert "10" not in logic.academies


def test_exclusion_skip_and_backfill():
    logic = _logic()
    for aid in range(10, 25):
        logic.alliances[str(aid)] = {"abbr": f"A{aid}", "name": f"N{aid}",
                                     "power": 250 - (aid - 10) * 10}
    logic.set_nap_exclusion(12, reason="broke rules", added_by="admin", tag="A12")
    ranking = asyncio.run(logic.get_nap_ranking())
    aids = [r["aid"] for r in ranking]
    assert 12 not in aids
    assert len(aids) == 10
    assert 20 in aids and 21 not in aids


def test_exclusion_lines_and_remove():
    logic = _logic(alliances={"12": {"abbr": "A12"}})
    logic.set_nap_exclusion(12, reason="broke rules", added_by="admin", tag="A12")
    assert logic.get_nap_exclusion_lines() == ["⛔ [A12] is not part of NAP because of broke rules"]
    assert logic.is_nap_excluded(aid=12)[0] is True
    assert logic.is_nap_excluded(tag="A12")[0] is True
    assert logic.remove_nap_exclusion(12) is True
    assert logic.is_nap_excluded(aid=12)[0] is False


def test_tag_list_filters_excluded():
    logic = _logic()
    logic.set_nap_exclusion(12, reason="x", added_by="admin", tag="A12")
    tags = logic.build_nap_tag_list([{"aid": 12, "abbr": "A12"}, {"aid": 10, "abbr": "A10"}], {}, [])
    assert tags == ["A10"]
