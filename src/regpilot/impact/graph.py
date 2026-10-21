"""
AGENT 2 (REG-IMPACT) as a LangGraph graph. One run = one trigger (a watcher
event, or a manual request for a regulation).

    START ─► load_inputs ──no requirements──► END
                 │
                 ▼
            judge_markets ─► rate_sizes ─► build_proposal ─► store ─► END
            (where does it    (limit row ·    (summary,        (impact.db, data/gate1/<id>/,
             apply?)           timing · test   deterministic    impact.proposed event)
                               · verdict)      id)

Inputs: watcher.db through WatcherReader (read-only) and the product CSVs.
Neither is ever written. The output is a PROPOSAL: a person decides at Gate 1
(impact/gate1.py), outside this graph.

No LLM: market applicability is rule-based (impact/judge.py) and rating is
pure arithmetic on the watcher's exact limits (impact/rating.py).
"""

import hashlib
import json
import operator
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from regpilot.core.models import AgentEvent, Requirement
from regpilot.impact import store
from regpilot.impact.judge import ApplicabilityJudge, RuleBasedJudge
from regpilot.impact.models import MarketApplicability, Proposal, SizeImpact
from regpilot.impact.products import load_catalogue
from regpilot.impact.rating import rate, superseded_by
from regpilot.impact.report import summarize, write_artifacts
from regpilot.watcher.reader import WatcherReader

EVENT_IMPACT_PROPOSED = "impact.proposed"


class ImpactState(TypedDict, total=False):
    # inputs
    regulation: str
    document_id: int | None  # None = current version
    trigger: str  # watcher event id or "manual"
    changes: dict | None  # from the watcher event: added / removed / changed
    as_of: str
    # working
    document: dict
    requirements: list[dict]
    catalogue: list[dict]
    applicability: list[dict]
    assessable: dict  # range_id -> markets where the regulation applies
    unassessed: list[dict]
    superseded: dict  # requirement id -> the later stage that replaced it (as of the date)
    impacts: list[dict]
    proposal: dict
    status: str
    event: dict | None
    messages: Annotated[list[str], operator.add]


