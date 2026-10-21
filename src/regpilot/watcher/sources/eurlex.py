"""
EUR-Lex via the EU Publications Office's official machine interfaces.

  discover: SPARQL endpoint (titles + dates of every OJ publication)
  fetch:    Cellar content negotiation by CELEX, XHTML, any EU language

The normal eur-lex.europa.eu pages answer scripts with a bot challenge
(HTTP 202, empty body), and unece.org answers 403. Don't scrape them.

UN Regulations applied by the EU are republished in the OJ, sector 4 CELEX:
  R117: 42008X0829(01), 42011X1123(03), 42016X0812(01), 42025X1453
"""

from urllib.parse import quote

from regpilot.core import net
from regpilot.watcher.sources.base import VersionRef

SPARQL_URL = "https://publications.europa.eu/webapi/rdf/sparql"
CELLAR_URL = "https://publications.europa.eu/resource/celex/{celex}"
EURLEX_LINK = "https://eur-lex.europa.eu/legal-content/{lang2}/TXT/?uri=CELEX:{celex}"
LANG3_TO_2 = {"eng": "EN", "deu": "DE", "fra": "FR", "ita": "IT", "spa": "ES", "pol": "PL", "nld": "NL"}

# Notes on the SPARQL regex (XPath flavour):
#  - no \b word boundaries: use ([^0-9]|$) instead
#  - newer titles use a NO-BREAK SPACE: "No\xa0117", hence No[^0-9]{0,3}
UN_REG_TITLE = "Regulation No[^0-9]{{0,3}}{number}([^0-9]|$)"

QUERY = """
PREFIX cdm: <http://publications.europa.eu/ontology/cdm#>
SELECT DISTINCT ?celex ?date ?title WHERE {{
  ?work cdm:resource_legal_id_celex ?celex ; cdm:work_date_document ?date .
  ?expr cdm:expression_belongs_to_work ?work ; cdm:expression_title ?title ;
        cdm:expression_uses_language <http://publications.europa.eu/resource/authority/language/ENG> .
  FILTER(REGEX(STR(?title), "{pattern}", "i"))
  FILTER(REGEX(?celex, "{celex_filter}"))
  {since_filter}
}} ORDER BY DESC(?date) LIMIT {limit}
"""


def sparql(pattern: str, celex_filter: str = "^4", since: str | None = None, limit: int = 50) -> list[dict]:
    since_filter = f'FILTER(?date >= "{since}"^^<http://www.w3.org/2001/XMLSchema#date>)' if since else ""
    q = QUERY.format(pattern=pattern, celex_filter=celex_filter, since_filter=since_filter, limit=limit)
    r = net.get(SPARQL_URL, params={"query": q, "format": "application/sparql-results+json"})
    r.raise_for_status()
    seen, out = set(), []
    for b in r.json()["results"]["bindings"]:
        celex = b["celex"]["value"]
        if celex not in seen:
            seen.add(celex)
            out.append({"celex": celex, "date": b["date"]["value"][:10],
                        "title": " ".join(b["title"]["value"].split()).replace("#", " — ")})
    return out


class EurLexCellarSource:
    name = "eurlex"

    def discover(self, regulation: str) -> list[VersionRef]:
        number = regulation.upper().removeprefix("R").removeprefix("UN")
        rows = sparql(UN_REG_TITLE.format(number=number), celex_filter="^4")
        rows = [r for r in rows if "corrigendum" not in r["title"].lower()]
        return [VersionRef(regulation=regulation.upper(), source=self.name, source_id=r["celex"], date=r["date"],
                           title=r["title"], url=EURLEX_LINK.format(lang2="EN", celex=r["celex"]))
                for r in rows]

    def fetch(self, ref: VersionRef, language: str = "eng") -> bytes:
        # CELEX ids can contain parentheses: 42016X0812(01) -> must be URL-encoded
        r = net.get(CELLAR_URL.format(celex=quote(ref.source_id, safe="")),
                    headers={"Accept": "application/xhtml+xml", "Accept-Language": language})
        if r.status_code != 200 or not r.content.lstrip().startswith(b"<?xml"):
            raise net.FetchError(f"Cellar returned HTTP {r.status_code} for {ref.source_id} [{language}]")
        return r.content


def scan_tyre_publications(since: str, limit: int = 100) -> list[dict]:
    """Broad watchlist: every OJ publication with 'tyre' in its title since a
    date (EU legislation, UN regulations, proposals, trade measures).
    For spotting things no extraction profile covers yet."""
    return sparql("(^|[^a-z])(tyres?|tires?)([^a-z]|$)", celex_filter="^(3|4|5[0-9]{4}PC)", since=since, limit=limit)
