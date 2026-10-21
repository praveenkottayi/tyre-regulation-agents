"""
ImpactAgent: the regpilot Agent contract (run(context) -> AgentResult) with
the LangGraph graph from impact/graph.py inside.

What triggers an assessment (first match wins):
  1. context["events"]       watcher events already in the shared context
                             (Orchestrator: watcher runs first, impact reacts)
  2. context["assess"]       explicit request, e.g. ["R117"]: assess the current version
                             (separate from the watcher's "regulations" key, so in a combined
                             run impact only reacts to what the watcher reported)
  3. otherwise               new watcher events since the last one processed (cursor in impact.db)

Only regulation.new_version and regulation.updated trigger an assessment.
regulation.backfilled is history and is ignored.

context keys: data_dir (default "data"), products (default "sample_data/products"), as_of (default today)
"""

from datetime import date
from pathlib import Path
from typing import Any

from regpilot.core.models import EVENT_NEW_VERSION, EVENT_UPDATED, AgentEvent, AgentResult
from regpilot.impact import store
from regpilot.impact.graph import ImpactAssessor
from regpilot.watcher.reader import WatcherReader

TRIGGERS = (EVENT_NEW_VERSION, EVENT_UPDATED)


class ImpactAgent:
    name = "impact"

    def __init__(self, judge=None):
        self.judge = judge

    def run(self, context: dict[str, Any]) -> AgentResult:
        data_dir = Path(context.get("data_dir", "data"))
        assessor = ImpactAssessor(data_dir, Path(context.get("products", "sample_data/products")), self.judge)
        as_of = context.get("as_of") or date.today().isoformat()
        result = AgentResult(agent=self.name, ok=True)

        events = [e for e in context.get("events", []) if e.type in TRIGGERS]
        use_cursor = not events and not context.get("assess")
        if use_cursor:
            with store.connect(data_dir / "impact.db") as con:
                since = store.get_meta(con, "last_watcher_event")
            reader = WatcherReader(data_dir / "watcher.db")
            events = reader.events(since=since)
            reader.close()

        # Several pending events for one regulation (e.g. after downtime): only the newest
        # published version matters; assessing an outdated one would mislead.
        latest: dict[str, AgentEvent] = {}
        for e in events:
            cur = latest.get(e.payload["regulation"])
            if cur is None or (e.payload.get("publication_date") or "", e.created_at) >= \
                    (cur.payload.get("publication_date") or "", cur.created_at):
                latest[e.payload["regulation"]] = e
        skipped = [e for e in events if e not in latest.values()]
        if skipped:
            result.messages.append(f"skipped {len(skipped)} outdated event(s): "
                                   + ", ".join(e.payload["celex"] for e in skipped))
        jobs = [(e.payload["regulation"], e.payload["document_id"], e.id, e.payload.get("changes"))
                for e in latest.values()]
        jobs += [(r, None, "manual", None) for r in context.get("assess", []) if not events]
        if not jobs:
            result.messages.append("nothing to assess (no new watcher events)")

        for regulation, document_id, trigger, changes in jobs:
            try:
                state = assessor.run(regulation, document_id=document_id, trigger=trigger, changes=changes, as_of=as_of)
            except Exception as e:
                result.ok = False
                result.errors.append(f"{regulation} ({trigger}): {type(e).__name__}: {e}")
                continue
            result.messages += state.get("messages", [])
            if state.get("event"):
                result.events.append(AgentEvent.model_validate(state["event"]))

        if events and result.ok:  # move the cursor however the events arrived (context or catch-up)
            newest = max(e.created_at for e in events).isoformat()
            with store.connect(data_dir / "impact.db") as con:
                if newest > (store.get_meta(con, "last_watcher_event") or ""):
                    store.set_meta(con, "last_watcher_event", newest)
        return result
