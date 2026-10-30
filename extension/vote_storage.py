"""Transactional council vote persistence, independent of Discord transport.

Authorization belongs to the command layer; this layer enforces lifecycle,
electorate, uniqueness and audit invariants even across concurrent writers.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

from .voting import QueueEntry, normalize_queue, tally


class VoteStore:
    def __init__(self, path="db/council_votes.sqlite"):
        self.path = path
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS votes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    guild_id INTEGER NOT NULL, title TEXT NOT NULL,
                    alliance_id INTEGER NOT NULL, submitter_fid INTEGER NOT NULL,
                    submitted_by INTEGER NOT NULL, submitted_at REAL NOT NULL,
                    status TEXT NOT NULL DEFAULT 'DEFINED',
                    priority INTEGER NOT NULL, position INTEGER NOT NULL,
                    urgent INTEGER NOT NULL DEFAULT 0,
                    electorate TEXT, opened_at REAL, closes_at REAL,
                    concluded_by INTEGER, concluded_at REAL,
                    removed_by INTEGER, removal_reason TEXT
                );
                CREATE UNIQUE INDEX IF NOT EXISTS one_pending_vote_per_guild
                    ON votes(guild_id)
                    WHERE status IN ('OPEN', 'AWAITS_APPROVAL');
                CREATE TABLE IF NOT EXISTS ballots (
                    vote_id INTEGER NOT NULL, alliance_id INTEGER NOT NULL,
                    answer TEXT NOT NULL, fid INTEGER NOT NULL,
                    actor INTEGER NOT NULL, recorded_at REAL NOT NULL,
                    PRIMARY KEY(vote_id, alliance_id)
                );
                CREATE TABLE IF NOT EXISTS ballot_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    vote_id INTEGER NOT NULL, alliance_id INTEGER NOT NULL,
                    answer TEXT NOT NULL, fid INTEGER NOT NULL,
                    actor INTEGER NOT NULL, recorded_at REAL NOT NULL,
                    manual INTEGER NOT NULL, reason TEXT NOT NULL,
                    previous_answer TEXT
                );
            """)
            columns = {r[1] for r in conn.execute("PRAGMA table_info(votes)")}
            for name, definition in {
                "normal_order": "TEXT", "submitter_tag": "TEXT",
                "submitter_name": "TEXT", "submitter_discord": "INTEGER",
                "channel_id": "INTEGER", "message_id": "INTEGER",
                "result_message_id": "INTEGER", "notified": "INTEGER DEFAULT 0",
                "advanced": "INTEGER DEFAULT 0",
            }.items():
                if name not in columns:
                    conn.execute(f"ALTER TABLE votes ADD COLUMN {name} {definition}")
            conn.execute("CREATE TABLE IF NOT EXISTS queue_audit (id INTEGER PRIMARY KEY,"
                         "vote_id INTEGER,actor INTEGER,action TEXT,reason TEXT,recorded_at REAL)")

    @contextmanager
    def _connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                conn.execute("BEGIN IMMEDIATE")
                yield conn
        finally:
            conn.close()

    @staticmethod
    def _now(now):
        return time.time() if now is None else now

    @staticmethod
    def _get(conn, vote_id):
        row = conn.execute("SELECT * FROM votes WHERE id=?", (vote_id,)).fetchone()
        if row is None:
            raise ValueError("Unknown vote.")
        return dict(row)

    def get(self, vote_id):
        with self._connect() as conn:
            return self._get(conn, vote_id)

    @staticmethod
    def _normalize(conn, guild_id):
        rows = conn.execute(
            "SELECT * FROM votes WHERE guild_id=? AND status='DEFINED' "
            "ORDER BY position,id", (guild_id,)).fetchall()
        entries = [QueueEntry(row["id"], row["priority"], bool(row["urgent"]))
                   for row in rows]
        for position, entry in enumerate(normalize_queue(entries)):
            conn.execute("UPDATE votes SET position=?,priority=? WHERE id=?",
                         (position, entry.priority, entry.vote_id))

    def submit(self, guild_id, title, alliance_id, fid, actor, *, now=None):
        title = title.strip()
        if not title:
            raise ValueError("A question is required.")
        with self._connect() as conn:
            lowest = conn.execute(
                "SELECT MIN(priority) FROM votes WHERE guild_id=? AND alliance_id=? "
                "AND status IN ('DEFINED','OPEN','AWAITS_APPROVAL')",
                (guild_id, alliance_id)).fetchone()[0]
            position = conn.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM votes "
                "WHERE guild_id=? AND status='DEFINED'", (guild_id,)).fetchone()[0]
            cursor = conn.execute(
                "INSERT INTO votes(guild_id,title,alliance_id,submitter_fid,"
                "submitted_by,submitted_at,priority,position) VALUES(?,?,?,?,?,?,?,?)",
                (guild_id, title, alliance_id, fid, actor, self._now(now),
                 0 if lowest is None else lowest - 1, position))
            self._normalize(conn, guild_id)
            return cursor.lastrowid

    def queue(self, guild_id):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM votes WHERE guild_id=? AND status='DEFINED' "
                "ORDER BY position,id", (guild_id,))]

    def pending(self, guild_id):
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM votes WHERE guild_id=? AND status IN "
                               "('OPEN','AWAITS_APPROVAL')", (guild_id,)).fetchone()
            return dict(row) if row else None

    def list_votes(self, guild_id):
        with self._connect() as conn:
            return [dict(r) for r in conn.execute(
                "SELECT * FROM votes WHERE guild_id=? ORDER BY id DESC", (guild_id,))]

    def set_identity(self, vote_id, tag, name, discord_id):
        with self._connect() as conn:
            conn.execute("UPDATE votes SET submitter_tag=?,submitter_name=?,submitter_discord=? "
                         "WHERE id=?", (tag, name, discord_id, vote_id))

    def update_delivery(self, vote_id, **values):
        allowed = {"channel_id", "message_id", "result_message_id", "notified", "advanced"}
        if not values or not set(values) <= allowed:
            raise ValueError("Invalid delivery metadata.")
        with self._connect() as conn:
            conn.execute("UPDATE votes SET " + ",".join(f"{k}=?" for k in values) + " WHERE id=?",
                         (*values.values(), vote_id))

    def set_urgent(self, vote_id, urgent, actor, reason):
        if urgent and not reason.strip():
            raise ValueError("Urgency requires a reason.")
        with self._connect() as conn:
            vote = self._get(conn, vote_id)
            if vote["status"] != "DEFINED":
                raise ValueError("Only DEFINED votes can change urgency.")
            if bool(vote["urgent"]) == urgent:
                return
            if urgent:
                order = [r[0] for r in conn.execute("SELECT id FROM votes WHERE guild_id=? "
                         "AND status='DEFINED' AND urgent=0 ORDER BY position,id", (vote["guild_id"],))]
                conn.execute("UPDATE votes SET urgent=1,normal_order=? WHERE id=?",
                             (json.dumps(order), vote_id))
            else:
                order = json.loads(vote["normal_order"] or "[]")
                current = [r[0] for r in conn.execute("SELECT id FROM votes WHERE guild_id=? "
                           "AND status='DEFINED' ORDER BY position,id", (vote["guild_id"],)) if r[0] != vote_id]
                before = set(order[:order.index(vote_id)]) if vote_id in order else set()
                index = max((i + 1 for i, v in enumerate(current) if v in before), default=0)
                current.insert(index, vote_id)
                conn.execute("UPDATE votes SET urgent=0 WHERE id=?", (vote_id,))
                for position, item in enumerate(current):
                    conn.execute("UPDATE votes SET position=? WHERE id=?", (position, item))
            conn.execute("INSERT INTO queue_audit(vote_id,actor,action,reason,recorded_at) "
                         "VALUES(?,?,?,?,?)", (vote_id, actor, "urgent" if urgent else "normal",
                                              reason.strip(), time.time()))
            self._normalize(conn, vote["guild_id"])

    def open_next(self, guild_id, electorate, *, now=None):
        electorate = [{"aid": int(row["aid"]), "abbr": str(row["abbr"])}
                      for row in electorate]
        if not electorate or len({row["aid"] for row in electorate}) != len(electorate):
            raise ValueError("A nonempty unique electorate is required.")
        now = self._now(now)
        with self._connect() as conn:
            if conn.execute("SELECT 1 FROM votes WHERE guild_id=? AND status IN "
                            "('OPEN','AWAITS_APPROVAL')", (guild_id,)).fetchone():
                raise ValueError("There is already a pending vote.")
            row = conn.execute("SELECT id FROM votes WHERE guild_id=? AND "
                               "status='DEFINED' ORDER BY position,id LIMIT 1",
                               (guild_id,)).fetchone()
            if row is None:
                raise ValueError("The queue is empty.")
            conn.execute("UPDATE votes SET status='OPEN',electorate=?,opened_at=?,"
                         "closes_at=? WHERE id=?",
                         (json.dumps(electorate), now, now + 86400, row[0]))
            self._normalize(conn, guild_id)
            return row[0]

    def expire(self, *, now=None):
        with self._connect() as conn:
            conn.execute("UPDATE votes SET status='AWAITS_APPROVAL' WHERE "
                         "status='OPEN' AND closes_at<=?", (self._now(now),))

    def cast(self, vote_id, alliance_id, answer, fid, actor, *, now=None,
             manual=False, reason=""):
        now = self._now(now)
        self.expire(now=now)
        if answer not in ("YES", "NO", "ABSTAINED"):
            raise ValueError("Unknown answer.")
        if manual and not reason.strip():
            raise ValueError("A manual ballot requires a reason.")
        with self._connect() as conn:
            vote = self._get(conn, vote_id)
            allowed = ("OPEN", "AWAITS_APPROVAL") if manual else ("OPEN",)
            if vote["status"] not in allowed:
                raise ValueError("Voting is closed.")
            if alliance_id not in {row["aid"] for row in json.loads(vote["electorate"])}:
                raise ValueError("Alliance is not eligible for this vote.")
            previous = conn.execute("SELECT answer FROM ballots WHERE vote_id=? "
                                    "AND alliance_id=?", (vote_id, alliance_id)).fetchone()
            conn.execute("INSERT INTO ballots VALUES(?,?,?,?,?,?) ON CONFLICT"
                         "(vote_id,alliance_id) DO UPDATE SET answer=excluded.answer,"
                         "fid=excluded.fid,actor=excluded.actor,recorded_at=excluded.recorded_at",
                         (vote_id, alliance_id, answer, fid, actor, now))
            conn.execute("INSERT INTO ballot_audit(vote_id,alliance_id,answer,fid,"
                         "actor,recorded_at,manual,reason,previous_answer) "
                         "VALUES(?,?,?,?,?,?,?,?,?)",
                         (vote_id, alliance_id, answer, fid, actor, now, int(manual),
                          reason.strip(), previous[0] if previous else None))

    @staticmethod
    def _participation(conn, vote):
        ballots = {row["alliance_id"]: row["answer"] for row in conn.execute(
            "SELECT alliance_id,answer FROM ballots WHERE vote_id=?", (vote["id"],))}
        groups = {answer: [] for answer in ("YES", "NO", "ABSTAINED", "DID_NOT_VOTE")}
        for alliance in json.loads(vote["electorate"]):
            groups[ballots.get(alliance["aid"], "DID_NOT_VOTE")].append(alliance["abbr"])
        return groups

    def participation(self, vote_id):
        with self._connect() as conn:
            return self._participation(conn, self._get(conn, vote_id))

    def audit(self, vote_id):
        with self._connect() as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM ballot_audit WHERE vote_id=? ORDER BY id", (vote_id,))]

    def conclude(self, vote_id, actor, *, now=None):
        now = self._now(now)
        self.expire(now=now)
        with self._connect() as conn:
            vote = self._get(conn, vote_id)
            if vote["status"] not in ("OPEN", "AWAITS_APPROVAL"):
                raise ValueError("Vote is not pending.")
            groups = self._participation(conn, vote)
            result = tally(sum(map(len, groups.values())), len(groups["YES"]),
                           len(groups["NO"]), len(groups["ABSTAINED"]))
            if vote["status"] == "OPEN" and not result.early_conclusion:
                raise ValueError("An absolute Yes majority is required to conclude early.")
            conn.execute("UPDATE votes SET status=?,concluded_by=?,concluded_at=? "
                         "WHERE id=?", (result.status, actor, now, vote_id))
            return result.status

    def remove(self, vote_id, actor, reason, *, now=None):
        with self._connect() as conn:
            vote = self._get(conn, vote_id)
            if vote["status"] != "DEFINED":
                raise ValueError("Only DEFINED votes can be removed.")
            conn.execute("UPDATE votes SET status='REMOVED',removed_by=?,"
                         "removal_reason=?,concluded_at=? WHERE id=?",
                         (actor, reason, self._now(now), vote_id))
            self._normalize(conn, vote["guild_id"])