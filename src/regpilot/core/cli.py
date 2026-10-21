"""
regpilot CLI (watcher part).

    uv run regpilot discover R117                 versions published in the OJ
    uv run regpilot watch R117 [--as-of D]        run Agent 1 (LangGraph)
    uv run regpilot requirements R117             extracted requirements (current version)
    uv run regpilot diff R117 --from 42016X0812(01) --to 42025X1453
    uv run regpilot outline R117 --depth 1        section tree
    uv run regpilot clause R117 6.1               one clause, verbatim, with its tables
    uv run regpilot search R117 "wet grip index"   text search in clauses
    uv run regpilot events                        the outbox for Agent 2
    uv run regpilot runs                          watcher run log
    uv run regpilot scan --since 2025-01-01       every tyre-related OJ publication (watchlist)
    uv run regpilot graph                         the watcher graph as Mermaid
"""

import json
from pathlib import Path

import typer

from regpilot.watcher import db as wdb

app = typer.Typer(add_completion=False, no_args_is_help=True, help="Tyre regulation pipeline: Watcher (Agent 1)")
DATA = Path("data")


def _ro():
    return wdb.connect_readonly(DATA / "watcher.db")


def _doc(con, regulation: str, language: str, celex: str | None = None):
    if celex:
        row = con.execute("SELECT * FROM documents WHERE regulation=? AND celex=? AND language=? "
                          "AND status='complete' ORDER BY id DESC LIMIT 1", (regulation.upper(), celex, language)).fetchone()
    else:
        row = wdb.current_document(con, regulation.upper(), language)
    if not row:
        raise typer.BadParameter(f"no stored version of {regulation} {celex or ''} [{language}]. Run `watch` first")
    return row


@app.command()
def discover(regulation: str):
    """List published versions of a regulation (EU Publications Office)."""
    from regpilot.watcher.sources import default_sources

    for source in default_sources():
        for v in source.discover(regulation):
            typer.echo(f"{v.date}  {v.source}:{v.source_id:16}  {v.title[:90]}")


@app.command()
def watch(regulation: list[str], as_of: str = typer.Option(None, help="YYYY-MM-DD: ignore later versions"),
          language: str = "eng", force: bool = typer.Option(False, help="re-fetch even if already stored"),
          all_versions: bool = typer.Option(False, "--all-versions", help="store every published version (history)"),
          llm: bool = typer.Option(False, "--llm", help="let the LLM propose readings of unparsed wording (for review)")):
    """Run the Watcher agent (LangGraph) for one or more regulations."""
    from regpilot.watcher.agent import WatcherAgent

    proposer = None
    if llm:
        from regpilot.watcher.llm_criteria import make_llm_proposer

        proposer = make_llm_proposer()
    result = WatcherAgent(proposer=proposer).run({"regulations": regulation, "as_of": as_of, "language": language,
                                 "force": force, "all_versions": all_versions, "data_dir": DATA})
    for m in result.messages:
        typer.echo(f"  {m}")
    for e in result.events:
        typer.secho(f"  EVENT {e.type} id={e.id} {e.payload['celex']} document #{e.payload['document_id']}",
                    fg="green" if e.type != "regulation.backfilled" else "cyan")
    for err in result.errors:
        typer.secho(f"  ERROR {err}", fg="red")
    raise typer.Exit(0 if result.ok else 1)


@app.command()
def requirements(regulation: str, language: str = "eng", celex: str = typer.Option(None),
                 as_json: bool = typer.Option(False, "--json")):
    """Extracted requirements of the current (or a given) version."""
    with _ro() as con:
        doc = _doc(con, regulation, language, celex)
        reqs = wdb.load_requirements(con, doc["id"])
    if as_json:
        typer.echo(json.dumps([r.model_dump(mode="json") for r in reqs], indent=1, ensure_ascii=False))
        return
    typer.echo(f"{doc['celex']} ({doc['pub_date']}), document #{doc['id']}: {len(reqs)} requirements\n")
    for r in reqs:
        dates = ", ".join(f"{a.scope.replace('_', ' ')} from {a.date}" for a in r.applicability)
        typer.secho(f"{r.id}  [{r.metric}, {r.unit}]" + (f"  previous stage {r.previous_stage}" if r.previous_stage else ""),
                    bold=True)
        for lr in r.limits:
            typer.echo(f"    {' / '.join(lr.conditions) or '(all)':70.70} {r.comparator} {lr.raw_value}")
        for a in r.adjustments:
            typer.echo(f"    adjustment {a.delta:+g} {a.unit or ''} for {a.applies_to[:80]}")
        if dates:
            typer.echo(f"    applies: {dates}")


