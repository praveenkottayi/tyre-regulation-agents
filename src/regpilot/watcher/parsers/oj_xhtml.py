"""
Parser for EU Official Journal XHTML (as served by Cellar), stdlib only.

How the OJ lays out a regulation (verified on UN R117 2025 = 42025X1453 and
2016 = 42016X0812(01)):
  p.oj-doc-ti        document title, and annex markers: "ANNEX 3", "Annex 2 - Appendix 1"
  p.oj-ti-grseq-1    section headings: "6.   Requirements"
  numbered clauses come in two layouts, often mixed in one document:
    a) layout table (no class): cells = [(empty) | "6.1.1." | body]
    b) inline paragraph: <p class="oj-normal">6.1.1.   For Class C1 tyres ...</p>,
       followed by tables/paragraphs that belong to it until the next clause
  table.oj-table     a data table (limits etc.), nested in a clause body or standalone

Clause numbers restart in every annex, so each section key is scoped:
  "main:6.1.1", "annex-5:1.2", "annex-2/appendix-1:3".

The parser does no interpretation. Requirement extraction lives in
watcher/requirements.py.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

NS = "{http://www.w3.org/1999/xhtml}"
CLAUSE_NUM = re.compile(r"^(\d+(?:\.\d+)*)\.?$")
HEADING_NUM = re.compile(r"^(\d+(?:\.\d+)*)\.?\s+(.+)$")
INLINE_CLAUSE = re.compile(r"^(\d+(?:\.\d+)*)\.\s+(.+)$", re.S)  # "6.1.1.   For Class C1 ..."
# Annex markers in the OJ languages we have seen (the word differs per language;
# a missed marker would merge annex clauses into main-body clauses).
ANNEX_WORDS = r"annex|anhang|annexe|allegato|anexo|bijlage|załącznik|příloha|bilaga|liite|bilag"
APPENDIX_WORDS = r"appendix|anlage|appendice|apéndice|aanhangsel|dodatek|apêndice|tillägg|lisäys|tillæg"
ANNEX = re.compile(rf"^(?:{ANNEX_WORDS})\s+(\d+)(?:\s*[-–—]?\s*(?:{APPENDIX_WORDS})\s*(\d+)?)?$", re.I)
DEFINITION = re.compile(r'^[“"‘](?P<term>[^”"’]+)[”"’]\s+(?:means|is|are)\b', re.I)


@dataclass
class Table:
    rows: list[list[str]]  # colspans expanded, so every row has the full width
    header_rows: int  # leading rows made of header cells


@dataclass
class Section:
    key: str  # "main:6.1.1"
    scope: str  # "main", "annex-5", ...
    number: str  # "6.1.1" ("" for unnumbered headings)
    heading: str | None
    text: str  # clause text; nested data tables replaced by "[table N]"
    tables: list[Table] = field(default_factory=list)
    order: int = 0

    @property
    def parent_number(self) -> str | None:
        return self.number.rsplit(".", 1)[0] if "." in self.number else None


@dataclass
class Definition:
    term: str
    text: str
    section_key: str


@dataclass
class ParsedDocument:
    title: str
    oj_reference: str | None  # e.g. "2025/1453"
    oj_date: str | None  # as printed, e.g. "7.8.2025"
    language: str | None
    sections: list[Section]
    definitions: list[Definition]
    warnings: list[str] = field(default_factory=list)  # parser-level oddities, surfaced to the run log

    def section(self, key: str) -> Section | None:
        return next((s for s in self.sections if s.key == key), None)

    def subtree(self, key: str) -> list[Section]:
        """A section and all its sub-clauses, in document order."""
        scope, number = key.split(":", 1)
        return [s for s in self.sections
                if s.scope == scope and (s.number == number or s.number.startswith(number + "."))]


# --- helpers -------------------------------------------------------------------------
def _cls(el) -> str:
    return el.get("class") or ""


def _tag(el) -> str:
    return el.tag.replace(NS, "")


def _norm(s: str) -> str:
    return " ".join(s.split())


def _text(el) -> str:
    return _norm("".join(el.itertext()))


def _is_data_table(el) -> bool:
    return _tag(el) == "table" and "oj-table" in _cls(el)


def _clause_row(table):
    """If `table` is a numbered-clause layout table, return (number, body_td)."""
    if _tag(table) != "table" or _is_data_table(table):
        return None
    rows = _direct_rows(table)
    if len(rows) != 1:
        return None
    cells = rows[0].findall(f"{NS}td")
    if len(cells) < 2:
        return None
    for i, td in enumerate(cells[:-1]):
        m = CLAUSE_NUM.match(_text(td))
        if m:
            return m.group(1), cells[-1]
    return None


def _direct_rows(table) -> list:
    """<tr> elements of this table only (not of tables nested inside it)."""
    rows = []
    for part in table:
        if _tag(part) in ("tbody", "thead", "tfoot"):
            rows += [tr for tr in part if _tag(tr) == "tr"]
        elif _tag(part) == "tr":
            rows.append(part)
    return rows


def parse_table(table) -> Table:
    """Table -> rectangular grid. colspan AND rowspan are expanded (a merged
    cell's text is repeated in every grid position it covers), so each row can
    be read on its own: ['Snow tyre', 'Severe snow', '73', '75']."""
    grid: list[list[str]] = []
    is_hdr: list[bool] = []
    carry: dict[int, tuple[str, int, bool]] = {}  # col -> (text, rows still to fill, header?)
    for tr in _direct_rows(table):
        row: list[str] = []
        hdr_flags: list[bool] = []
        cells = [td for td in tr if _tag(td) in ("td", "th")]
        col = 0

        def fill_carried():
            nonlocal col
            while col in carry:
                text, left, h = carry[col]
                row.append(text)
                hdr_flags.append(h)
                carry[col] = (text, left - 1, h) if left > 1 else None
                if carry[col] is None:
                    del carry[col]
                col += 1

        for td in cells:
            fill_carried()
            text = _text(td)
            h = bool(td.findall(f".//{NS}p[@class='oj-tbl-hdr']")) or _tag(td) == "th"
            colspan, rowspan = int(td.get("colspan", "1")), int(td.get("rowspan", "1"))
            for _ in range(colspan):
                row.append(text)
                hdr_flags.append(h)
                if rowspan > 1:
                    carry[col] = (text, rowspan - 1, h)
                col += 1
        fill_carried()
        if row:
            grid.append(row)
            is_hdr.append(all(hdr_flags))
    width = max((len(r) for r in grid), default=0)
    grid = [r + [""] * (width - len(r)) for r in grid]
    header_rows = 0
    for h in is_hdr:
        if not h:
            break
        header_rows += 1
    return Table(rows=grid, header_rows=header_rows)


def _body_text(el, tables: list[Table]) -> list[str]:
    """Text blocks of a clause body. Data tables become '[table N]' and are
    collected into `tables`; nested enumerations ((a), (i), ...) become one line each."""
    lines: list[str] = []
    for child in el:
        tag = _tag(child)
        if _is_data_table(child):
            tables.append(parse_table(child))
            lines.append(f"[table {len(tables)}]")
        elif tag == "table":
            for tr in _direct_rows(child):
                parts = []
                for td in tr.findall(f"{NS}td"):
                    sub: list[str] = []
                    inner = _body_text(td, tables)
                    sub += inner
                    if sub:
                        parts.append(" ".join(sub))
                if parts:
                    lines.append(" ".join(parts))
        elif tag == "img":
            lines.append("[formula]" if "formula" in _cls(child) else "[image]")
        elif tag in ("p", "div", "span", "td") or len(child):
            inner = _body_text(child, tables) if any(_tag(c) in ("table", "p", "div", "img") for c in child) else []
            if inner:
                if child.text and child.text.strip():
                    lines.insert(len(lines), _norm(child.text))
                lines += inner
            else:
                t = _text(child)
                if t:
                    lines.append(t)
        else:
            t = _text(child)
            if t:
                lines.append(t)
    return lines


# --- main entry point ------------------------------------------------------------------
def parse(xhtml: bytes) -> ParsedDocument:
    root = ET.fromstring(xhtml)
    body = root.find(f"{NS}body")
    if body is None:
        raise ValueError("not an XHTML document (no <body>)")

    header = {_cls(p): _text(p) for p in body.iter(f"{NS}p") if _cls(p).startswith("oj-hd-")}
    title = next((_text(p) for p in body.iter(f"{NS}p") if _cls(p) == "oj-doc-ti"), "")

    sections: list[Section] = []
    warnings: list[str] = []
    # Everything before the first numbered section heading ("1. Scope") is front
    # matter: title block and the table of contents, whose rows look like clauses.
    state = {"scope": "front"}

    def add(number, heading, text, tables):
        scope = state["scope"]
        key = f"{scope}:{number}" if number else f"{scope}:#{len(sections)}"
        existing = next((s for s in sections if s.key == key), None)
        if existing and number and not existing.text and not existing.tables:
            # heading "6. Requirements" followed by a clause row "6." -> one section
            existing.text, existing.tables = text, tables
            existing.heading = existing.heading or heading
            return
        if existing:  # repeated number: never merge silently, keep both as key~N
            n = sum(1 for s in sections if s.key == key or s.key.startswith(key + "~"))
            if scope == "main":  # annexes legitimately restart numbering (parts, report forms)
                warnings.append(f"duplicate section number {key}; stored as {key}~{n + 1}")
            key = f"{key}~{n + 1}"
        sections.append(Section(key=key, scope=scope, number=number, heading=heading, text=text,
                                tables=tables, order=len(sections)))

    def walk(el):
        for child in el:
            tag, cls = _tag(child), _cls(child)
            if tag == "p" and cls == "oj-doc-ti":
                m = ANNEX.match(_text(child))
                if m:
                    has_appendix = re.search(APPENDIX_WORDS, _text(child), re.I)
                    state["scope"] = f"annex-{m.group(1)}" + (f"/appendix-{m.group(2) or 1}" if has_appendix else "")
                continue
            if tag == "p" and cls == "oj-ti-grseq-1":
                t = _text(child)
                m = HEADING_NUM.match(t)
                if state["scope"] == "front" and m:
                    state["scope"] = "main"
                add(m.group(1) if m else "", m.group(2) if m else t, "", [])
                continue
            clause = _clause_row(child)
            if clause:
                number, body_td = clause
                tables: list[Table] = []
                text = "\n".join(_body_text(body_td, tables))
                add(number, None, text, tables)
                continue
            if _is_data_table(child):  # standalone table: belongs to the current clause
                if sections:
                    sections[-1].tables.append(parse_table(child))
                    sections[-1].text += f"\n[table {len(sections[-1].tables)}]"
                continue
            if tag == "p" and cls in ("oj-normal", "normal", "oj-note"):
                t = _text(child)
                m = INLINE_CLAUSE.match(t)
                if state["scope"] == "front" and m and m.group(1) == "1" and "scope" in m.group(2).lower()[:10]:
                    state["scope"] = "main"  # older layout: "1.   SCOPE" as a plain paragraph
                if m and cls != "oj-note":  # layout (b): a new clause
                    add(m.group(1), None, m.group(2), [])
                elif t and sections:  # continuation paragraph of the current clause
                    sections[-1].text = "\n".join(filter(None, [sections[-1].text, t]))
                continue
            walk(child)

    walk(body)

    definitions = []
    for s in sections:
        if s.scope == "main" and s.number.startswith("2."):
            m = DEFINITION.match(s.text)
            if m:
                definitions.append(Definition(term=m.group("term"), text=s.text, section_key=s.key))

    return ParsedDocument(
        title=title, oj_reference=header.get("oj-hd-uniq"), oj_date=header.get("oj-hd-date"),
        language=header.get("oj-hd-lg"), sections=sections, definitions=definitions, warnings=warnings,
    )
