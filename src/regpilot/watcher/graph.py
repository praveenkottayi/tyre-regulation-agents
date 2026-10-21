"""
AGENT 1 (WATCHER) as a LangGraph graph.

    START ─► discover ──found──► check_known ──new/force──► fetch ──changed──► parse ─► store
               │                    │                         │                          │
               └─nothing──┐         └─already complete──┐     └─same hash──┐            ▼
                          ▼                             ▼                  ▼          extract ──ok──────────────► emit ─► finish ─► END
                        finish ◄────────────────────────┴──────────────────┘            │  └─unparsed wording─► propose_criteria ─┘ ▲
                                                                                        └──failed──────────────────────────────────┘

propose_criteria is the only LLM step (LangChain, optional): it reads condition
wording the rules couldn't, and stores PROPOSALS for a human. It never changes
requirements; see watcher/llm_criteria.py.

LangGraph orchestrates; it does not decide anything legal. Every node calls a
plain function (sources/, parsers/, requirements.py, diff.py, db.py) that is
unit-tested on its own, and the routing is explicit conditional edges.

Design choices (from the handoff, made explicit):
  - Requirement VALUES come from the legal text by rule. The LLM (if enabled) only
    proposes readings of wording, and a human approves them before they're used.
  - No checkpointer, no automatic node retries. Safety on re-runs comes from
    the data instead: content hashes skip unchanged versions, documents stay
    'pending' until their event is written, and event ids are deterministic
    (INSERT OR IGNORE). Running the graph twice never duplicates anything.
  - HTTP GETs retry inside core/net.py (idempotent, so safe).
  - Legal text is data. Nothing read from a document is ever used as an instruction.
"""

import hashlib
import operator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, Any, TypedDict

from langgraph.graph import END, START, StateGraph

from regpilot.core.models import EVENT_BACKFILLED, EVENT_NEW_VERSION, EVENT_UPDATED, AgentEvent
from regpilot.watcher import db
from regpilot.watcher.diff import diff
from regpilot.watcher.llm_criteria import Proposer, check, footnote_for, to_criteria
from regpilot.watcher.parsers import oj_xhtml
from regpilot.watcher.requirements import ExtractionError, extract
from regpilot.watcher.sources import RegulationSource, VersionRef, default_sources


class WatcherState(TypedDict, total=False):
    # --- inputs
    regulation: str  # "R117"
    as_of: str | None  # YYYY-MM-DD: watch as if today were this date (versions published after are ignored)
    celex: str | None  # watch exactly this version (used by run_all)
    language: str  # OJ language code: "eng", "deu", ...
    force: bool  # re-fetch even if this publication is already stored
    # --- working state
    target: dict | None  # VersionRef chosen by discover
    known_document_id: int | None
    raw: bytes | None
    content_hash: str | None
    raw_path: str | None
    parsed: Any  # ParsedDocument (in-memory only, never checkpointed)
    document_id: int | None
    requirements: list[dict]
    unparsed: list[dict]  # condition texts the rules couldn't read (+ context)
    status: str  # discovered | unchanged | stored | extracted | extraction_failed | not_found | complete
    started_at: str
    # --- accumulated (reducers: nodes append, never overwrite)
    events: Annotated[list[dict], operator.add]
    messages: Annotated[list[str], operator.add]
    warnings: Annotated[list[str], operator.add]