@app.command(name="diff")
def diff_cmd(regulation: str, from_celex: str = typer.Option(..., "--from"), to_celex: str = typer.Option(..., "--to"),
             language: str = "eng"):
    """Requirement changes between two stored versions."""
    from regpilot.watcher.diff import diff

    with _ro() as con:
        old = wdb.load_requirements(con, _doc(con, regulation, language, from_celex)["id"])
        new = wdb.load_requirements(con, _doc(con, regulation, language, to_celex)["id"])
    d = diff(old, new)
    for k in ("added", "removed"):
        typer.echo(f"{k.upper()} ({len(d[k])}): " + ", ".join(i.split(':', 1)[1] for i in d[k]))
    typer.echo(f"CHANGED ({len(d['changed'])}):")
    for i, notes in d["changed"].items():
        typer.echo(f"  {i}")
        for n in notes:
            typer.echo(f"     - {n[:150]}")
    typer.echo(f"UNCHANGED ({len(d['unchanged'])})")


@app.command()
def outline(regulation: str, depth: int = 1, scope: str = "main", language: str = "eng"):
    """Section tree of the current version."""
    with _ro() as con:
        doc = _doc(con, regulation, language)
        rows = con.execute("SELECT * FROM sections WHERE document_id=? AND key LIKE ? ORDER BY ord", (doc["id"], scope + ":%"))
        for s in rows:
            level = s["number"].count(".") + 1 if s["number"] else 1
            if s["number"] and level <= depth:
                label = s["heading"] or s["text"].split("\n")[0]
                typer.echo(f"{'  ' * (level - 1)}{s['number']}  {label[:90]}")


@app.command()
def clause(regulation: str, number: str, scope: str = "main", language: str = "eng"):
    """One clause and its sub-clauses, verbatim, with tables."""
    with _ro() as con:
        doc = _doc(con, regulation, language)
        rows = con.execute("SELECT * FROM sections WHERE document_id=? AND (key=? OR key LIKE ?) ORDER BY ord",
                           (doc["id"], f"{scope}:{number}", f"{scope}:{number}.%")).fetchall()
        for s in rows:
            typer.secho(f"\n{s['number']}  {s['heading'] or ''}", bold=True)
            typer.echo(s["text"])
            for i, t in enumerate(json.loads(s["tables"] or "[]"), 1):
                typer.echo(f"  [table {i}]")
                for row in t["rows"]:
                    typer.echo("    | " + " | ".join(c[:40] for c in row))


@app.command()
def search(regulation: str, text: str, language: str = "eng", limit: int = 10):
    """Case-insensitive text search in the current version's clauses."""
    with _ro() as con:
        doc = _doc(con, regulation, language)
        rows = con.execute("SELECT key, text FROM sections WHERE document_id=? AND text LIKE ? ORDER BY ord LIMIT ?",
                           (doc["id"], f"%{text}%", limit))
        for r in rows:
            i = r["text"].lower().find(text.lower())
            snippet = r["text"][max(0, i - 50): i + len(text) + 60].replace("\n", " ")
            typer.echo(f"{r['key']:22} …{snippet}…")


@app.command()
def events(limit: int = 20):
    """Events emitted for downstream agents (the outbox)."""
    with _ro() as con:
        for e in con.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)):
            p = json.loads(e["payload"])
            ch = p.get("changes")
            delta = f"+{len(ch['added'])} -{len(ch['removed'])} ~{len(ch['changed'])} vs {p['previous_celex']}" if ch else "first version"
            typer.echo(f"{e['created_at']}  {e['type']:24} {p['regulation']} {p['celex']} doc #{e['document_id']}  {delta}")


@app.command()
def runs(limit: int = 20):
    """Watcher run log."""
    with _ro() as con:
        for r in con.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)):
            s = json.loads(r["summary"])
            typer.echo(f"#{r['id']} {r['finished_at']}  {r['regulation']:6} {r['status']:18} target={s['target']} "
                       f"events={len(s['events'])} warnings={len(s['warnings'])}")


