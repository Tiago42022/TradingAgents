"""Single dispatcher and durable attempt/outbox state, independent of the UI."""
import json
import os
from pathlib import Path
import sqlite3

from .common import Refusal, canonical


class Singleton:
    def __init__(self, directory):
        self.file = open(Path(directory) / "executor.lock", "a+b")
        try:
            if self.file.seek(0, 2) == 0:
                self.file.write(b"0"); self.file.flush(); os.fsync(self.file.fileno())
            self.file.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except Exception:
            self.file.close()
            raise Refusal("executor_already_running", True) from None

    def close(self):
        self.file.close()


class Journal:
    def __init__(self, directory):
        self.db = sqlite3.connect(str(Path(directory) / "journal.sqlite3"), timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS intents(id TEXT PRIMARY KEY, binding TEXT NOT NULL, state TEXT NOT NULL,
            prepared TEXT, execute_claim TEXT, attempted_at INTEGER);
          CREATE TABLE IF NOT EXISTS outbox(id TEXT NOT NULL, claim TEXT NOT NULL, payload TEXT NOT NULL,
            acknowledged INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(id,claim));
          CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        """)

    def bind(self, binding):
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO metadata VALUES('binding',?)", (binding,))
            if self.db.execute("SELECT value FROM metadata WHERE key='binding'").fetchone()[0] != binding:
                raise Refusal("journal_binding_changed", True)

    def get(self, identity):
        row = self.db.execute("SELECT * FROM intents WHERE id=?", (identity,)).fetchone()
        return dict(row) if row else None

    def prepare(self, identity, binding, prepared, payload):
        with self.db:
            if self.get(identity):
                raise Refusal("duplicate_prepare", True)
            self.db.execute("INSERT INTO intents VALUES(?,?, 'ready', ?,NULL,NULL)", (identity, binding, canonical(prepared)))
            self._outbox(payload)

    def reserve(self, identity, binding, claim, timestamp):
        with self.db:
            cursor = self.db.execute("UPDATE intents SET state='attempted',execute_claim=?,attempted_at=? WHERE id=? AND binding=? AND state='ready'", (claim, timestamp, identity, binding))
            if cursor.rowcount != 1:
                raise Refusal("attempt_already_reserved", True)
        # Returning only after COMMIT under synchronous=FULL is the send barrier.

    def _outbox(self, payload):
        encoded = canonical(payload)
        current = self.db.execute("SELECT payload FROM outbox WHERE id=? AND claim=?", (payload["id"], payload["claimId"])).fetchone()
        if current and current[0] != encoded:
            # A later reconciliation is an explicit state transition, never a
            # rewritten unacknowledged result or duplicate execution permission.
            acknowledged = self.db.execute("SELECT acknowledged FROM outbox WHERE id=? AND claim=?", (payload["id"], payload["claimId"])).fetchone()[0]
            if not acknowledged:
                raise Refusal("outbox_conflict", True)
        self.db.execute("INSERT INTO outbox VALUES(?,?,?,0) ON CONFLICT(id,claim) DO UPDATE SET payload=excluded.payload,acknowledged=0", (payload["id"], payload["claimId"], encoded))

    def result(self, payload, state=None):
        with self.db:
            self._outbox(payload)
            if state:
                self.db.execute("UPDATE intents SET state=? WHERE id=?", (state, payload["id"]))

    def pending(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM outbox WHERE acknowledged=0 ORDER BY rowid")]

    def acknowledge(self, payload, discarded=None):
        with self.db:
            self.db.execute("UPDATE outbox SET acknowledged=1 WHERE id=? AND claim=? AND payload=?", (payload["id"], payload["claimId"], canonical(payload)))
            if discarded:
                self.db.execute("UPDATE intents SET state=? WHERE id=? AND state='ready' AND execute_claim IS NULL", (discarded, payload["id"]))

    def latest_result(self, identity, claim):
        row = self.db.execute("SELECT payload FROM outbox WHERE id=? AND claim=?", (identity, claim)).fetchone()
        return json.loads(row[0]).get("result") if row else None

    def unsettled(self):
        return [dict(row) for row in self.db.execute("SELECT * FROM intents WHERE state IN ('attempted','unknown','accepted')")]

    def close(self):
        self.db.close()
