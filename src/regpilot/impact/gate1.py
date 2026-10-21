"""
Gate 1: a person approves, edits or rejects an impact proposal.

    proposed ──edit──► edited ──approve──► approved
        │                 │
        └──approve────────┤
        └──reject─────────┴──► rejected

Rules:
  - Every action needs the reviewer's name and is appended to gate1_log (audit trail).
  - Edits override a single verdict (size × requirement) and REQUIRE a reason.
    The agent's original verdict is kept next to the reviewer's.
  - Approved or rejected proposals are final: no further edits.
  - Rejecting needs notes (why).
"""

import json
from pathlib import Path

from pydantic import BaseModel

from regpilot.impact import store
from regpilot.impact.report import summarize, write_artifacts

REVIEWER_VERDICTS = {"pass", "fail", "accepted_risk", "not_applicable", "unknown"}
OPEN = ("proposed", "edited")


class Edit(BaseModel):
    size_id: str
    requirement_id: str
    verdict: str
    reason: str


class Gate1Error(ValueError):
    pass


def _validate(edits: list[Edit], proposal) -> None:
    keys = {(i.size_id, i.requirement_id) for i in proposal.impacts}
    for e in edits:
        if (e.size_id, e.requirement_id) not in keys:
            raise Gate1Error(f"no assessment for size {e.size_id} × {e.requirement_id} in this proposal")
        if e.verdict not in REVIEWER_VERDICTS:
            raise Gate1Error(f"verdict must be one of {sorted(REVIEWER_VERDICTS)}, got {e.verdict!r}")
        if len(e.reason.strip()) < 10:
            raise Gate1Error("every edit needs a reason (at least 10 characters) for the audit trail")


def _finish(con, data_dir: Path, proposal, status: str, action: str, reviewer: str, notes, edits):
    if not reviewer.strip():
        raise Gate1Error("reviewer name is required")
    proposal.summary = summarize(proposal.impacts)
    s = proposal.summary
    with con:
        con.execute("UPDATE proposals SET status=?, data=?, fails=?, unresolved=?, passes=? WHERE id=?",
                    (status, proposal.model_dump_json(), s["fail"], s["unresolved"], s["pass"], proposal.id))
        store.log(con, proposal.id, action, reviewer, notes, [e.model_dump() for e in edits] if edits else None)
    folder = write_artifacts(proposal, data_dir / "gate1", status)
    history = [dict(r) for r in con.execute("SELECT action, reviewer, notes, edits, at FROM gate1_log "
                                            "WHERE proposal_id=? ORDER BY id", (proposal.id,))]
    (folder / "review.json").write_text(json.dumps({"proposal_id": proposal.id, "status": status,
                                                    "history": history}, indent=1, ensure_ascii=False))
    return proposal


def edit(data_dir: Path, proposal_id: str, reviewer: str, edits: list[Edit], notes: str | None = None):
    with store.connect(data_dir / "impact.db") as con:
        p, status = store.load_proposal(con, proposal_id)
        if status not in OPEN:
            raise Gate1Error(f"proposal {proposal_id} is {status}; decided proposals cannot be edited")
        _validate(edits, p)
        for e in edits:
            for i in p.impacts:
                if (i.size_id, i.requirement_id) == (e.size_id, e.requirement_id):
                    i.reviewer_verdict, i.reviewer_reason = e.verdict, e.reason.strip()
        return _finish(con, data_dir, p, "edited", "edit", reviewer, notes, edits)


def approve(data_dir: Path, proposal_id: str, reviewer: str, notes: str | None = None):
    with store.connect(data_dir / "impact.db") as con:
        p, status = store.load_proposal(con, proposal_id)
        if status not in OPEN:
            raise Gate1Error(f"proposal {proposal_id} is already {status}")
        return _finish(con, data_dir, p, "approved", "approve", reviewer, notes, None)


def reject(data_dir: Path, proposal_id: str, reviewer: str, notes: str):
    if not (notes or "").strip():
        raise Gate1Error("rejecting needs notes explaining why")
    with store.connect(data_dir / "impact.db") as con:
        p, status = store.load_proposal(con, proposal_id)
        if status not in OPEN:
            raise Gate1Error(f"proposal {proposal_id} is already {status}")
        return _finish(con, data_dir, p, "rejected", "reject", reviewer, notes, None)