@app.command()
def scan(since: str = typer.Option(..., help="YYYY-MM-DD"), limit: int = 100):
    """Every tyre-related OJ publication since a date: spots documents that no
    extraction profile covers yet (new UN regulations, EU acts, trade measures)."""
    from regpilot.watcher.requirements import PROFILES
    from regpilot.watcher.sources.eurlex import scan_tyre_publications

    for r in scan_tyre_publications(since, limit):
        covered = any(f"No {p[1:]}" in r["title"].replace("\xa0", " ") for p in PROFILES)
        typer.echo(f"{r['date']}  {r['celex']:18} {'[profiled] ' if covered else ''}{r['title'][:100]}")


@app.command(name="limits")
def limits_cmd(regulation: str, products: Path = typer.Option(Path("sample_data/products")),
               brand: str = typer.Option(None), language: str = "eng"):
    """Hand-off preview: for every product size, the limit row each current
    requirement selects (after adjustments), next to the latest test value.
    No pass/fail. Rating is Agent 2's job."""
    from regpilot.impact.products import load_catalogue
    from regpilot.impact.rating import select_limit
    from regpilot.watcher.reader import WatcherReader

    reqs = WatcherReader(DATA / "watcher.db").current_requirements(regulation, language)
    for tyre in load_catalogue(products):
        if brand and tyre["brand"].lower() != brand.lower():
            continue
        typer.secho(f"\n{tyre['brand']} {tyre['range_name']} {tyre['designation']}  [{tyre['tyre_class']}]", bold=True)
        latest = {t["metric"]: t for t in tyre["tests"]}
        for r in reqs:
            if r.tyre_class != tyre["tyre_class"]:
                continue
            sel = select_limit(r, tyre)
            test = latest.get(r.metric)
            shown = f"test {test['value']:g} {test['unit']} ({test['test_date']})" if test else "no test"
            if sel.status == "selected":
                adj = f" incl. {sel.adjustments[0].delta:+g}" if sel.adjustments else ""
                cond = " / ".join(sel.row.conditions) or "(all)"
                typer.echo(f"  {r.id.split(':', 1)[1]:22} limit {r.comparator} {sel.effective_value:g}{adj:9} "
                           f"[{cond[:55]}]  {shown}")
            else:
                typer.secho(f"  {r.id.split(':', 1)[1]:22} {sel.status.upper()}: {sel.reason[:110]}", fg="yellow")


criteria_app = typer.Typer(help="Human review of LLM-proposed condition readings")
app.add_typer(criteria_app, name="criteria")


@criteria_app.command("propose")
def criteria_propose(regulation: str, language: str = "eng"):
    """Ask the LLM to propose readings for unparsed wording in stored versions."""
    from regpilot.watcher.graph import Watcher
    from regpilot.watcher.llm_criteria import make_llm_proposer

    for m in Watcher(DATA, proposer=make_llm_proposer()).propose_for_stored(regulation, language):
        typer.echo(f"  {m}")


@criteria_app.command("list")
def criteria_list(show_all: bool = typer.Option(False, "--all", help="include decided reviews")):
    """Proposals waiting for a decision (or all with --all)."""
    with _ro() as con:
        q = "SELECT * FROM criteria_reviews" + ("" if show_all else " WHERE status='proposed'") + " ORDER BY id"
        for r in con.execute(q):
            ctx, prop, checks = json.loads(r["context"]), json.loads(r["proposal"] or "{}"), json.loads(r["checks"])
            fields = {k: v for k, v in prop.items() if v is not None and k not in
                      ("fully_representable", "not_representable", "evidence")}
            typer.secho(f"\n#{r['id']} [{r['status']}] {r['condition_text']!r}", bold=True)
            typer.echo(f"   where:    {ctx.get('celex')} clause {ctx.get('clause')} {ctx.get('tyre_class')} "
                       f"stage {ctx.get('stage')}: {' / '.join(ctx.get('row_conditions', []))} -> {ctx.get('value')}")
            if ctx.get("footnote"):
                typer.echo(f"   footnote: {ctx['footnote']}")
            typer.echo(f"   LLM ({r['model']}): {fields}")
            typer.echo(f"   evidence: {prop.get('evidence')!r}")
            for c in checks:
                typer.secho(f"   CHECK:    {c}", fg="yellow")
            if r["status"] != "proposed":
                typer.echo(f"   decision: {r['status']} by {r['decided_by']} at {r['decided_at']}: {r['final_criteria']}")


