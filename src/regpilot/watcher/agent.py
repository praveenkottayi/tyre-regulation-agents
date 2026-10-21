"""
WatcherAgent: the regpilot Agent contract (run(context) -> AgentResult)
implemented with the LangGraph graph from watcher/graph.py.

context keys read:
    regulations: list[str]   e.g. ["R117"]
    as_of:       str | None  YYYY-MM-DD
    language:    str         default "eng"
    force:       bool        re-fetch even if stored
    all_versions: bool       store every published version (history), oldest first
    data_dir:    str | Path  default "data"
"""

from pathlib import Path
from typing import Any

from regpilot.core.models import AgentEvent, AgentResult
from regpilot.watcher.graph import Watcher


class WatcherAgent:
    name = "watcher"

    def __init__(self, sources=None, proposer=None):
        self.sources = sources
        self.proposer = proposer  # optional LLM step (watcher/llm_criteria.py)

    def run(self, context: dict[str, Any]) -> AgentResult:
        watcher = Watcher(Path(context.get("data_dir", "data")), sources=self.sources, proposer=self.proposer)
        result = AgentResult(agent=self.name, ok=True)
        for reg in context.get("regulations", []):
            try:
                if context.get("all_versions"):
                    states = watcher.run_all(reg, language=context.get("language", "eng"),
                                             force=bool(context.get("force")))
                else:
                    states = [watcher.run(reg, as_of=context.get("as_of"), language=context.get("language", "eng"),
                                          force=bool(context.get("force")))]
            except Exception as e:  # network down, malformed document, ...
                result.ok = False
                result.errors.append(f"{reg}: {type(e).__name__}: {e}")
                continue
            for state in states:
                self._collect(reg, state, result)
        return result

    @staticmethod
    def _collect(reg: str, state, result: AgentResult):
        result.events += [AgentEvent.model_validate(e) for e in state.get("events", [])]
        result.messages += state.get("messages", [])
        for w in state.get("warnings", []):
            if w.startswith("extraction failed"):
                result.errors.append(f"{reg}: {w}")
            else:
                result.messages.append(f"WARNING {reg}: {w}")
        if state.get("status") == "extraction_failed":
            result.ok = False
