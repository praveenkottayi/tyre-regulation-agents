"""
impact.db: Agent 2's own database (separate from watcher.db, which it only reads).

  proposals   one row per impact proposal (full Proposal JSON in `data`)
              status: proposed -> edited -> approved | rejected
  gate1_log   every human action on a proposal (audit trail, append-only)
  meta        e.g. the last watcher event processed (cursor)
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from regpilot.impact.models import Proposal

SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id           TEXT PRIMARY KEY,      -- deterministic: same trigger + inputs -> same id
    regulation   TEXT NOT NULL,
    celex        TEXT,
    document_id  INTEGER,
    trigger      TEXT,                  -- watcher event id or 'manual'
    as_of        TEXT,
    status       TEXT NOT NULL DEFAULT 'proposed',
    fails        INTEGER, unresolved INTEGER, passes INTEGER,
    data         TEXT NOT NULL,         -- full Proposal JSON (reviewer edits applied)
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS gate1_log (
    id INTEGER PRIMARY KEY, proposal_id TEXT NOT NULL, action TEXT NOT NULL,
    reviewer TEXT NOT NULL, notes TEXT, edits TEXT, at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.executescript(SCHEMA)
    return con


def save_proposal(con, p: Proposal) -> bool:
    """False if this exact proposal already exists (re-run): never duplicated or overwritten."""
    s = p.summary
    with con:
        cur = con.execute(
            "INSERT OR IGNORE INTO proposals (id, regulation, celex, document_id, trigger, as_of, status, fails, "
            "unresolved, passes, data, created_at) VALUES (?,?,?,?,?,?,'proposed',?,?,?,?,?)",
            (p.id, p.regulation, p.celex, p.document_id, p.trigger, p.as_of.isoformat(), s.get("fail", 0),
             s.get("unresolved", 0), s.get("pass", 0), p.model_dump_json(), p.created_at.isoformat()))
    return cur.rowcount == 1


def load_proposal(con, proposal_id: str) -> tuple[Proposal, str]:
    row = con.execute("SELECT data, status FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        raise LookupError(f"no proposal {proposal_id}")
    return Proposal.model_validate_json(row["data"]), row["status"]


def get_meta(con, key: str, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(con, key: str, value: str):
    with con:
        con.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))


def log(con, proposal_id: str, action: str, reviewer: str, notes: str | None, edits: list | None):
    con.execute("INSERT INTO gate1_log (proposal_id, action, reviewer, notes, edits, at) VALUES (?,?,?,?,?,?)",
                (proposal_id, action, reviewer, notes, json.dumps(edits or [], ensure_ascii=False), now()))
