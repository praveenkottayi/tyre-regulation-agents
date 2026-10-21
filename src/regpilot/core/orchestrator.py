"""
Framework-neutral agent contract, kept from the original regpilot.

    Agent.run(context) -> AgentResult

Agents implement this Protocol however they like. WatcherAgent implements it
with a LangGraph graph inside (see watcher/graph.py), so callers like the
Orchestrator and the CLI don't need to know about LangGraph.
"""

from typing import Any, Protocol

from regpilot.core.models import AgentResult


class Agent(Protocol):
    name: str

    def run(self, context: dict[str, Any]) -> AgentResult: ...


class Orchestrator:
    """Sequential: each agent sees the shared context, including events emitted
    by agents before it (context["events"])."""

    def __init__(self, agents: list[Agent]):
        self.agents = agents

    def run(self, context: dict[str, Any] | None = None) -> dict[str, Any]:
        context = dict(context or {})
        context.setdefault("events", [])
        context.setdefault("results", [])
        for agent in self.agents:
            result = agent.run(context)
            context["events"].extend(result.events)
            context["results"].append(result)
        return context
