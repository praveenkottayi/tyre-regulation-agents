"""
Contracts between agents. These are the boundaries that must stay stable:
the Watcher produces Requirements and AgentEvents; Reg-Impact (Agent 2,
future) consumes them.

Rule: every legal value carries the verbatim source text it came from
(SourceQuote). Nothing here is produced by an LLM.
"""

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field

Comparator = Literal["<=", ">="]
TyreClass = Literal["C1", "C2", "C3"]

EVENT_NEW_VERSION = "regulation.new_version"  # first version seen, or a newer OJ publication
EVENT_UPDATED = "regulation.updated"  # same publication, content changed on re-fetch
EVENT_BACKFILLED = "regulation.backfilled"  # an OLDER version stored for history; impact must not act on it
EVENT_IMPACT_PROPOSED = "impact.proposed"  # emitted by Reg-Impact (not implemented here)


class SourceQuote(BaseModel):
    """Verbatim legal text plus where it came from."""

    text: str
    section_key: str  # e.g. "main:6.1.1"
    celex: str | None = None
    document_id: int | None = None


class Range(BaseModel):
    min: float | None = None
    min_inclusive: bool = True
    max: float | None = None
    max_inclusive: bool = True

    def contains(self, v: float) -> bool:
        if self.min is not None and (v < self.min or (v == self.min and not self.min_inclusive)):
            return False
        if self.max is not None and (v > self.max or (v == self.max and not self.max_inclusive)):
            return False
        return True


# Tyre attributes a product must provide for criteria matching (Agent 2 supplies them).
TYRE_ATTRIBUTES = (
    "section_width_mm", "load_index", "aspect_ratio", "speed_kmh",  # numbers
    "category_of_use",  # "normal" | "snow" | "special_use"
    "severe_snow", "ice_grip", "traction", "run_flat", "c_cp_lt_marking", "extra_load", "reinforced",  # booleans
)


class Criteria(BaseModel):
    """Machine-readable version of a limit row's conditions, parsed BY RULE from
    the verbatim text (watcher/criteria.py). A field left None = not constrained.
    Anything the rules could not read goes to `unparsed`, and then matches() says
    'unknown' (None), never True."""

    section_width_mm: Range | None = None
    load_index: Range | None = None
    aspect_ratio: Range | None = None
    speed_kmh: Range | None = None
    category_of_use: Literal["normal", "snow", "special_use"] | None = None
    severe_snow: bool | None = None  # 3PMSF / "tyre for use in severe snow conditions"
    ice_grip: bool | None = None
    traction: bool | None = None
    run_flat: bool | None = None  # run flat or extended mobility tyre
    c_cp_lt_marking: bool | None = None  # C3 tyres marked "C", "CP" or "LT"
    extra_load: bool | None = None
    reinforced: bool | None = None
    unparsed: list[str] = Field(default_factory=list)

    def constrained(self) -> list[str]:
        return [f for f in TYRE_ATTRIBUTES if getattr(self, f) is not None]

    def specificity(self) -> int:
        """How many attributes this row constrains. When several rows of one
        requirement match a tyre (e.g. 'Snow tyre' and its sub-row 'severe snow'),
        the most specific one is the row the regulation means."""
        return len(self.constrained())

    def matches(self, tyre: dict) -> bool | None:
        """True / False, or None = unknown (unparsed text, or the tyre lacks an attribute)."""
        missing = False
        for f in self.constrained():
            want, have = getattr(self, f), tyre.get(f)
            if have is None:
                missing = True
            elif (not want.contains(float(have))) if isinstance(want, Range) else want != have:
                return False  # a known mismatch decides, even if other attributes are missing
        return None if (missing or self.unparsed) else True


class LimitRow(BaseModel):
    """One cell of a limit table, with the row/column conditions that select it."""

    conditions: list[str]  # verbatim, e.g. ["Snow tyre", "...severe snow conditions", "Traction tyres"]
    criteria: Criteria = Field(default_factory=Criteria)  # the same conditions, machine-readable
    criteria_source: Literal["rules", "human_reviewed"] = "rules"  # human_reviewed = an approved LLM proposal
    comparator: Comparator
    value: float | None  # None when the regulation says "Not defined"
    raw_value: str  # exactly as printed, e.g. "≥ 1,1"


class Adjustment(BaseModel):
    """'The above limits shall be increased by 1 dB(A) for snow tyres ...'"""

    delta: float | None  # +1.0 / -0.08; None = sentence not understood -> tyres it may cover rate 'unknown'
    unit: str | None
    applies_to: str  # the condition, as written
    applies_if_any: list[Criteria] = Field(default_factory=list)  # adjustment applies if ANY matches
    quote: SourceQuote


class ApplicabilityDate(BaseModel):
    date: date
    scope: Literal["new_approvals", "all_approvals"]
    tyre_classes: list[TyreClass]
    quote: SourceQuote


class Requirement(BaseModel):
    """The watcher -> impact boundary: one metric, one tyre class, one stage."""

    id: str  # stable across versions: "R117:6.1.1:C1:stage-2"
    regulation: str  # "R117"
    clause: str  # "6.1.1"
    metric: str  # "rolling_sound_emission" | "wet_grip_index" | "rolling_resistance_coefficient"
    unit: str  # "dB(A)", "G", "N/kN"
    comparator: Comparator
    tyre_class: TyreClass
    stage: str | None  # "2"
    previous_stage: str | None  # the stage printed alongside it in the same clause, if any
    limits: list[LimitRow]
    conditions: list[str] = Field(default_factory=list)  # clause-level, e.g. test method
    adjustments: list[Adjustment] = Field(default_factory=list)
    applicability: list[ApplicabilityDate] = Field(default_factory=list)
    quotes: list[SourceQuote] = Field(default_factory=list)

    def fingerprint(self) -> str:
        """Hash of the legal content only (not ids/quotes' document ids), used to
        detect whether a requirement changed between two versions."""
        import hashlib

        payload = self.model_dump_json(include={"metric", "unit", "comparator", "tyre_class", "stage",
                                                "limits", "conditions"})
        payload += "|".join(f"{a.delta}{a.unit}{a.applies_to}" for a in self.adjustments)
        payload += "|".join(f"{a.date}{a.scope}{a.tyre_classes}" for a in self.applicability)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]


class AgentEvent(BaseModel):
    id: str  # deterministic, so re-running never duplicates an event
    type: str
    source_agent: str
    payload: dict
    created_at: datetime


class AgentResult(BaseModel):
    agent: str
    ok: bool
    events: list[AgentEvent] = Field(default_factory=list)
    messages: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