def _reprocess_affected(text: str):
    from regpilot.watcher.graph import Watcher

    with _ro() as con:
        docs = [r[0] for r in con.execute("SELECT DISTINCT document_id FROM limits WHERE conditions LIKE ?",
                                          (f"%{text}%",))]
    w = Watcher(DATA)
    for d in docs:
        out = w.reprocess(d)
        if not out:
            typer.echo(f"   document #{d}: no change")
            continue
        n = len(out["changes"]["changed"])
        ev = out["event"]
        typer.echo(f"   document #{d}: {n} requirement(s) updated"
                   + (f"; EVENT {ev.type} (reason: criteria_reviewed)" if ev else " (history version: no event)"))


@criteria_app.command("approve")
def criteria_approve(review_id: int, by: str = typer.Option(..., help="your name (audit trail)"),
                     criteria: str = typer.Option(None, help='override as JSON, e.g. \'{"category_of_use": "snow"}\'')):
    """Approve a proposal (optionally corrected), then re-extract affected versions."""
    from regpilot.core.models import Criteria
    from regpilot.watcher.llm_criteria import LLMCriteria, to_criteria

    with wdb.connect(DATA / "watcher.db") as con:
        r = con.execute("SELECT * FROM criteria_reviews WHERE id=?", (review_id,)).fetchone()
        if not r or r["status"] != "proposed":
            raise typer.BadParameter(f"review #{review_id} is not open")
        final = (Criteria.model_validate_json(criteria) if criteria
                 else to_criteria(LLMCriteria.model_validate_json(r["proposal"])))
        if not final.constrained():
            raise typer.BadParameter("approved criteria must constrain at least one attribute")
        wdb.decide_review(con, review_id, "approved", by, final.model_dump(mode="json", exclude_none=True))
    typer.secho(f"approved #{review_id}: {r['condition_text']!r} -> {final.model_dump(exclude_none=True, exclude_defaults=True)}",
                fg="green")
    _reprocess_affected(r["condition_text"])


@criteria_app.command("reject")
def criteria_reject(review_id: int, by: str = typer.Option(..., help="your name (audit trail)")):
    """Reject a proposal: the condition stays 'unknown'."""
    with wdb.connect(DATA / "watcher.db") as con:
        wdb.decide_review(con, review_id, "rejected", by, None)
    typer.echo(f"rejected #{review_id}; the condition stays 'unknown'")


def _print_result(result):
    for m in result.messages:
        typer.echo(f"  {m}")
    for e in result.events:
        color = "cyan" if e.type == "regulation.backfilled" else "magenta" if e.type == "impact.proposed" else "green"
        typer.secho(f"  EVENT {e.type} id={e.id}", fg=color)
    for err in result.errors:
        typer.secho(f"  ERROR {err}", fg="red")


@app.command()
def impact(regulation: list[str] = typer.Argument(None, help="assess these (current version); omit = new watcher events"),
           products: Path = typer.Option(Path("sample_data/products")),
           as_of: str = typer.Option(None, help="YYYY-MM-DD (default today)")):
    """Run the Reg-Impact agent (Agent 2): rate product sizes, create a Gate 1 proposal."""
    from regpilot.impact.agent import ImpactAgent

    result = ImpactAgent().run({"assess": regulation or [], "products": products, "as_of": as_of,
                                "data_dir": DATA})
    _print_result(result)
    raise typer.Exit(0 if result.ok else 1)


@app.command(name="run")
def run_cmd(regulation: list[str], products: Path = typer.Option(Path("sample_data/products")),
            as_of: str = typer.Option(None, help="YYYY-MM-DD (default today)")):
    """Watcher then Impact: impact reacts to the watcher's events (Orchestrator)."""
    from regpilot.core.orchestrator import Orchestrator
    from regpilot.impact.agent import ImpactAgent
    from regpilot.watcher.agent import WatcherAgent

    ctx = Orchestrator([WatcherAgent(), ImpactAgent()]).run(
        {"regulations": regulation, "products": products, "as_of": as_of, "data_dir": DATA})
    for r in ctx["results"]:
        typer.secho(f"[{r.agent}]", bold=True)
        _print_result(r)
    if not any(e.type == "impact.proposed" for e in ctx["events"]):
        typer.echo("  (no new regulation version, so no new impact proposal. Use `regpilot impact R117` to re-assess)")


