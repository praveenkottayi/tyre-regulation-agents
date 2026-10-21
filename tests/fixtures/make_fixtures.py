"""
Builds the test fixtures: real OJ XHTML cut down to sections 6 and 12 of R117,
so tests run offline and fast.

    uv run python tests/fixtures/make_fixtures.py data/raw/eurlex/42025X1453/<hash>.eng.xhtml r117_2025
    uv run python tests/fixtures/make_fixtures.py "data/raw/eurlex/42016X0812(01)/<hash>.eng.xhtml" r117_2016

Source: EUR-Lex / Official Journal of the European Union, (c) European Union.
Reuse is authorised under Commission Decision 2011/833/EU.
"""

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

NS = "{http://www.w3.org/1999/xhtml}"
ET.register_namespace("", NS[1:-1])


def text(el) -> str:
    return " ".join("".join(el.itertext()).split())


def section_start(el) -> str | None:
    """'6' if el starts top-level section 6 (heading or inline paragraph)."""
    if el.tag == NS + "p" and el.get("class") in ("oj-ti-grseq-1", "oj-normal"):
        m = re.match(r"^(\d+)\.\s", text(el))
        return m.group(1) if m else None
    return None


def main(src: str, name: str):
    root = ET.fromstring(Path(src).read_bytes())
    body = root.find(NS + "body")
    # the element holding the numbered section HEADINGS (annex forms also contain numbered paragraphs)
    container = max(body.iter(), key=lambda e: sum(1 for c in e if section_start(c) and c.get("class") == "oj-ti-grseq-1"))
    keep, on = [], False
    for child in list(container):
        start = section_start(child)
        if start is not None:  # a new top-level section begins
            on = start in ("6", "12")
        if child.tag == NS + "p" and child.get("class") == "oj-doc-ti" and re.match(r"(?i)annex", text(child)):
            on = False
        if on:
            keep.append(child)
    header = [p for p in body.iter(NS + "p") if (p.get("class") or "").startswith("oj-hd-")]
    title = next(p for p in body.iter(NS + "p") if p.get("class") == "oj-doc-ti")
    for el in list(body):
        body.remove(el)
    for el in header + [title] + keep:
        body.append(el)
    out = Path(__file__).parent / f"{name}.xhtml"
    out.write_bytes(b'<?xml version="1.0" encoding="UTF-8"?>' + ET.tostring(root, encoding="utf-8"))
    print(f"wrote {out} ({out.stat().st_size:,} bytes, {len(keep)} blocks)")


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
