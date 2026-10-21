"""
watcher.db: what Agent 1 knows. Agent 2 reads it through watcher/reader.py (read-only).

  documents     one row per fetched version (status: pending -> complete)
  sections      clause text, with its tables as JSON (for clause/outline/search)
  requirements  one row per requirement (readable columns + full JSON in `data`)
  limits        one row per limit value: what products are rated against
  events        hand-off signals for Agent 2 (deterministic ids, never duplicated)
  runs          one row per watcher run
  criteria_reviews  LLM-proposed readings of conditions the rules couldn't parse,
                    and the human decision. Only APPROVED rows are ever used.

  views: current_requirements, current_limits = the latest complete version
         of each regulation, so consumers never pick a version themselves.
"""

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from regpilot.core.models import AgentEvent, Requirement
from regpilot.watcher.parsers.oj_xhtml import ParsedDocument

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    id            INTEGER PRIMARY KEY,
    regulation    TEXT NOT NULL,     -- 'R117'
    source        TEXT NOT NULL,     -- 'eurlex'
    celex         TEXT NOT NULL,     -- '42025X1453'
    language      TEXT NOT NULL,     -- 'eng'
    pub_date      TEXT,              -- '2025-08-07'
    title         TEXT,
    oj_reference  TEXT,              -- '2025/1453'
    url           TEXT,
    content_hash  TEXT NOT NULL,
    raw_path      TEXT NOT NULL,     -- data/raw/... copy of the fetched XHTML
    status        TEXT NOT NULL DEFAULT 'pending',  -- pending -> complete
    fetched_at    TEXT NOT NULL,
    UNIQUE (source, celex, language, content_hash)
);
CREATE TABLE IF NOT EXISTS sections (
    document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    key         TEXT NOT NULL,       -- 'main:6.1.1', 'annex-5:2.1'
    number      TEXT,
    heading     TEXT,
    text        TEXT,                -- verbatim; '[table N]' marks where table N sits
    tables      TEXT,                -- JSON: [{"header_rows": 2, "rows": [[...], ...]}, ...]
    ord         INTEGER,
    PRIMARY KEY (document_id, key)
);
CREATE TABLE IF NOT EXISTS requirements (
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    requirement_id  TEXT NOT NULL,   -- 'R117:6.3:C1:stage-3' (stable across versions)
    regulation      TEXT NOT NULL,
    clause          TEXT,            -- '6.3' (as numbered in this version)
    metric          TEXT,            -- 'rolling_resistance_coefficient'
    unit            TEXT,            -- 'N/kN'
    comparator      TEXT,            -- '<=' or '>='
    tyre_class      TEXT,            -- 'C1'
    stage           TEXT,            -- '3' (NULL if the table prints no stage)
    previous_stage  TEXT,
    applies_from    TEXT,            -- earliest applicability date, if any (details in data)
    fingerprint     TEXT,            -- changes iff the legal content changes
    data            TEXT NOT NULL,   -- full Requirement JSON (quotes, adjustments, dates)
    PRIMARY KEY (document_id, requirement_id)
);
CREATE TABLE IF NOT EXISTS limits (
    document_id     INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
    requirement_id  TEXT NOT NULL,
    row_no          INTEGER NOT NULL,
    regulation      TEXT, metric TEXT, unit TEXT, tyre_class TEXT, stage TEXT,
    conditions      TEXT,            -- 'load capacity index ≥ 87 / Tyres other than Run Flat ...'
    comparator      TEXT,            -- '<='
    value           REAL,            -- 9.0 (NULL = 'Not defined' in the regulation)
    raw_value       TEXT,            -- '9,0' exactly as printed
    criteria        TEXT,            -- JSON, machine-readable conditions: {"load_index": {"min": 87}, "run_flat": false}
    PRIMARY KEY (document_id, requirement_id, row_no)
);
CREATE TABLE IF NOT EXISTS events (
    id           TEXT PRIMARY KEY,   -- deterministic: re-runs never duplicate
    type         TEXT NOT NULL,      -- 'regulation.new_version' | 'regulation.updated'
    regulation   TEXT,
    document_id  INTEGER,
    payload      TEXT NOT NULL,      -- JSON: ids, previous version, changes
    created_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS criteria_reviews (
    id              INTEGER PRIMARY KEY,
    condition_text  TEXT NOT NULL UNIQUE,   -- verbatim, e.g. 'Snow (*1)'
    context         TEXT,                   -- JSON: regulation, clause, class, footnote, ...
    proposal        TEXT,                   -- JSON: what the LLM proposed (incl. explanation)
    model           TEXT,                   -- e.g. 'ollama:gemma4:12b'
    checks          TEXT,                   -- JSON list of problems the code found ([] = grounded)
    status          TEXT NOT NULL DEFAULT 'proposed',   -- proposed | approved | rejected
    final_criteria  TEXT,                   -- JSON Criteria actually used (approved rows only)
    decided_by      TEXT,
    decided_at      TEXT,
    created_at      TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY, regulation TEXT, started_at TEXT, finished_at TEXT,
    status TEXT, summary TEXT
);

-- The latest complete version per regulation + language.
CREATE VIEW IF NOT EXISTS current_documents AS
    SELECT d.* FROM documents d
    WHERE d.status = 'complete' AND d.id = (
        SELECT d2.id FROM documents d2
        WHERE d2.regulation = d.regulation AND d2.language = d.language AND d2.status = 'complete'
        ORDER BY d2.pub_date DESC, d2.id DESC LIMIT 1);
CREATE VIEW IF NOT EXISTS current_requirements AS
    SELECT r.*, d.celex, d.language, d.pub_date FROM requirements r JOIN current_documents d ON d.id = r.document_id;
CREATE VIEW IF NOT EXISTS current_limits AS
    SELECT l.*, d.celex, d.language, d.pub_date FROM limits l JOIN current_documents d ON d.id = l.document_id;
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.executescript(SCHEMA)
    return con


def connect_readonly(path: Path) -> sqlite3.Connection:
    """For query commands and for Agent 2: cannot write by construction."""
    if not path.exists():
        raise FileNotFoundError(f"{path} does not exist yet. Run `regpilot watch` first")
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


# --- reads -------------------------------------------------------------------------------
def find_document(con, source: str, celex: str, language: str) -> sqlite3.Row | None:
    """Latest stored copy of one publication in one language."""
    return con.execute("SELECT * FROM documents WHERE source=? AND celex=? AND language=? ORDER BY id DESC LIMIT 1",
                       (source, celex, language)).fetchone()


def current_document(con, regulation: str, language: str = "eng", exclude_id: int | None = None):
    """The newest COMPLETE version of a regulation (by publication date)."""
    return con.execute(
        "SELECT * FROM documents WHERE regulation=? AND language=? AND status='complete' AND id IS NOT ? "
        "ORDER BY pub_date DESC, id DESC LIMIT 1", (regulation, language, exclude_id)).fetchone()


def predecessor(con, regulation: str, language: str, pub_date: str, exclude_id: int):
    """The newest complete version published BEFORE pub_date (what this version replaced)."""
    return con.execute(
        "SELECT * FROM documents WHERE regulation=? AND language=? AND status='complete' AND id != ? "
        "AND pub_date < ? ORDER BY pub_date DESC, id DESC LIMIT 1", (regulation, language, exclude_id, pub_date)).fetchone()


def has_newer(con, regulation: str, language: str, pub_date: str) -> bool:
    return con.execute("SELECT 1 FROM documents WHERE regulation=? AND language=? AND status='complete' "
                       "AND pub_date > ? LIMIT 1", (regulation, language, pub_date)).fetchone() is not None


def load_requirements(con, document_id: int) -> list[Requirement]:
    rows = con.execute("SELECT data FROM requirements WHERE document_id=? ORDER BY requirement_id", (document_id,))
    return [Requirement.model_validate_json(r["data"]) for r in rows]


# --- writes (each one is a single transaction) ----------------------------------------------
def store_version(con, *, regulation, source, celex, language, pub_date, title, url,
                  content_hash, raw_path, parsed: ParsedDocument) -> int:
    with con:
        # A 'pending' leftover from a crashed run with the same content is replaced.
        con.execute("DELETE FROM documents WHERE source=? AND celex=? AND language=? AND content_hash=? "
                    "AND status='pending'", (source, celex, language, content_hash))
        doc_id = con.execute(
            "INSERT INTO documents (regulation, source, celex, language, pub_date, title, oj_reference, url, "
            "content_hash, raw_path, status, fetched_at) VALUES (?,?,?,?,?,?,?,?,?,?,'pending',?)",
            (regulation, source, celex, language, pub_date, title or parsed.title, parsed.oj_reference, url,
             content_hash, str(raw_path), now())).lastrowid
        con.executemany("INSERT INTO sections VALUES (?,?,?,?,?,?,?)", [
            (doc_id, s.key, s.number, s.heading, s.text,
             json.dumps([{"header_rows": t.header_rows, "rows": t.rows} for t in s.tables], ensure_ascii=False)
             if s.tables else None, s.order)
            for s in parsed.sections])
    return doc_id


def store_requirements(con, document_id: int, requirements: list[Requirement]):
    with con:
        con.execute("DELETE FROM requirements WHERE document_id=?", (document_id,))
        con.execute("DELETE FROM limits WHERE document_id=?", (document_id,))
        for r in requirements:
            applies = min((str(a.date) for a in r.applicability), default=None)
            con.execute("INSERT INTO requirements VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (document_id, r.id, r.regulation, r.clause, r.metric, r.unit, r.comparator, r.tyre_class,
                         r.stage, r.previous_stage, applies, r.fingerprint(), r.model_dump_json()))
            con.executemany("INSERT INTO limits VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)", [
                (document_id, r.id, i, r.regulation, r.metric, r.unit, r.tyre_class, r.stage,
                 " / ".join(lr.conditions) or None, lr.comparator, lr.value, lr.raw_value,
                 lr.criteria.model_dump_json(exclude_none=True, exclude_defaults=True))
                for i, lr in enumerate(r.limits, 1)])