class ImpactAssessor:
    def __init__(self, data_dir: Path, products_dir: Path, judge: ApplicabilityJudge | None = None):
        self.data_dir = Path(data_dir)
        self.products_dir = Path(products_dir)
        self.judge = judge or RuleBasedJudge()

    # --- nodes ---------------------------------------------------------------------------------
    def load_inputs(self, state: ImpactState):
        reader = WatcherReader(self.data_dir / "watcher.db")
        try:
            if state.get("document_id"):
                doc = reader.document(state["document_id"])
                reqs = reader.requirements(state["document_id"])
            else:
                reqs = reader.current_requirements(state["regulation"])
                doc = reader.document(reqs[0].quotes[0].document_id) if reqs and reqs[0].quotes else None
        finally:
            reader.close()
        if not reqs or not doc:
            return {"status": "no_requirements", "messages": [f"{state['regulation']}: no stored requirements to assess"]}
        catalogue = load_catalogue(self.products_dir)
        return {"document": doc, "requirements": [r.model_dump(mode="json") for r in reqs], "catalogue": catalogue,
                "status": "loaded",
                "messages": [f"{doc['regulation']} {doc['celex']}: {len(reqs)} requirements × {len(catalogue)} sizes"]}

    def judge_markets(self, state: ImpactState):
        doc = state["document"]
        markets = sorted({m for t in state["catalogue"] for m in t["markets"]})
        verdicts = {m: self.judge.assess(doc["regulation"], doc["source"], m) for m in markets}
        assessable, unassessed = {}, {}
        for t in state["catalogue"]:
            applies = [m for m in t["markets"] if verdicts[m].status == "applies"]
            if applies:
                assessable[t["range_id"]] = applies
            else:
                unassessed[t["range_id"]] = {"brand": t["brand"], "range_name": t["range_name"], "markets": t["markets"]}
        return {"applicability": [v.model_dump() for v in verdicts.values()], "assessable": assessable,
                "unassessed": list(unassessed.values()),
                "messages": [f"markets: " + ", ".join(f"{m}={v.status}" for m, v in verdicts.items())]}

    def rate_sizes(self, state: ImpactState):
        as_of = date.fromisoformat(state["as_of"])
        changes = state.get("changes")
        all_reqs = [Requirement.model_validate(r) for r in state["requirements"]]
        superseded = {r.id: by for r in all_reqs if (by := superseded_by(r, all_reqs, as_of))}
        reqs = [r for r in all_reqs if r.id not in superseded]
        impacts = []
        for tyre in state["catalogue"]:
            markets = state["assessable"].get(tyre["range_id"])
            if not markets:
                continue
            for req in reqs:
                if req.tyre_class != tyre["tyre_class"]:
                    continue
                change = None
                if changes:
                    change = ("added" if req.id in changes.get("added", []) else
                              "changed" if req.id in changes.get("changed", {}) else "unchanged")
                r = rate(req, tyre, as_of)
                impacts.append(SizeImpact(
                    size_id=tyre["size_id"], brand=tyre["brand"], range_name=tyre["range_name"],
                    designation=tyre["designation"], tyre_class=tyre["tyre_class"], markets=markets,
                    requirement_id=req.id, metric=req.metric, unit=req.unit, stage=req.stage,
                    requirement_change=change, **r).model_dump(mode="json"))
        return {"impacts": impacts, "superseded": superseded,
                "messages": [f"rated {len(impacts)} size × requirement combinations"
                             + (f"; skipped superseded stages: {sorted(superseded)}" if superseded else "")]}

    def build_proposal(self, state: ImpactState):
        doc, impacts = state["document"], [SizeImpact.model_validate(i) for i in state["impacts"]]
        catalogue_hash = hashlib.sha256(json.dumps(state["catalogue"], sort_keys=True, default=str).encode()).hexdigest()
        pid = hashlib.sha256(f"{doc['id']}|{state['trigger']}|{state['as_of']}|{catalogue_hash}".encode()).hexdigest()[:12]
        p = Proposal(id=pid, regulation=doc["regulation"], celex=doc["celex"], document_id=doc["id"],
                     trigger=state["trigger"], as_of=state["as_of"], created_at=datetime.now(timezone.utc),
                     applicability=[MarketApplicability.model_validate(a) for a in state["applicability"]],
                     impacts=impacts, unassessed_ranges=state["unassessed"], superseded=state.get("superseded", {}),
                     summary=summarize(impacts))
        s = p.summary
        return {"proposal": p.model_dump(mode="json"),
                "messages": [f"proposal {pid}: {s['fail']} fail · {s['unresolved']} not proven compliant · {s['pass']} pass"]}

    def store_proposal(self, state: ImpactState):
        p = Proposal.model_validate(state["proposal"])
        with store.connect(self.data_dir / "impact.db") as con:
            new = store.save_proposal(con, p)
        if not new:  # keep the stored one (and any Gate 1 edits) untouched
            return {"status": "exists", "event": None,
                    "messages": [f"proposal {p.id} already exists (same trigger and inputs), not duplicated"]}
        folder = write_artifacts(p, self.data_dir / "gate1")
        event = AgentEvent(
            id=hashlib.sha256(f"{EVENT_IMPACT_PROPOSED}|{p.id}".encode()).hexdigest()[:24],
            type=EVENT_IMPACT_PROPOSED, source_agent="impact", created_at=datetime.now(timezone.utc),
            payload={"proposal_id": p.id, "regulation": p.regulation, "celex": p.celex, "as_of": str(p.as_of),
                     "summary": p.summary, "folder": str(folder), "proposal": p.model_dump(mode="json")})
        return {"status": "proposed", "event": event.model_dump(mode="json"),
                "messages": [f"stored -> {folder} (awaiting Gate 1)"]}

    # --- graph --------------------------------------------------------------------------------------
    def build(self):
        g = StateGraph(ImpactState)
        g.add_node("load_inputs", self.load_inputs)
        g.add_node("judge_markets", self.judge_markets)
        g.add_node("rate_sizes", self.rate_sizes)
        g.add_node("build_proposal", self.build_proposal)
        g.add_node("store", self.store_proposal)
        g.add_edge(START, "load_inputs")
        g.add_conditional_edges("load_inputs", lambda s: "judge_markets" if s["status"] == "loaded" else END,
                                ["judge_markets", END])
        g.add_edge("judge_markets", "rate_sizes")
        g.add_edge("rate_sizes", "build_proposal")
        g.add_edge("build_proposal", "store")
        g.add_edge("store", END)
        return g.compile()

    def run(self, regulation: str, *, document_id: int | None = None, trigger: str = "manual",
            changes: dict | None = None, as_of: str | date | None = None) -> ImpactState:
        return self.build().invoke({"regulation": regulation.upper(), "document_id": document_id, "trigger": trigger,
                                    "changes": changes, "as_of": str(as_of or date.today())})
