"""
Limit selection: which row of a Requirement applies to one tyre size, and its
effective value after adjustments. Pure functions, no I/O.

Pipeline per (requirement, tyre size):
  select_limit  -> which limit row applies (+ adjustments)
  timing        -> in force / new approvals only / upcoming, as of a date
  select_test   -> latest valid, unexpired test record
  rate          -> verdict + margin

Conservative by design. The result is 'unknown' whenever the data can't prove
which row applies, and it never guesses.
"""

from dataclasses import dataclass, field
from datetime import date

from regpilot.core.models import Adjustment, LimitRow, Requirement


@dataclass
class LimitSelection:
    status: str  # selected | no_match | unknown | ambiguous | not_defined
    row: LimitRow | None = None
    effective_value: float | None = None
    adjustments: list[Adjustment] = field(default_factory=list)
    reason: str = ""


def select_limit(req: Requirement, tyre: dict) -> LimitSelection:
    if tyre.get("tyre_class") != req.tyre_class:
        return LimitSelection("no_match", reason=f"requirement is for {req.tyre_class}")

    results = [(row, row.criteria.matches(tyre)) for row in req.limits]
    unknown = [row for row, m in results if m is None]
    if unknown:
        missing = sorted({f for row in unknown for f in row.criteria.constrained() if tyre.get(f) is None})
        return LimitSelection("unknown", reason=f"cannot evaluate {len(unknown)} row(s); "
                              f"missing tyre attributes: {missing or 'unparsed conditions'}")
    matching = [row for row, m in results if m]
    if not matching:
        return LimitSelection("no_match", reason="no limit row matches this tyre")

    # Most specific row wins: 'Snow tyre' vs its sub-row 'Snow tyre ... severe snow'.
    best = max(r.criteria.specificity() for r in matching)
    top = [r for r in matching if r.criteria.specificity() == best]
    if len({r.raw_value for r in top}) > 1:
        return LimitSelection("ambiguous", reason="equally specific rows with different limits: "
                              + "; ".join(f"{' / '.join(r.conditions)} = {r.raw_value}" for r in top))
    row = top[0]
    if row.value is None:
        return LimitSelection("not_defined", row=row, reason=f"regulation says {row.raw_value!r}")

    applied = []
    for adj in req.adjustments:
        hits = [c.matches(tyre) for c in adj.applies_if_any]
        if any(h is None for h in hits) and not any(hits):
            return LimitSelection("unknown", row=row, reason=f"cannot tell whether adjustment applies: {adj.applies_to}")
        if any(hits):
            if adj.delta is None:
                return LimitSelection("unknown", row=row, reason=f"adjustment sentence not understood: {adj.applies_to[:80]}")
            applied.append(adj)
    if len(applied) > 1:
        return LimitSelection("ambiguous", row=row, adjustments=applied,
                              reason="more than one adjustment applies; the regulation does not say whether they add up")
    effective = row.value + (applied[0].delta if applied else 0.0)
    return LimitSelection("selected", row=row, effective_value=round(effective, 4), adjustments=applied)


# --- timing: does this stage apply yet? ----------------------------------------------------------
def timing(req: Requirement, as_of: date) -> tuple[str, date | None, date | None]:
    """-> (timing, new_from, all_from) from the requirement's applicability dates.
    No dates in this version of the text = in force (it is the current legal text)."""
    new_from = min((a.date for a in req.applicability if a.scope == "new_approvals"), default=None)
    all_from = min((a.date for a in req.applicability if a.scope == "all_approvals"), default=None)
    if not req.applicability or (all_from and all_from <= as_of):
        return "in_force", new_from, all_from
    if new_from and new_from <= as_of:
        return "new_approvals_only", new_from, all_from
    return "upcoming", new_from, all_from


def superseded_by(req: Requirement, all_reqs: list[Requirement], as_of: date) -> str | None:
    """A stage stops mattering once a LATER stage of the same metric and class applies
    to all approvals (e.g. C1 wet grip stage 1 after 7 July 2026). -> id of that stage, or None."""
    if not req.stage:
        return None
    for other in all_reqs:
        if (other.metric, other.tyre_class) == (req.metric, req.tyre_class) and other.stage \
                and int(other.stage) > int(req.stage):
            t, _, all_from = timing(other, as_of)
            if t == "in_force" and all_from:
                return other.id
    return None


# --- test record selection -----------------------------------------------------------------------
def select_test(tyre: dict, metric: str, as_of: date) -> tuple[dict | None, str | None]:
    """Latest VALID test for this metric, done on or before as_of.
    -> (test, None) or (None, verdict) with verdict missing_test | expired_test | invalid_test."""
    tests = [t for t in tyre.get("tests", []) if t["metric"] == metric and t["test_date"] <= as_of.isoformat()]
    if not tests:
        return None, "missing_test"
    valid = [t for t in tests if (t.get("status") or "valid") == "valid"]
    if not valid:
        return None, "invalid_test"
    current = [t for t in valid if not t.get("valid_until") or t["valid_until"] >= as_of.isoformat()]
    if not current:
        return None, "expired_test"
    return max(current, key=lambda t: t["test_date"]), None


# --- the rating ----------------------------------------------------------------------------------------
def rate(req: Requirement, tyre: dict, as_of: date) -> dict:
    """Everything needed for one SizeImpact (except product/market fields)."""
    t, new_from, all_from = timing(req, as_of)
    out = {"timing": t, "new_from": new_from, "all_from": all_from, "comparator": req.comparator,
           "limit": None, "limit_conditions": [], "adjustments": [], "test_value": None, "test_date": None,
           "margin": None}
    sel = select_limit(req, tyre)
    if sel.status != "selected":
        return {**out, "verdict": sel.status if sel.status != "no_match" else "unknown",
                "reason": sel.reason, "limit_conditions": sel.row.conditions if sel.row else []}
    out.update(limit=sel.effective_value, limit_conditions=sel.row.conditions,
               adjustments=[f"{a.delta:+g} {a.unit or ''} for {a.applies_to}".strip() for a in sel.adjustments])

    test, problem = select_test(tyre, req.metric, as_of)
    if problem:
        return {**out, "verdict": problem, "reason": {"missing_test": "no test record for this metric",
                                                      "expired_test": "all test records have expired",
                                                      "invalid_test": "all test records are marked invalid"}[problem]}
    out.update(test_value=test["value"], test_date=test["test_date"])
    if (test.get("unit") or req.unit) != req.unit:
        return {**out, "verdict": "incomparable", "reason": f"test unit {test.get('unit')} ≠ requirement unit {req.unit}"}

    value, limit = test["value"], sel.effective_value
    ok = value <= limit if req.comparator == "<=" else value >= limit
    margin = round(limit - value if req.comparator == "<=" else value - limit, 4)
    return {**out, "verdict": "pass" if ok else "fail", "margin": margin,
            "reason": f"{value:g} {req.comparator} {limit:g} {req.unit}" + ("" if ok else " NOT met")}
