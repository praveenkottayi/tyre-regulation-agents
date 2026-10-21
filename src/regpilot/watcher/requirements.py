"""
Deterministic requirement extraction: ParsedDocument -> list[Requirement].

There is no LLM here on purpose: these numbers are legal limits that products are
rated against, so they have to come from the text by rule, and the extraction
has to fail loudly when the text doesn't look the way the rules expect.

Extraction is driven by PROFILES. A profile names the clauses to read and what
they measure. Only profiled regulations/clauses are extracted. There is no
"general-purpose" extraction.

What gets read, per profiled clause (e.g. R117 6.1 -> 6.1.1, 6.1.2, 6.1.3):
  tyre class  <- clause text ("For class C1 tyres") or a 'C1/C2/C3' table column
  stage       <- table title row ("Stage 2"); None if the table has no stage
  limit rows  <- every value cell + the row/column labels that select it
  adjustments <- "limits shall be increased by 1 dB(A) for ..." after a table
  dates       <- transitional provisions ("As from 7 July 2024 ... stage 2
                 requirements ... set out in paragraph 6.2.")
  quotes      <- verbatim sentences for all of the above
"""

import re
from dataclasses import dataclass
from datetime import date, datetime

from regpilot.core.models import (
    Adjustment, ApplicabilityDate, Comparator, LimitRow, Requirement, SourceQuote,
)
from regpilot.core.models import Criteria
from regpilot.watcher.criteria import parse_adjustment_condition, parse_conditions
from regpilot.watcher.parsers.oj_xhtml import ParsedDocument, Section, Table


@dataclass(frozen=True)
class ClauseProfile:
    clause: str  # "6.1"
    metric: str
    unit: str
    comparator: Comparator
    expected_classes: tuple[str, ...] = ("C1", "C2", "C3")


@dataclass(frozen=True)
class RegulationProfile:
    code: str
    clauses: tuple[ClauseProfile, ...]
    transitional_section: str | None
    languages: tuple[str, ...]  # OJ language codes the text rules are written for


PROFILES: dict[str, RegulationProfile] = {
    "R117": RegulationProfile(
        code="R117",
        clauses=(
            ClauseProfile("6.1", "rolling_sound_emission", "dB(A)", "<="),
            ClauseProfile("6.2", "wet_grip_index", "G", ">="),
            ClauseProfile("6.3", "rolling_resistance_coefficient", "N/kN", "<="),
        ),
        transitional_section="12",
        languages=("EN",),
    ),
}


class ExtractionError(ValueError):
    pass


@dataclass
class ExtractionResult:
    requirements: list[Requirement]
    warnings: list[str]  # things a human must look at; never silently dropped

    def unparsed_conditions(self) -> list[dict]:
        """Distinct limit-row condition texts the rules couldn't read, with context."""
        seen: dict[str, dict] = {}
        for r in self.requirements:
            for lr in r.limits:
                for text in lr.criteria.unparsed:
                    if text in lr.conditions and text not in seen:
                        seen[text] = {"text": text, "regulation": r.regulation, "clause": r.clause,
                                      "metric": r.metric, "tyre_class": r.tyre_class, "stage": r.stage,
                                      "row_conditions": lr.conditions, "value": lr.raw_value}
        return list(seen.values())


# --- small parsers ---------------------------------------------------------------------
VALUE = re.compile(r"^\s*(≥|≤|>=|<=)?\s*(\d+(?:[.,]\d+)?)\s*$")
NOT_DEFINED = re.compile(r"^\s*not defined\s*$", re.I)
STAGE = re.compile(r"^\s*stage\s+(\d+)\s*$", re.I)
STAGE_IN_TEXT = re.compile(r"\bstage\s+(\d+)\b", re.I)
CLASS_CELL = re.compile(r"^\s*(C[123])\s*$")
CLASS_IN_TEXT = re.compile(r"\bclass(?:es)?\s+(C[123])\b", re.I)
GENERIC_LABEL = re.compile(r"limit|index|max(imum)? value|category of use|tyre class|dB\(A\)|N/kN", re.I)
ADJUST = re.compile(r"\b(increased|decreased) by (\d+(?:[.,]\d+)?)\s*(dB\(A\)|N/kN)?", re.I)
METHOD = re.compile(r"\b(?:as measured by|tested in accordance with|evaluated in accordance with)\s+(.+?)(?:,|\.(?:\s|$)|$)", re.I)
DATE = re.compile(r"\b(\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4})\b")
STAGE_REF = re.compile(r"stage (\d+) requirements[^.]*?set out in paragraph (\d+(?:\.\d+)*)", re.I)


