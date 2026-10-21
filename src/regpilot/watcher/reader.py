"""
The ONLY way Agent 2 (Reg-Impact) should read the watcher's data. Read-only by
construction, and it returns the contract models, not raw rows.

    reader = WatcherReader("data/watcher.db")
    for event in reader.events(since=last_seen):          # what's new?
        reqs = reader.requirements(event.payload["document_id"])
        ...                                                # assess against products
    reader.current_requirements("R117")                    # or: just the latest version
"""

import json
from datetime import datetime
from pathlib import Path

from regpilot.core.models import EVENT_NEW_VERSION, EVENT_UPDATED, AgentEvent, Requirement
from regpilot.watcher import db


class WatcherReader:
    def __init__(self, db_path: str | Path = "data/watcher.db"):
        self.con = db.connect_readonly(Path(db_path))

    def events(self, since: str | None = None,
               types: tuple[str, ...] = (EVENT_NEW_VERSION, EVENT_UPDATED)) -> list[AgentEvent]:
        """Events in creation order, optionally only those after `since` (ISO timestamp).
        Agent 2 remembers the created_at of the last event it processed."""
        marks = ",".join("?" * len(types))
        rows = self.con.execute(
            f"SELECT * FROM events WHERE type IN ({marks}) AND created_at > ? ORDER BY created_at, id",
            (*types, since or ""))
        return [AgentEvent(id=r["id"], type=r["type"], source_agent="watcher", payload=json.loads(r["payload"]),
                           created_at=datetime.fromisoformat(r["created_at"])) for r in rows]

    def document(self, document_id: int) -> dict | None:
        row = self.con.execute("SELECT id, regulation, source, celex, language, pub_date, title, oj_reference, url, "
                               "status FROM documents WHERE id=?", (document_id,)).fetchone()
        return dict(row) if row else None

    def requirements(self, document_id: int) -> list[Requirement]:
        """Requirements of one specific version (e.g. the one an event points to)."""
        doc = self.document(document_id)
        if not doc or doc["status"] != "complete":
            raise LookupError(f"document #{document_id} is not a complete watcher version")
        return db.load_requirements(self.con, document_id)

    def current_requirements(self, regulation: str, language: str = "eng") -> list[Requirement]:
        rows = self.con.execute("SELECT data FROM current_requirements WHERE regulation=? AND language=? "
                                "ORDER BY requirement_id", (regulation.upper(), language))
        return [Requirement.model_validate_json(r["data"]) for r in rows]

    def close(self):
        self.con.close()
