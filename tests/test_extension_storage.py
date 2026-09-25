"""Tests for the extension storage layer: register/upsert + resolvers."""
import os
import sqlite3

from extension import storage


def test_register_player_first_is_main(tmp_path):
    db = str(tmp_path)
    storage.register_player(fid=100, discord_id=111, nickname="Bob", kid=2464,
                            alliance_aid=7, rank=4, power=1000, abbr="XYZ",
                            alliance_name="Xyz", db_dir=db)
    assert storage.main_fid_for_discord(111, db_dir=db) == 100
    row = storage.user_row(100, db_dir=db)
    assert row["nickname"] == "Bob" and row["rank"] == 4 and row["power"] == 1000


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

