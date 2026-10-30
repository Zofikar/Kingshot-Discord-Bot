"""Durable diplomatic mandates, distinct from temporary voting eligibility."""
import time

from .vote_storage import VoteStore


class MandateStore(VoteStore):
    def __init__(self, path="db/council_votes.sqlite"):
        super().__init__(path)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS mandates (
                    guild_id INTEGER NOT NULL, aid INTEGER NOT NULL,
                    member_id INTEGER NOT NULL, active INTEGER NOT NULL DEFAULT 1,
                    PRIMARY KEY(guild_id,aid), UNIQUE(guild_id,member_id)
                );
                CREATE TABLE IF NOT EXISTS mandate_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, aid INTEGER NOT NULL,
                    member_id INTEGER, actor INTEGER, action TEXT NOT NULL,
                    recorded_at REAL NOT NULL
                );
            """)

    @staticmethod
    def _record(conn, guild_id, aid, member, actor, action):
        conn.execute("INSERT INTO mandate_history(guild_id,aid,member_id,actor,"
                     "action,recorded_at) VALUES(?,?,?,?,?,?)",
                     (guild_id, aid, member, actor, action, time.time()))

    def assignments(self, guild_id):
        with self._connect() as conn:
            return {r["aid"]: r["member_id"] for r in conn.execute(
                "SELECT * FROM mandates WHERE guild_id=?", (guild_id,))}

    def active(self, guild_id):
        with self._connect() as conn:
            return {r["aid"]: r["member_id"] for r in conn.execute(
                "SELECT * FROM mandates WHERE guild_id=? AND active=1", (guild_id,))}

    def assign(self, guild_id, aid, member, actor, source):
        with self._connect() as conn:
            conflict = conn.execute("SELECT aid FROM mandates WHERE guild_id=? "
                                    "AND member_id=? AND aid<>?", (guild_id, member, aid)).fetchone()
            if conflict:
                raise ValueError("This member already represents another alliance; relinquish first.")
            conn.execute("INSERT INTO mandates(guild_id,aid,member_id) VALUES(?,?,?) "
                         "ON CONFLICT(guild_id,aid) DO UPDATE SET member_id=excluded.member_id",
                         (guild_id, aid, member))
            self._record(conn, guild_id, aid, member, actor, source)

    def relinquish(self, guild_id, member):
        with self._connect() as conn:
            row = conn.execute("SELECT aid FROM mandates WHERE guild_id=? AND member_id=?",
                               (guild_id, member)).fetchone()
            if not row:
                raise ValueError("You do not hold a diplomatic mandate.")
            conn.execute("DELETE FROM mandates WHERE guild_id=? AND member_id=?", (guild_id, member))
            self._record(conn, guild_id, row[0], None, member, "relinquished")

    def reconcile(self, guild_id, eligible, present, leaders):
        eligible, present = set(eligible), set(present)
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM mandates WHERE guild_id=?", (guild_id,)).fetchall()
            leader_alliances = {member: aid for aid, member in leaders.items()}
            for row in rows:
                member, aid = row["member_id"], row["aid"]
                new_leadership = leader_alliances.get(member)
                if member not in present or (new_leadership is not None and new_leadership != aid):
                    conn.execute("DELETE FROM mandates WHERE guild_id=? AND aid=?", (guild_id, aid))
                    self._record(conn, guild_id, aid, None, None, "departed_or_new_leadership")
            assignments = {r["aid"]: r["member_id"] for r in conn.execute(
                "SELECT * FROM mandates WHERE guild_id=?", (guild_id,))}
            for aid in sorted(eligible):
                member = leaders.get(aid)
                if aid not in assignments and member in present and member not in assignments.values():
                    conn.execute("INSERT INTO mandates(guild_id,aid,member_id) VALUES(?,?,?)",
                                 (guild_id, aid, member))
                    assignments[aid] = member
                    self._record(conn, guild_id, aid, member, None, "default_r5")
            for aid in assignments:
                conn.execute("UPDATE mandates SET active=? WHERE guild_id=? AND aid=?",
                             (int(aid in eligible), guild_id, aid))

    def history(self, guild_id):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM mandate_history WHERE guild_id=? ORDER BY id", (guild_id,))]