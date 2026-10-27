"""Tests for the extension storage layer: register/upsert + resolvers."""
import os
import sqlite3

import pytest

from extension import storage


@pytest.mark.parametrize("fid", [100, 200], ids=["main", "secondary"])
def test_guarded_registration_preserves_other_owner(tmp_path, fid):
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=42, nickname="Main", db_dir=db)
    storage.register_player(fid=200, discord_id=42, nickname="Alt", db_dir=db)
    before = dict(storage.user_row(fid, db_dir=db))
    assert storage.register_player(
        fid=fid, discord_id=43, nickname="Replacement", alliance_aid=999,
        reject_other_owner=True, db_dir=db) is False
    assert dict(storage.user_row(fid, db_dir=db)) == before
    assert storage.alliance_row(999, db_dir=db) is None
    assert storage.fids_for_discord(42, db_dir=db) == [(100, True), (200, False)]


def test_guarded_registration_allows_unbound_and_same_owner(tmp_path):
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=None, db_dir=db)
    assert storage.register_player(fid=100, discord_id=42, reject_other_owner=True, db_dir=db)
    assert storage.register_player(fid=100, discord_id=42, nickname="Updated",
                                   reject_other_owner=True, db_dir=db)
    assert storage.user_row(100, db_dir=db)["nickname"] == "Updated"
    # The explicit admin binding path retains its reassignment capability.
    assert storage.register_player(fid=100, discord_id=43, db_dir=db)
    assert storage.discord_for_fid(100, db_dir=db) == 43


def test_register_player_first_is_main(tmp_path):
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=111, nickname="Bob", kid=2464,
                            alliance_aid=7, rank=4, power=1000, abbr="XYZ",
                            alliance_name="Xyz", db_dir=db)
    assert storage.main_fid_for_discord(111, db_dir=db) == 100
    row = storage.user_row(100, db_dir=db)
    assert row["nickname"] == "Bob" and row["rank"] == 4 and row["power"] == 1000


def test_register_player_preserves_alliance_power(tmp_path):
    db = str(tmp_path)
    storage.upsert_alliance(247300011, abbr="TKO", name="theknightsONE",
                            kid=2464, role_id=1544429714104385500,
                            power=2105508213, db_dir=db)
    storage.register_player(fid=100, discord_id=111, alliance_aid=247300011,
                            abbr="TKO", alliance_name="theknightsONE", kid=2464,
                            power=33912143, db_dir=db)

    assert storage.user_row(100, db_dir=db)["power"] == 33912143
    alliance = storage.alliance_row(247300011, db_dir=db)
    assert alliance["power"] == 2105508213
    assert alliance["role_id"] == 1544429714104385500


def test_register_player_does_not_seed_alliance_with_player_power(tmp_path):
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=111, alliance_aid=7,
                            abbr="XYZ", alliance_name="Xyz", kid=2464,
                            power=33912143, db_dir=db)

    alliance = storage.alliance_row(7, db_dir=db)
    assert alliance["abbr"] == "XYZ"
    assert alliance["power"] is None
    assert storage.user_row(100, db_dir=db)["power"] == 33912143


def test_register_player_second_is_secondary(tmp_path):
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=111, db_dir=db)
    storage.register_player(fid=200, discord_id=111, db_dir=db)
    assert storage.main_fid_for_discord(111, db_dir=db) == 100
    assert storage.fids_for_discord(111, db_dir=db) == [(100, True), (200, False)]


def test_register_player_reregister_keeps_main(tmp_path):
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=111, db_dir=db)
    storage.register_player(fid=200, discord_id=111, db_dir=db)
    storage.set_main_fid(111, 200, db_dir=db)
    assert storage.main_fid_for_discord(111, db_dir=db) == 200
    # re-register fid 100 must not demote 200
    storage.register_player(fid=100, discord_id=111, nickname="Bob2", db_dir=db)
    assert storage.main_fid_for_discord(111, db_dir=db) == 200


def test_linked_main_accounts_returns_one_primary_per_member(tmp_path):
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=111, discord_server_id=1, db_dir=db)
    storage.register_player(fid=200, discord_id=111, discord_server_id=1, db_dir=db)
    storage.set_main_fid(111, 200, db_dir=db)
    storage.register_player(fid=300, discord_id=222, discord_server_id=2, db_dir=db)
    storage.register_player(fid=400, discord_id=None, discord_server_id=2, db_dir=db)

    assert storage.linked_main_accounts(db_dir=db) == [
        (200, 111, 1),
        (300, 222, 2),
    ]


def test_upsert_alliance_writes_extension_cols(tmp_path):
    db = str(tmp_path)
    storage.upsert_alliance(7, abbr="XYZ", name="Xyz", kid=2464, power=1000, db_dir=db)
    with sqlite3.connect(os.path.join(db, storage.ALLIANCE_FILE)) as conn:
        row = conn.execute(
            "SELECT name, kid, abbr, power FROM alliance_list WHERE alliance_id=7"
        ).fetchone()
    assert row == ("Xyz", 2464, "XYZ", 1000)


def test_top_alliances_by_power_orders(tmp_path):
    db = str(tmp_path)
    storage.upsert_alliance(1, power=10, db_dir=db)
    storage.upsert_alliance(2, power=30, db_dir=db)
    storage.upsert_alliance(3, power=20, db_dir=db)
    assert storage.top_alliances_by_power(2, db_dir=db) == [2, 3]


def test_all_alliance_role_ids(tmp_path):
    db = str(tmp_path)
    storage.upsert_alliance(1, role_id=101, db_dir=db)
    storage.upsert_alliance(2, role_id=102, db_dir=db)
    assert sorted(storage.all_alliance_role_ids(db_dir=db)) == [101, 102]


def test_discord_ids_for_alliances_matches_linked_fids(tmp_path):
    """Drives the post-rename re-tagging of members."""
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=111, alliance_aid=7, abbr="XYZ", db_dir=db)
    storage.register_player(fid=200, discord_id=222, alliance_aid=8, abbr="ABC", db_dir=db)
    storage.register_player(fid=300, discord_id=None, alliance_aid=7, db_dir=db)

    assert storage.discord_ids_for_alliances([7], db_dir=db) == [111]
    assert sorted(storage.discord_ids_for_alliances([7, "8"], db_dir=db)) == [111, 222]
    assert storage.discord_ids_for_alliances([], db_dir=db) == []
    assert storage.discord_ids_for_alliances([999], db_dir=db) == []

