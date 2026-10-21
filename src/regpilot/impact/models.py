"""
Reg-Impact (Agent 2) contracts: market applicability, per-size impacts, the proposal.

Verdicts are deliberately more than pass/fail. Anything that isn't proven
compliant is reported as what it is:

  pass, fail            test result compared with the applicable limit
  missing_test          no test record for this metric
  expired_test          only expired tests (valid_until before the as-of date)
  invalid_test          only tests marked invalid
  incomparable          test unit differs from the requirement unit
  unknown, ambiguous    the limit row could not be determined (see watcher criteria)
  not_defined           the regulation itself says "Not defined"
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Verdict = Literal["pass", "fail", "missing_test", "expired_test", "invalid_test", "incomparable",
                  "unknown", "ambiguous", "not_defined"]
NOT_COMPLIANT_PROVEN = {"fail", "missing_test", "expired_test", "invalid_test", "incomparable",
                        "unknown", "ambiguous"}

# When does a requirement bite, relative to the as-of date?
Timing = Literal[
    "in_force",            # applies to all approvals (date passed, or no transition date in this version)
    "new_approvals_only",  # new approvals must comply; existing ones from `all_from`
    "upcoming",            # nothing applies yet
]


class MarketApplicability(BaseModel):
    regulation: str
    market: str  # "EU", "IN", ...
    status: Literal["applies", "not_applicable", "unknown"]
    basis: str  # why: "published in the EU Official Journal (UN R117 applied by the EU)"


class SizeImpact(BaseModel):
    size_id: str
    brand: str
    range_name: str
    designation: str
    tyre_class: str
    markets: list[str]  # markets where this requirement applies to this range
    requirement_id: str
    metric: str
    unit: str
    stage: str | None
    timing: Timing
    new_from: date | None
    all_from: date | None
    requirement_change: str | None  # added | changed | unchanged | None (no previous version)
    comparator: str | None
    limit: float | None  # effective limit (after adjustments)
    limit_conditions: list[str] = Field(default_factory=list)  # verbatim row conditions
    adjustments: list[str] = Field(default_factory=list)
    test_value: float | None = None
    test_date: date | None = None
    verdict: Verdict
    margin: float | None = None  # >0 = headroom, <0 = shortfall (in the requirement's unit)
    reason: str = ""
    # Gate 1 edits (never set by the agent)
    reviewer_verdict: str | None = None
    reviewer_reason: str | None = None


class Proposal(BaseModel):
    id: str
    regulation: str
    celex: str
    document_id: int
    trigger: str  # watcher event id, or "manual"
    as_of: date
    created_at: datetime
    applicability: list[MarketApplicability]
    impacts: list[SizeImpact]
    unassessed_ranges: list[dict] = Field(default_factory=list)  # ranges with no applicable market
    superseded: dict = Field(default_factory=dict)  # requirement id -> later stage that replaced it
    summary: dict = Field(default_factory=dict)