gate1_app = typer.Typer(help="Gate 1: human decision on impact proposals")
app.add_typer(gate1_app, name="gate1")


@gate1_app.command("list")
def gate1_list():
    """All proposals with their status."""
    from regpilot.impact import store

    with store.connect(DATA / "impact.db") as con:
        for r in con.execute("SELECT * FROM proposals ORDER BY created_at DESC"):
            typer.echo(f"{r['id']}  {r['status']:9} {r['regulation']} {r['celex']:15} as of {r['as_of']}  "
                       f"fail {r['fails']} · not proven {r['unresolved']} · pass {r['passes']}  trigger={r['trigger'][:10]}")


@gate1_app.command("show")
def gate1_show(proposal_id: str):
    """Print the proposal report (also in data/gate1/<id>/report.md)."""
    from regpilot.impact import store
    from regpilot.impact.report import report_md

    with store.connect(DATA / "impact.db") as con:
        p, status = store.load_proposal(con, proposal_id)
    typer.echo(report_md(p, status))


def _gate1(fn, *args, **kw):
    from regpilot.impact.gate1 import Gate1Error

    try:
        p = fn(DATA, *args, **kw)
    except (Gate1Error, LookupError) as e:
        typer.secho(f"refused: {e}", fg="red")
        raise typer.Exit(1)
    s = p.summary
    typer.secho(f"{p.id}: {fn.__name__} recorded · fail {s['fail']} · not proven {s['unresolved']} · "
                f"accepted risk {s['accepted_risk']} · pass {s['pass']}  (data/gate1/{p.id}/)", fg="green")


@gate1_app.command("approve")
def gate1_approve(proposal_id: str, by: str = typer.Option(...), notes: str = typer.Option(None)):
    """Approve a proposal (final)."""
    from regpilot.impact.gate1 import approve

    _gate1(approve, proposal_id, by, notes)


@gate1_app.command("reject")
def gate1_reject(proposal_id: str, by: str = typer.Option(...), notes: str = typer.Option(...)):
    """Reject a proposal (final, needs notes)."""
    from regpilot.impact.gate1 import reject

    _gate1(reject, proposal_id, by, notes)


@gate1_app.command("edit")
def gate1_edit(proposal_id: str, by: str = typer.Option(...), size: str = typer.Option(...),
               requirement: str = typer.Option(...), verdict: str = typer.Option(...),
               reason: str = typer.Option(...)):
    """Override one verdict (size × requirement) with a reason. Allowed: pass, fail, accepted_risk,
    not_applicable, unknown."""
    from regpilot.impact.gate1 import Edit, edit

    _gate1(edit, proposal_id, by, [Edit(size_id=size, requirement_id=requirement, verdict=verdict, reason=reason)])


@app.command(name="app")
def app_cmd(port: int = 8501):
    """Open the review app (Streamlit). Needs: uv sync --extra ui"""
    import subprocess
    import sys

    try:
        import streamlit  # noqa: F401
    except ImportError:
        typer.secho("The app needs the 'ui' extra:  uv run --extra ui regpilot app", fg="red")
        raise typer.Exit(1)
    script = Path(__file__).resolve().parents[3] / "app" / "regpilot_app.py"
    subprocess.run([sys.executable, "-m", "streamlit", "run", str(script), "--server.port", str(port)])


@app.command()
def graph(agent: str = typer.Option("watcher", help="watcher | impact")):
    """Print an agent's LangGraph as Mermaid (paste into mermaid.live)."""
    if agent == "impact":
        from regpilot.impact.graph import ImpactAssessor

        typer.echo(ImpactAssessor(DATA, Path("sample_data/products")).build().get_graph().draw_mermaid())
        return
    from regpilot.watcher.graph import Watcher

    typer.echo(Watcher(DATA).build().get_graph().draw_mermaid())


if __name__ == "__main__":
    app()
