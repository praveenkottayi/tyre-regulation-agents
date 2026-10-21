"""
Market applicability: does a requirement apply in a given market?

ApplicabilityJudge is the interface. RuleBasedJudge is the only implementation:
it knows one fact for sure (UN Regulations published in the EU Official
Journal apply in the EU) and says 'unknown' for everything else. It never
infers that a country adopted, adapted or voluntarily follows a regulation.

An LLM-backed judge could implement the same interface later, but it would
have to return evidence and still say 'unknown' for unsupported claims.
"""

from typing import Protocol

from regpilot.impact.models import MarketApplicability

EU_MARKETS = {"EU", "EEA", "AT", "BE", "BG", "HR", "CY", "CZ", "DK", "EE", "FI", "FR", "DE", "GR", "HU", "IE",
              "IT", "LV", "LT", "LU", "MT", "NL", "PL", "PT", "RO", "SK", "SI", "ES", "SE"}


class ApplicabilityJudge(Protocol):
    def assess(self, regulation: str, source: str, market: str) -> MarketApplicability: ...


class RuleBasedJudge:
    def assess(self, regulation: str, source: str, market: str) -> MarketApplicability:
        m = market.strip().upper()
        if source == "eurlex" and m in EU_MARKETS:
            return MarketApplicability(regulation=regulation, market=m, status="applies",
                                       basis="published in the EU Official Journal: applies in the EU")
        return MarketApplicability(regulation=regulation, market=m, status="unknown",
                                   basis=f"no rule establishes whether {regulation} applies in {m}; "
                                         "not assumed (needs a source or a human decision)")