def emit_event(con, event: AgentEvent, document_id: int) -> bool:
    """Write the event and mark the document complete, atomically.
    Returns False if this event already existed (idempotent re-run)."""
    with con:
        cur = con.execute("INSERT OR IGNORE INTO events VALUES (?,?,?,?,?,?)",
                          (event.id, event.type, event.payload.get("regulation"), document_id,
                           json.dumps(event.payload, ensure_ascii=False, default=str), event.created_at.isoformat()))
        con.execute("UPDATE documents SET status='complete' WHERE id=?", (document_id,))
    return cur.rowcount == 1


def mark_complete(con, document_id: int):
    with con:
        con.execute("UPDATE documents SET status='complete' WHERE id=?", (document_id,))


def approved_mappings(con) -> dict:
    """condition text -> Criteria, for human-approved reviews only."""
    from regpilot.core.models import Criteria

    return {r["condition_text"]: Criteria.model_validate_json(r["final_criteria"])
            for r in con.execute("SELECT condition_text, final_criteria FROM criteria_reviews WHERE status='approved'")}


def reviewed_texts(con) -> set[str]:
    return {r[0] for r in con.execute("SELECT condition_text FROM criteria_reviews")}


def add_review(con, text: str, context: dict, proposal: dict, model: str, checks: list[str]) -> int | None:
    with con:
        cur = con.execute(
            "INSERT OR IGNORE INTO criteria_reviews (condition_text, context, proposal, model, checks, created_at) "
            "VALUES (?,?,?,?,?,?)", (text, json.dumps(context, ensure_ascii=False), json.dumps(proposal, ensure_ascii=False),
                                     model, json.dumps(checks, ensure_ascii=False), now()))
    return cur.lastrowid if cur.rowcount else None


def decide_review(con, review_id: int, status: str, by: str, final_criteria: dict | None):
    with con:
        con.execute("UPDATE criteria_reviews SET status=?, decided_by=?, decided_at=?, final_criteria=? WHERE id=?",
                    (status, by, now(), json.dumps(final_criteria) if final_criteria is not None else None, review_id))


def log_run(con, regulation: str, started_at: str, status: str, summary: dict):
    with con:
        con.execute("INSERT INTO runs (regulation, started_at, finished_at, status, summary) VALUES (?,?,?,?,?)",
                    (regulation, started_at, now(), status, json.dumps(summary, ensure_ascii=False, default=str)))
