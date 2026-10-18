"""SQLite: change history (for undo), pending confirmations, and OAuth state (see auth.py)."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS changes (id TEXT PRIMARY KEY, ts REAL, kind TEXT, summary TEXT,
                                    payload TEXT, undone INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS pending (token TEXT PRIMARY KEY, kind TEXT, payload TEXT, expires REAL);
CREATE TABLE IF NOT EXISTS audit (ts REAL, tool TEXT, args TEXT, result TEXT);
"""

CONFIRM_TTL = 600


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            self.db.executescript(SCHEMA)

    def q(self, sql: str, args: tuple = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.db.execute(sql, args).fetchall()

    def x(self, sql: str, args: tuple = ()) -> None:
        with self.lock:
            self.db.execute(sql, args)

    # ------------------------------------------------------------ confirmations
    def new_confirmation(self, kind: str, payload: Any) -> str:
        token = secrets.token_hex(3).upper()
        self.x("DELETE FROM pending WHERE expires < ?", (time.time(),))
        self.x("INSERT INTO pending VALUES (?,?,?,?)",
               (token, kind, json.dumps(payload), time.time() + CONFIRM_TTL))
        return token

    def take_confirmation(self, token: str, kind: str) -> Any | None:
        """The payload a token was issued for, once only. None if unknown, expired or another kind."""
        rows = self.q("SELECT payload FROM pending WHERE token=? AND kind=? AND expires > ?",
                      ((token or "").strip().upper(), kind, time.time()))
        if not rows:
            return None
        self.x("DELETE FROM pending WHERE token=?", ((token or "").strip().upper(),))
        return json.loads(rows[0]["payload"])

    # ------------------------------------------------------------ change history
    def record_change(self, kind: str, summary: str, payload: Any) -> str:
        cid = "c" + secrets.token_hex(3)
        self.x("INSERT INTO changes (id, ts, kind, summary, payload) VALUES (?,?,?,?,?)",
               (cid, time.time(), kind, summary, json.dumps(payload)))
        return cid

    def change(self, cid: str) -> sqlite3.Row | None:
        rows = self.q("SELECT * FROM changes WHERE id=?", (cid,))
        return rows[0] if rows else None

    def changes(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.q("SELECT * FROM changes ORDER BY ts DESC LIMIT ?", (limit,))

    def audit(self, tool: str, args: dict, result: str) -> None:
        self.x("INSERT INTO audit VALUES (?,?,?,?)",
               (time.time(), tool, json.dumps(args, default=str)[:4000], result[:4000]))
