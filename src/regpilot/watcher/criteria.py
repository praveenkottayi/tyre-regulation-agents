"""
Verbatim limit-row conditions -> Criteria, by rule.

    "Over 185 up to 245"                          -> section_width_mm: (185, 245]
    "load capacity index ≥ 87"                    -> load_index: [87, ∞)
    "Snow tyre that is classified as tyre for use
     in severe snow conditions and with a speed
     category greater than 160 km/h"              -> snow, severe_snow, speed_kmh: (160, ∞)
    "Tyres other than Run Flat Tyres or ..."      -> run_flat: False

Each pattern consumes the part of the text it understood. If anything
meaningful is left over, the whole condition goes to `unparsed` and the row
will never match a tyre automatically. Unknown is better than a wrong match.
"""

import re

from regpilot.core.models import Criteria, Range

NUM = r"(\d+(?:[.,]\d+)?)"
GE = r"(?:≥|>=|equal to or (?:greater|higher) than|greater than or equal to)"
LE = r"(?:≤|<=|equal to or less than|less than or equal to)"


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def _range(op: str, v: float) -> Range:
    op = op.lower()
    if op in ("≥", ">=") or "equal to or greater" in op or "equal to or higher" in op or "greater than or equal" in op:
        return Range(min=v)
    if op in ("≤", "<=") or "equal to or less" in op or "less than or equal" in op or op == "not greater than":
        return Range(max=v)
    if op in (">", "greater than"):
        return Range(min=v, min_inclusive=False)
    if op in ("<", "less than"):
        return Range(max=v, max_inclusive=False)
    raise ValueError(op)


# (pattern, setter). Order matters: negations before the positive forms.
PATTERNS: list[tuple[str, callable]] = [
    # --- whole-cell short category labels (2008/2011 tables: "Normal", "Snow", "Special")
    (r"^normal$", lambda c, m: setattr(c, "category_of_use", "normal")),
    (r"^snow$", lambda c, m: setattr(c, "category_of_use", "snow")),
    (r"^special$", lambda c, m: setattr(c, "category_of_use", "special_use")),
    (r"normal \(road type\) tyres?", lambda c, m: setattr(c, "category_of_use", "normal")),
    # --- whole-cell section-width bands (R117 6.1.1 noise table)
    (rf"^{NUM} and lower$", lambda c, m: setattr(c, "section_width_mm", Range(max=_num(m[1])))),
    (rf"^over {NUM} up to {NUM}$", lambda c, m: setattr(c, "section_width_mm",
                                                          Range(min=_num(m[1]), min_inclusive=False, max=_num(m[2])))),
    (rf"^over {NUM}$", lambda c, m: setattr(c, "section_width_mm", Range(min=_num(m[1]), min_inclusive=False))),
    # --- negations
    (r"other than run flat tyres or extended mobility tyres", lambda c, m: setattr(c, "run_flat", False)),
    (r"other than traction tyres", lambda c, m: setattr(c, "traction", False)),
    (r"other than tyres marked with [“\"]?c[”\"]?, [“\"]?cp[”\"]? or [“\"]?lt[”\"]?", lambda c, m: setattr(c, "c_cp_lt_marking", False)),
    # --- positive classifications
    (r"run flat tyres or extended mobility tyres", lambda c, m: setattr(c, "run_flat", True)),
    (r"marked with [“\"]?c[”\"]? or [“\"]?cp[”\"]? as suffix.*?service description", lambda c, m: setattr(c, "c_cp_lt_marking", True)),
    (r"traction tyres?", lambda c, m: setattr(c, "traction", True)),
    (r"ice grip tyres?", lambda c, m: setattr(c, "ice_grip", True)),
    (r"extra load tyres?", lambda c, m: setattr(c, "extra_load", True)),
    (r"reinforced tyres?", lambda c, m: setattr(c, "reinforced", True)),
    (r"(?:that (?:is|are) )?(?:classified as (?:a )?tyres? )?for use in severe snow conditions",
     lambda c, m: setattr(c, "severe_snow", True)),
    (r"special use tyres?", lambda c, m: setattr(c, "category_of_use", "special_use")),
    (r"snow tyres?", lambda c, m: setattr(c, "category_of_use", "snow")),
    (r"normal tyres?", lambda c, m: setattr(c, "category_of_use", "normal")),
    # --- numeric constraints inside sentences
    (rf"load capacity index\s*(≥|≤|>=|<=|<|>)\s*{NUM}", lambda c, m: setattr(c, "load_index", _range(m[1], _num(m[2])))),
    (rf"(?:nominal )?aspect ratio\s*({LE}|{GE}|≤|≥)\s*{NUM}", lambda c, m: setattr(c, "aspect_ratio", _range(m[1], _num(m[2])))),
    (rf"speed category (not greater than|greater than)\s*{NUM}\s*km/h",
     lambda c, m: setattr(c, "speed_kmh", _range(m[1], _num(m[2])))),
    (rf"suitable for speeds?\s*({GE}|{LE}|≥|≤)\s*{NUM}\s*km/h", lambda c, m: setattr(c, "speed_kmh", _range(m[1], _num(m[2])))),
    # 2016 wording: "speed symbol ('R' and above, including 'H') indicating a maximum permissible speed greater than 160 km/h"
    (rf"speed symbol\s*\([^)]*\)\s*indicating a maximum permissible speed (not greater than|greater than)\s*{NUM}\s*km/h",
     lambda c, m: setattr(c, "speed_kmh", _range(m[1], _num(m[2])))),
    # --- connective phrase in adjustment sentences
    (r"(?:or )?any combination of these classifications", lambda c, m: None),
]

