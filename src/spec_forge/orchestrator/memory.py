"""Episodic Counterexample Ledger (SQLite, WAL mode).

Persists, per verification session and target:
  * every counterexample ever found (deduplicated by content hash) -- the Builder must make
    *all* of them pass (Regression Monotonicity Guarantee);
  * every candidate program evaluated, with its verdict -- candidates refuted by the Breaker
    are never proposed again, which prevents CEGIS oscillation between two wrong patches.
"""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
import uuid
from typing import Any, Optional

from .. import codec

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY, label TEXT, created REAL
);
CREATE TABLE IF NOT EXISTS counterexamples (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, target TEXT, property TEXT, message TEXT,
    case_json TEXT, case_hash TEXT, round INTEGER, created REAL,
    UNIQUE(session, target, case_hash)
);
CREATE TABLE IF NOT EXISTS candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, session TEXT, target TEXT, source_hash TEXT, status TEXT,
    edits TEXT, reward REAL, created REAL,
    UNIQUE(session, target, source_hash)
);
CREATE INDEX IF NOT EXISTS ix_cex ON counterexamples(session, target);
CREATE INDEX IF NOT EXISTS ix_cand ON candidates(session, target);
"""


class CounterexampleLedger:
    def __init__(self, path: str = ".specforge/ledger.db") -> None:
        self.path = path
        if path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._lock = threading.Lock()
        self._db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        if path != ":memory:":
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=NORMAL")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        with self._lock:
            self._db.close()

    @property
    def journal_mode(self) -> str:
        return self._db.execute("PRAGMA journal_mode").fetchone()[0]

    def new_session(self, label: str = "") -> str:
        sid = uuid.uuid4().hex[:12]
        with self._lock:
            self._db.execute("INSERT INTO sessions VALUES (?, ?, ?)", (sid, label, time.time()))
        return sid

    # ------------------------------------------------------------------ counterexamples
    def add_counterexample(self, session: str, target: str, prop: str, message: str, case: dict[str, Any],
                           round_: int = 0) -> bool:
        h = codec.stable_hash(case)
        with self._lock:
            cur = self._db.execute(
                "INSERT OR IGNORE INTO counterexamples(session, target, property, message, case_json, case_hash, "
                "round, created) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (session, target, prop, message, codec.dumps(case), h, round_, time.time()))
            return cur.rowcount > 0

    def counterexamples(self, session: str, target: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._db.execute(
                "SELECT property, message, case_json, round FROM counterexamples WHERE session=? AND target=? "
                "ORDER BY id", (session, target)).fetchall()
        return [{"property": p, "message": m, "case": codec.loads(c), "round": r} for p, m, c, r in rows]

    # ------------------------------------------------------------------ candidates
    def record_candidate(self, session: str, target: str, source_hash: str, status: str, edits: list[str],
                         reward: float = 0.0) -> None:
        with self._lock:
            self._db.execute(
                "INSERT INTO candidates(session, target, source_hash, status, edits, reward, created) "
                "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(session, target, source_hash) DO UPDATE SET "
                "status=excluded.status, reward=excluded.reward",
                (session, target, source_hash, status, json.dumps(edits), reward, time.time()))

    def refuted(self, session: str, target: str) -> set[str]:
        with self._lock:
            rows = self._db.execute("SELECT source_hash FROM candidates WHERE session=? AND target=? AND "
                                    "status='refuted'", (session, target)).fetchall()
        return {r[0] for r in rows}

    def candidate_status(self, session: str, target: str, source_hash: str) -> Optional[str]:
        with self._lock:
            row = self._db.execute("SELECT status FROM candidates WHERE session=? AND target=? AND source_hash=?",
                                   (session, target, source_hash)).fetchone()
        return row[0] if row else None

    def stats(self, session: str) -> dict[str, int]:
        with self._lock:
            cex = self._db.execute("SELECT COUNT(*) FROM counterexamples WHERE session=?", (session,)).fetchone()[0]
            cands = self._db.execute("SELECT COUNT(*) FROM candidates WHERE session=?", (session,)).fetchone()[0]
            ref = self._db.execute("SELECT COUNT(*) FROM candidates WHERE session=? AND status='refuted'",
                                   (session,)).fetchone()[0]
        return {"counterexamples": cex, "candidates": cands, "refuted": ref}