class Watcher:
    """Holds the dependencies (sources, paths) and builds the graph.
    Pass fake sources in tests: no network needed."""

    def __init__(self, data_dir: Path, sources: list[RegulationSource] | None = None,
                 proposer: Proposer | None = None):
        self.data_dir = Path(data_dir)
        self.db_path = self.data_dir / "watcher.db"
        self.sources = {s.name: s for s in (sources or default_sources())}
        self.proposer = proposer  # None = no LLM step

    # --- nodes ------------------------------------------------------------------------------
    def discover(self, state: WatcherState):
        reg, as_of = state["regulation"].upper(), state.get("as_of")
        versions: list[VersionRef] = []
        for source in self.sources.values():
            versions += source.discover(reg)
        if as_of:
            versions = [v for v in versions if v.date <= as_of]
        if state.get("celex"):
            versions = [v for v in versions if v.source_id == state["celex"]]
        if not versions:
            return {"target": None, "status": "not_found",
                    "messages": [f"{reg}: no published version found" + (f" as of {as_of}" if as_of else "")]}
        target = max(versions, key=lambda v: v.date)
        return {"target": target.model_dump(), "status": "discovered", "started_at": db.now(),
                "messages": [f"{reg}: {len(versions)} version(s) found; latest{' as of ' + as_of if as_of else ''}: "
                             f"{target.source_id} ({target.date})"]}

    def check_known(self, state: WatcherState):
        t = state["target"]
        with db.connect(self.db_path) as con:
            row = db.find_document(con, t["source"], t["source_id"], state["language"])
        if row and row["status"] == "complete" and not state.get("force"):
            return {"status": "unchanged", "known_document_id": row["id"],
                    "messages": [f"{t['source_id']} [{state['language']}] already stored (doc #{row['id']}); "
                                 "not re-fetched (use force to re-check)"]}
        return {"known_document_id": row["id"] if row and row["status"] == "complete" else None}

    def fetch(self, state: WatcherState):
        t = state["target"]
        raw = self.sources[t["source"]].fetch(VersionRef(**t), state["language"])
        h = hashlib.sha256(raw).hexdigest()
        if state.get("known_document_id"):
            with db.connect(self.db_path) as con:
                old = con.execute("SELECT content_hash FROM documents WHERE id=?",
                                  (state["known_document_id"],)).fetchone()
            if old and old["content_hash"] == h:
                return {"status": "unchanged", "messages": [f"{t['source_id']}: re-fetched, content identical"]}
        path = self.data_dir / "raw" / t["source"] / t["source_id"] / f"{h[:16]}.{state['language']}.xhtml"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(raw)
        return {"raw": raw, "content_hash": h, "raw_path": str(path),
                "messages": [f"fetched {t['source_id']} [{state['language']}]: {len(raw):,} bytes -> {path.name}"]}

    def parse(self, state: WatcherState):
        parsed = oj_xhtml.parse(state["raw"])
        return {"parsed": parsed, "raw": None,  # drop the bytes from state once parsed
                "warnings": [f"parser: {w}" for w in parsed.warnings],
                "messages": [f"parsed: {len(parsed.sections)} sections, {len(parsed.definitions)} definitions, "
                             f"{sum(len(s.tables) for s in parsed.sections)} tables"]}

    def store(self, state: WatcherState):
        t = state["target"]
        with db.connect(self.db_path) as con:
            doc_id = db.store_version(
                con, regulation=t["regulation"], source=t["source"], celex=t["source_id"],
                language=state["language"], pub_date=t["date"], title=t["title"], url=t["url"],
                content_hash=state["content_hash"], raw_path=state["raw_path"], parsed=state["parsed"])
        return {"document_id": doc_id, "status": "stored", "messages": [f"stored as document #{doc_id} (pending)"]}

    def extract_requirements(self, state: WatcherState):
        t = state["target"]
        with db.connect(self.db_path) as con:
            approved = db.approved_mappings(con)
        try:
            result = extract(state["parsed"], t["regulation"], celex=t["source_id"], document_id=state["document_id"],
                             approved=approved)
        except ExtractionError as e:
            # Stored, searchable, but NO event: impact must never act on unknown requirements.
            with db.connect(self.db_path) as con:
                db.mark_complete(con, state["document_id"])
            return {"status": "extraction_failed", "warnings": [f"extraction failed: {e}"]}
        with db.connect(self.db_path) as con:
            db.store_requirements(con, state["document_id"], result.requirements)
        return {"requirements": [r.model_dump(mode="json") for r in result.requirements], "status": "extracted",
                "warnings": result.warnings, "unparsed": result.unparsed_conditions(),
                "messages": [f"extracted {len(result.requirements)} requirements"
                             + (f", {len(result.warnings)} warning(s)" if result.warnings else "")]}

    def propose_criteria(self, state: WatcherState):
        """LLM step (graph node): one proposal per condition text not reviewed before."""
        texts = [s.text for s in state["parsed"].sections]
        return {"messages": self._propose(state["unparsed"], texts, state["target"]["source_id"])}

    def propose_for_stored(self, regulation: str, language: str = "eng") -> list[str]:
        """Run the LLM step over versions already stored (no fetch)."""
        msgs = []
        with db.connect(self.db_path) as con:
            docs = con.execute("SELECT * FROM documents WHERE regulation=? AND language=? AND status='complete' "
                               "ORDER BY pub_date", (regulation.upper(), language)).fetchall()
            approved = db.approved_mappings(con)
        for d in docs:
            parsed = oj_xhtml.parse(Path(d["raw_path"]).read_bytes())
            try:
                res = extract(parsed, d["regulation"], celex=d["celex"], document_id=d["id"], approved=approved)
            except ExtractionError:
                continue
            msgs += self._propose(res.unparsed_conditions(), [s.text for s in parsed.sections], d["celex"])
        return msgs

    def _propose(self, unparsed: list[dict], texts: list[str], celex: str) -> list[str]:
        if not self.proposer:
            return ["no LLM configured"]
        with db.connect(self.db_path) as con:
            done = db.reviewed_texts(con)
        msgs = []
        for u in [u for u in unparsed if u["text"] not in done]:
            context = {**u, "celex": celex, "footnote": footnote_for(u["text"], texts)}
            try:
                proposal = self.proposer(u["text"], context)
            except Exception as e:  # model down, malformed output: record, keep going
                proposal, err = None, f"{type(e).__name__}: {e}"[:200]
            else:
                err = None if proposal else "model returned no proposal"
            if proposal is None:  # not stored, so the next run can retry
                msgs.append(f"no usable LLM answer for {u['text']!r} ({err}); will retry next run")
                continue
            issues = check(proposal, u["text"], context["footnote"])
            with db.connect(self.db_path) as con:
                rid = db.add_review(con, u["text"], context, proposal.model_dump(mode="json"),
                                    getattr(self.proposer, "model", "custom"), issues)
            msgs.append(f"criteria review #{rid} proposed for {u['text']!r}"
                        + (f" ({len(issues)} issue(s) for the reviewer)" if issues else " (grounded)"))
        return msgs or ["no new wording to review"]

    def emit(self, state: WatcherState):
        t, doc_id = state["target"], state["document_id"]
        with db.connect(self.db_path) as con:
            same_pub = state.get("known_document_id")  # same CELEX, different content
            if same_pub:
                prev, etype = con.execute("SELECT * FROM documents WHERE id=?", (same_pub,)).fetchone(), EVENT_UPDATED
            else:
                # compare with what this version REPLACED, not with whatever is newest
                prev = db.predecessor(con, t["regulation"], state["language"], t["date"], exclude_id=doc_id)
                older = db.has_newer(con, t["regulation"], state["language"], t["date"])
                etype = EVENT_BACKFILLED if older else EVENT_NEW_VERSION
            new_reqs = db.load_requirements(con, doc_id)
            old_reqs = db.load_requirements(con, prev["id"]) if prev else []
            changes = diff(old_reqs, new_reqs)
            event = AgentEvent(
                id=hashlib.sha256(f"{etype}|{doc_id}|{state['content_hash']}".encode()).hexdigest()[:24],
                type=etype, source_agent="watcher", created_at=datetime.now(timezone.utc),
                payload={
                    "regulation": t["regulation"], "source": t["source"], "celex": t["source_id"],
                    "document_id": doc_id, "language": state["language"], "publication_date": t["date"],
                    "oj_reference": state["parsed"].oj_reference, "content_hash": state["content_hash"],
                    "previous_document_id": prev["id"] if prev else None,
                    "previous_celex": prev["celex"] if prev else None,
                    "requirement_ids": [r.id for r in new_reqs],
                    "unknown_conditions": [u["text"] for u in state.get("unparsed", [])],
                    "changes": changes if prev else None,
                    "warnings": state.get("warnings", []),
                },
            )
            written = db.emit_event(con, event, doc_id)
        summary = (f"+{len(changes['added'])} -{len(changes['removed'])} ~{len(changes['changed'])} vs "
                   f"{prev['celex']}" if prev else "first version seen")
        return {"events": [event.model_dump(mode="json")] if written else [], "status": "complete",
                "messages": [f"event {etype} ({summary})" + ("" if written else " already emitted earlier")]}

    def finish(self, state: WatcherState):
        with db.connect(self.db_path) as con:
            db.log_run(con, state["regulation"], state.get("started_at") or db.now(), state.get("status", "?"), {
                "target": (state.get("target") or {}).get("source_id"), "document_id": state.get("document_id"),
                "events": [e["id"] for e in state.get("events", [])], "warnings": state.get("warnings", []),
            })
        return {}

    # --- routing ------------------------------------------------------------------------------
    @staticmethod
    def after_discover(state):
        return "check_known" if state.get("target") else "finish"

    @staticmethod
    def after_check(state):
        return "finish" if state.get("status") == "unchanged" else "fetch"

    @staticmethod
    def after_fetch(state):
        return "finish" if state.get("status") == "unchanged" else "parse"

    def after_extract(self, state):
        if state.get("status") != "extracted":
            return "finish"
        return "propose_criteria" if (self.proposer and state.get("unparsed")) else "emit"

    # --- graph ------------------------------------------------------------------------------
    def build(self):
        g = StateGraph(WatcherState)
        for name in ("discover", "check_known", "fetch", "parse", "store", "emit", "finish"):
            g.add_node(name, getattr(self, name))
        g.add_node("extract", self.extract_requirements)
        g.add_node("propose_criteria", self.propose_criteria)
        g.add_edge(START, "discover")
        g.add_conditional_edges("discover", self.after_discover, ["check_known", "finish"])
        g.add_conditional_edges("check_known", self.after_check, ["fetch", "finish"])
        g.add_conditional_edges("fetch", self.after_fetch, ["parse", "finish"])
        g.add_edge("parse", "store")
        g.add_edge("store", "extract")
        g.add_conditional_edges("extract", self.after_extract, ["propose_criteria", "emit", "finish"])
        g.add_edge("propose_criteria", "emit")
        g.add_edge("emit", "finish")
        g.add_edge("finish", END)
        return g.compile()

    def run(self, regulation: str, *, as_of: str | date | None = None, language: str = "eng",
            force: bool = False, celex: str | None = None) -> WatcherState:
        return self.build().invoke({
            "regulation": regulation.upper(), "as_of": str(as_of) if as_of else None,
            "language": language, "force": force, "celex": celex,
        })

    def reprocess(self, document_id: int) -> dict | None:
        """Re-extract a stored version from its raw file (after a criteria review
        was approved). Emits regulation.updated if the current version changed.
        Deterministic: no fetch, no LLM."""
        from regpilot.core.models import EVENT_UPDATED as UPDATED

        with db.connect(self.db_path) as con:
            doc = con.execute("SELECT * FROM documents WHERE id=?", (document_id,)).fetchone()
            parsed = oj_xhtml.parse(Path(doc["raw_path"]).read_bytes())
            old = db.load_requirements(con, document_id)
            res = extract(parsed, doc["regulation"], celex=doc["celex"], document_id=document_id,
                          approved=db.approved_mappings(con))
            changes = diff(old, res.requirements)
            if not (changes["added"] or changes["removed"] or changes["changed"]):
                return None
            db.store_requirements(con, document_id, res.requirements)
            if db.has_newer(con, doc["regulation"], doc["language"], doc["pub_date"]):
                return {"document_id": document_id, "changes": changes, "event": None}  # history: no event
            fp = hashlib.sha256("".join(r.fingerprint() for r in res.requirements).encode()).hexdigest()
            event = AgentEvent(
                id=hashlib.sha256(f"{UPDATED}|{document_id}|reviewed|{fp}".encode()).hexdigest()[:24],
                type=UPDATED, source_agent="watcher", created_at=datetime.now(timezone.utc),
                payload={"regulation": doc["regulation"], "source": doc["source"], "celex": doc["celex"],
                         "document_id": document_id, "language": doc["language"], "publication_date": doc["pub_date"],
                         "reason": "criteria_reviewed", "previous_document_id": document_id,
                         "previous_celex": doc["celex"], "requirement_ids": [r.id for r in res.requirements],
                         "unknown_conditions": [u["text"] for u in res.unparsed_conditions()], "changes": changes})
            written = db.emit_event(con, event, document_id)
        return {"document_id": document_id, "changes": changes, "event": event if written else None}

    def run_all(self, regulation: str, *, language: str = "eng", force: bool = False) -> list[WatcherState]:
        """Store EVERY published version, oldest first. Versions older than the
        newest stored one emit regulation.backfilled (history), not new_version."""
        versions = sorted((v for s in self.sources.values() for v in s.discover(regulation.upper())),
                          key=lambda v: (v.date, v.source_id))
        return [self.run(regulation, language=language, force=force, celex=v.source_id) for v in versions]