def to_float(s: str) -> float:
    return float(s.replace(",", "."))


def parse_date(s: str) -> date:
    return datetime.strptime(s, "%d %B %Y").date()


def classes_in(text: str) -> list[str]:
    """'C2 and C3' -> ['C2', 'C3']"""
    return sorted(set(re.findall(r"\bC[123]\b", text)))


def is_value(cell: str) -> bool:
    return bool(VALUE.match(cell) or NOT_DEFINED.match(cell))


def dedupe(cells: list[str]) -> list[str]:
    """Drop empties and repeats (merged cells are repeated across the grid)."""
    out: list[str] = []
    for c in cells:
        if c and c not in out:
            out.append(c)
    return out


def table_blocks(text: str) -> dict[int, list[str]]:
    """Text lines that follow each '[table N]' marker (until the next marker).
    Adjustments printed under a table apply to that table."""
    blocks: dict[int, list[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.fullmatch(r"\[table (\d+)\]", line.strip())
        if m:
            current = int(m.group(1))
            blocks[current] = []
        elif current is not None:
            blocks[current].append(line)
    return blocks


def intro(text: str) -> str:
    """The clause's own sentence(s) before the first table."""
    return text.split("[table")[0].strip()


# --- table reading ------------------------------------------------------------------------
def read_limit_table(table: Table, profile: ClauseProfile, section_classes: list[str],
                     where: str, warnings: list[str],
                     approved: dict | None = None) -> tuple[str | None, dict[str, list[LimitRow]], list[str]]:
    """-> (stage, {tyre_class: [LimitRow, ...]}, notes)
    notes = full-width sentence rows inside the table (older texts put
    'The above limits shall be increased by ...' there)."""
    rows = [list(r) for r in table.rows]
    notes = [r[0] for r in rows if len(set(r)) == 1 and re.search(r"\bshall\b", r[0], re.I)]
    rows = [r for r in rows if r[0] not in notes or len(set(r)) > 1]
    stage = None
    if rows and len(set(rows[0])) == 1 and STAGE.match(rows[0][0]):
        stage = STAGE.match(rows[0][0]).group(1)
        rows = rows[1:]

    first_data = next((i for i, r in enumerate(rows) if any(is_value(c) for c in r)), None)
    if first_data is None:
        warnings.append(f"{where}: table has no limit values")
        return stage, {}, notes
    header, data = rows[:first_data], rows[first_data:]
    width = len(rows[0])
    value_cols = [j for j in range(width) if any(is_value(r[j]) for r in data)]

    def column_label(j: int) -> str | None:
        labels = dedupe([h[j] for h in header if not GENERIC_LABEL.search(h[j])])
        return " / ".join(labels) or None

    multi_value = len(value_cols) > 1
    traction_columns = any((column_label(j) or "").lower() == "traction tyres" for j in value_cols)
    by_class: dict[str, list[LimitRow]] = {}
    for r in data:
        label_cells = [r[j] for j in range(width) if j not in value_cols]
        row_class = next((CLASS_CELL.match(c).group(1) for c in label_cells if CLASS_CELL.match(c)), None)
        conditions = dedupe([c for c in label_cells if not CLASS_CELL.match(c)])
        cls = row_class or (section_classes[0] if len(section_classes) == 1 else None)
        if cls is None:
            warnings.append(f"{where}: cannot tell tyre class for row {r}; row skipped")
            continue
        for j in value_cols:
            cell = r[j]
            if not is_value(cell):
                continue
            col = column_label(j) if multi_value else None
            m = VALUE.match(cell)
            if m and m.group(1):
                printed = ">=" if m.group(1) in ("≥", ">=") else "<="
                if printed != profile.comparator:
                    raise ExtractionError(f"{where}: cell {cell!r} contradicts profile comparator {profile.comparator}")
            row_conditions = conditions + ([col] if col else [])
            criteria = parse_conditions(row_conditions, traction_columns=traction_columns, approved=approved)
            reviewed = bool(approved) and any(c in approved for c in row_conditions)
            if criteria.unparsed:
                warnings.append(f"{where}: condition(s) not machine-readable, will match as 'unknown': "
                                f"{criteria.unparsed}")
            by_class.setdefault(cls, []).append(LimitRow(
                conditions=row_conditions, criteria=criteria,
                criteria_source="human_reviewed" if reviewed else "rules",
                comparator=profile.comparator,
                value=to_float(m.group(2)) if m else None,
                raw_value=cell,
            ))
    return stage, by_class, notes


def read_adjustments(lines: list[str], q, warnings: list[str], where: str) -> list[Adjustment]:
    """All sentence shapes seen in R117 2008-2025:
      'For snow tyre ..., the limits shall be increased by 1 N/kN.'
      'The above limits shall be increased by 1 dB(A) for snow tyres ..., extra load tyres or ...'
      '... by 1 dB(A) for extra load tyres or reinforced tyres and by 2 dB(A) for special use tyres.'
      'In case of traction tyres, the above limits shall be increased by 1 dB(A) for category of
       use normal and special, and by 2 dB(A) for category of use snow.'
    A sentence that says 'increased/decreased by' but can't be read is kept as an
    adjustment with delta=None, so affected tyres become 'unknown' and never get a wrong limit."""
    out = []
    for line in lines:
        direction = re.search(r"\b(increased|decreased) by\b", line, re.I)
        if not direction:
            continue
        sign = 1 if direction.group(1).lower() == "increased" else -1
        prefix = re.match(r"^\s*(?:For|In (?:the )?case of) (.+?), the (?:above )?limits?\b", line, re.I)
        prefix_text = prefix.group(1).strip() if prefix else None
        segments = re.split(r",?\s+and by (?=\d)", line[direction.end():])
        for seg in segments:
            m = re.fullmatch(r"\s*(\d+(?:[.,]\d+)?)\s*(dB\(A\)|N/kN)?(?:\s+for\s+(.+?))?\s*\.?\s*", seg, re.I)
            if not m:
                warnings.append(f"{where}: adjustment sentence not understood, affected tyres will be 'unknown': {line!r}")
                out.append(Adjustment(delta=None, unit=None, applies_to=line,
                                      applies_if_any=[Criteria(unparsed=[line])], quote=q(line)))
                break
            target = m.group(3).strip(" .") if m.group(3) else None
            alts = parse_adjustment_condition(prefix_text, target)
            if any(a.unparsed for a in alts):
                warnings.append(f"{where}: adjustment condition not machine-readable, will match as 'unknown': "
                                f"{[u for a in alts for u in a.unparsed]}")
            out.append(Adjustment(
                delta=sign * to_float(m.group(1)), unit=m.group(2),
                applies_to=" AND ".join(filter(None, [prefix_text, target])) or "all tyres in this table",
                applies_if_any=alts, quote=q(line)))
    return out


# --- main entry point --------------------------------------------------------------------------
def extract(doc: ParsedDocument, regulation: str, *, celex: str | None = None,
            document_id: int | None = None, approved: dict[str, Criteria] | None = None) -> ExtractionResult:
    """approved: human-approved condition mappings (from watcher.db criteria_reviews)."""
    profile = PROFILES.get(regulation.upper())
    if profile is None:
        raise ExtractionError(f"no extraction profile for {regulation}; profiles: {sorted(PROFILES)}")
    if (doc.language or "EN").upper() not in profile.languages:
        raise ExtractionError(f"profile {profile.code} rules are written for {profile.languages}, "
                              f"document language is {doc.language}")

    def q(text: str, section: Section | None = None, key: str | None = None) -> SourceQuote:
        return SourceQuote(text=text.strip(), section_key=key or section.key, celex=celex, document_id=document_id)

    warnings: list[str] = []
    requirements: list[Requirement] = []

    for cp in profile.clauses:
        parent = doc.section(f"main:{cp.clause}")
        if parent is None:  # e.g. rolling resistance (6.3) only entered R117 in 2011
            warnings.append(f"clause {cp.clause} is not in this version")
            continue
        parent_methods = METHOD.findall(parent.text)
        for sec in doc.subtree(parent.key):
            if not sec.tables:
                continue
            sec_intro = intro(sec.text)
            section_classes = sorted(set(CLASS_IN_TEXT.findall(sec_intro)))
            blocks = table_blocks(sec.text)
            stages_here: list[tuple[str | None, dict]] = []
            text_stages = set(STAGE_IN_TEXT.findall(sec_intro))
            for n, table in enumerate(sec.tables, 1):
                where = f"{sec.key} table {n}"
                stage, by_class, notes = read_limit_table(table, cp, section_classes, where, warnings, approved)
                if stage is None and len(sec.tables) == 1 and len(text_stages) == 1:
                    stage = text_stages.pop()  # "The maximum values for stage 1 ..." (older layout)
                adjustments = read_adjustments(blocks.get(n, []) + notes, lambda t: q(t, sec), warnings, where)
                stages_here.append((stage, by_class, adjustments))

            for stage, by_class, adjustments in stages_here:
                for cls, limits in sorted(by_class.items()):
                    quotes = [q(parent.text, parent)] if parent is not sec else []
                    quotes.append(q(sec_intro, sec))
                    quotes += [a.quote for a in adjustments]
                    # id uses the PROFILED clause (6.3), not the sub-clause (6.3.2 in 2016,
                    # 6.3 in 2025): stable ids are what make version-to-version diffs work.
                    requirements.append(Requirement(
                        id=f"{profile.code}:{cp.clause}:{cls}:stage-{stage or 'unlabelled'}",
                        regulation=profile.code, clause=sec.number, metric=cp.metric, unit=cp.unit,
                        comparator=cp.comparator, tyre_class=cls, stage=stage, previous_stage=None,
                        limits=limits,
                        conditions=[f"method: {m}" for m in dedupe(parent_methods + METHOD.findall(sec_intro))],
                        adjustments=adjustments, quotes=quotes,
                    ))

        mine = [r for r in requirements if r.metric == cp.metric]
        for r in mine:  # previous stage = highest lower stage printed for the same metric + class
            if r.stage:
                lower = sorted(int(o.stage) for o in mine
                               if o.stage and o.tyre_class == r.tyre_class and int(o.stage) < int(r.stage))
                r.previous_stage = str(lower[-1]) if lower else None
        found = {r.tyre_class for r in mine}
        for missing in sorted(set(cp.expected_classes) - found):
            warnings.append(f"clause {cp.clause}: nothing found for {missing} (absent in this version, or a format change)")

    ids = [r.id for r in requirements]
    for dup in sorted({i for i in ids if ids.count(i) > 1}):
        raise ExtractionError(f"two requirements resolved to the same id {dup}; the profile needs refining")

    if profile.transitional_section:
        attach_dates(doc, profile, requirements, q, warnings)

    return ExtractionResult(requirements=requirements, warnings=warnings)


def attach_dates(doc, profile, requirements, q, warnings):
    """'As from <date> ... if the stage N requirements ... set out in paragraph 6.X
    are not complied with' -> ApplicabilityDate on matching requirements."""
    for sec in doc.subtree(f"main:{profile.transitional_section}"):
        if "as from" not in sec.text.lower():
            continue
        refs = STAGE_REF.findall(sec.text)
        if not refs:
            continue
        scope = "new_approvals" if "first issued after" in sec.text.lower() else "all_approvals"
        dated: list[tuple[date, list[str]]] = []
        for table in sec.tables:  # 'Tyre class | Date' table
            for row in table.rows[table.header_rows:]:
                ds = [DATE.search(c) for c in row]
                d = next((m.group(1) for m in ds if m), None)
                cls = classes_in(" ".join(row))
                if d and cls:
                    dated.append((parse_date(d), cls))
        if not dated:
            m = DATE.search(sec.text)
            if not m:
                warnings.append(f"{sec.key}: 'as from' provision without a readable date")
                continue
            dated = [(parse_date(m.group(1)), ["C1", "C2", "C3"])]
        quote = q(intro(sec.text) or sec.text, sec)
        for stage, paragraph in refs:
            for r in requirements:
                if r.stage == stage and (r.clause == paragraph or r.clause.startswith(paragraph + ".")):
                    for d, cls in dated:
                        if r.tyre_class in cls:
                            r.applicability.append(ApplicabilityDate(date=d, scope=scope, tyre_classes=cls, quote=quote))
