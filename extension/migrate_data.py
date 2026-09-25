"""One-time migration: import the pre-migration ``data.json`` into the unified DBs.

Run from the bot directory (so ``extension`` is importable):

    python -m extension.migrate_data /path/to/data.json
    python -m extension.migrate_data /path/to/data.json --db-dir db

Maps the pre-migration shapes onto the upstream tables:

    data.json.players   -> db/users.sqlite      users          (idempotent upsert)
    data.json.alliances -> db/alliance.sqlite   alliance_list  (idempotent upsert)
    data.json.nap_*     -> db/verification.sqlite             (extension-owned)
    data.json.settings  -> db/settings.sqlite   bot_global_settings (ext.* keys)
"""

import argparse

from .storage import import_from_json


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("json_path", help="Path to the pre-migration data.json")
    parser.add_argument("--db-dir", default="db",
                        help="Database directory (default: %(default)s)")
    args = parser.parse_args(argv)

    report = import_from_json(args.json_path, db_dir=args.db_dir)
    print("Migration complete:")
    for line in report:
        print(f"  - {line}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