FILLER = re.compile(r"\b(and|or|with|a|an|the|that|is|are|which|tyres?|classified|as)\b|[,.;:’‘“”'\"()/\-–]", re.I)


def _apply(text: str, c: Criteria) -> bool:
    """Apply all patterns to one condition text. True if fully understood."""
    rest = " ".join(text.split())
    for pattern, setter in PATTERNS:
        m = re.search(pattern, rest, re.I)
        if m:
            setter(c, m)
            rest = (rest[: m.start()] + " " + rest[m.end():]).strip()
    return FILLER.sub(" ", rest).strip() == ""


def parse_conditions(conditions: list[str], *, traction_columns: bool = False,
                     approved: dict[str, Criteria] | None = None) -> Criteria:
    """All conditions of one limit row -> one Criteria (they are AND-ed).
    traction_columns: the table splits values into 'Other' | 'Traction tyres'
    columns, so the column label 'Other' means 'not a traction tyre'.
    approved: human-approved mappings (condition text -> Criteria), used only
    for texts the rules can't read."""
    c = Criteria()
    for text in conditions:
        if traction_columns and text.strip().lower() == "other":
            c.traction = False
            continue
        probe = Criteria()
        if _apply(text, probe):
            c = and_criteria(c, probe)
        elif approved and text in approved:
            c = and_criteria(c, approved[text])
        else:
            c = and_criteria(c, probe)
            c.unparsed.append(text)
    return c


def and_criteria(a: Criteria, b: Criteria) -> Criteria:
    """Both must hold. A contradiction is recorded as unparsed (-> unknown)."""
    from regpilot.core.models import TYRE_ATTRIBUTES

    out = a.model_copy(deep=True)
    out.unparsed = a.unparsed + b.unparsed
    for f in TYRE_ATTRIBUTES:
        vb = getattr(b, f)
        if vb is None:
            continue
        va = getattr(out, f)
        if va is not None and va != vb:
            out.unparsed.append(f"conflicting {f}: {va} vs {vb}")
        else:
            setattr(out, f, vb)
    return out


CATEGORY_WORDS = {"normal": "normal", "snow": "snow", "special": "special_use", "special use": "special_use"}


def parse_adjustment_condition(prefix: str | None, target: str | None) -> list[Criteria]:
    """Condition of one adjustment = prefix AND target, where target may list
    alternatives. 'In case of traction tyres, ... by 1 dB(A) for category of use
    normal and special' -> [traction+normal, traction+special_use]."""
    base = parse_conditions([prefix]) if prefix else Criteria()
    if not target:
        return [base]
    m = re.fullmatch(r"\s*category of use (.+?)\s*", target, re.I)
    if m:
        alts = []
        for word in re.split(r"\s+and\s+|\s*,\s*|\s+or\s+", m.group(1).strip()):
            cat = CATEGORY_WORDS.get(word.strip().lower())
            alts.append(Criteria(category_of_use=cat) if cat else Criteria(unparsed=[word]))
    else:
        alts = parse_applies_to(target)
    return [and_criteria(base, alt) for alt in alts]


def parse_applies_to(text: str) -> list[Criteria]:
    """Adjustment condition -> alternatives (OR). E.g. 'snow tyres that are
    classified ... severe snow conditions, extra load tyres or reinforced tyres,
    or any combination of these classifications' -> [severe snow], [XL], [reinforced]."""
    parts = [p for p in re.split(r",\s*(?:or\s+)?|\s+or\s+(?!extended mobility)", text) if p.strip()]
    out = []
    for p in parts:
        if re.fullmatch(r"\s*any combination of these classifications\s*\.?", p, re.I):
            continue
        c = Criteria()
        if not _apply(p, c):
            c.unparsed.append(p.strip())
        out.append(c)
    return out
