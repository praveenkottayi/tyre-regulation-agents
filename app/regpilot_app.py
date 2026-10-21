"""
regpilot review app (Streamlit).

    uv run --extra ui regpilot app          (or: uv run --extra ui streamlit run app/regpilot_app.py)

Reads watcher.db / impact.db and calls the same library functions as the CLI.
Every decision (wording review, Gate 1) needs the reviewer name in the sidebar
and is written to the audit trail.
"""

import json
import os
from datetime import date
from pathlib import Path

import pandas as pd
import streamlit as st

from regpilot.impact import gate1, store
from regpilot.impact.products import load_catalogue
from regpilot.impact.rating import select_limit
from regpilot.watcher import db as wdb

ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.getenv("REGPILOT_DATA", ROOT / "data"))
PRODUCTS = Path(os.getenv("REGPILOT_PRODUCTS", ROOT / "sample_data" / "products"))

st.set_page_config(page_title="regpilot", page_icon="🛞", layout="wide")

VERDICT_ICON = {"pass": "✅ pass", "fail": "❌ fail", "accepted_risk": "🟠 accepted risk", "missing_test": "❓ missing test",
                "expired_test": "⌛ expired test", "invalid_test": "🚫 invalid test", "incomparable": "≠ incomparable",
                "unknown": "❓ unknown", "ambiguous": "⚠️ ambiguous", "not_defined": "— not defined",
                "not_applicable": "— not applicable"}
TIMING_TEXT = {"in_force": "in force", "new_approvals_only": "new approvals only", "upcoming": "upcoming"}


# --- data access -------------------------------------------------------------------------------------
def wcon():
    return wdb.connect_readonly(DATA / "watcher.db") if (DATA / "watcher.db").exists() else None


def q(sql: str, params=()) -> pd.DataFrame:
    con = wcon()
    if con is None:
        return pd.DataFrame()
    try:
        return pd.read_sql_query(sql, con, params=params)
    finally:
        con.close()


def iq(sql: str, params=()) -> pd.DataFrame:
    if not (DATA / "impact.db").exists():
        return pd.DataFrame()
    with store.connect(DATA / "impact.db") as con:
        return pd.read_sql_query(sql, con, params=params)


def need_reviewer() -> str | None:
    name = st.session_state.get("reviewer", "").strip()
    if not name:
        st.warning("Enter your name in the sidebar first: every decision is recorded with it.")
    return name or None


# --- sidebar ---------------------------------------------------------------------------------------------
with st.sidebar:
    st.title("🛞 regpilot")
    st.caption("Tyre regulation watcher + impact")
    page = st.radio("Page", ["🏠 Overview", "📜 Regulation", "🧠 Wording reviews", "📊 Impact & Gate 1", "🛞 Products"],
                    label_visibility="collapsed")
    st.divider()
    st.text_input("Reviewer name", key="reviewer", placeholder="needed for decisions")
    st.caption(f"Data: `{DATA}`  \nProducts: `{PRODUCTS.name}/` (synthetic)")


# =========================================================================================================
if page == "🏠 Overview":
    st.header("Overview")
    docs = q("SELECT regulation, celex, pub_date, language, status FROM documents ORDER BY pub_date")
    reviews = q("SELECT status FROM criteria_reviews") if not docs.empty else pd.DataFrame()
    props = iq("SELECT status FROM proposals")
    current = q("SELECT celex, pub_date FROM current_documents WHERE language='eng'")

    c = st.columns(5)
    c[0].metric("Versions stored", len(docs))
    c[1].metric("Current R117", current.iloc[0]["pub_date"] if not current.empty else "-")
    c[2].metric("Requirements (current)", int(q("SELECT COUNT(*) n FROM current_requirements WHERE language='eng'")
                                           .iloc[0]["n"]) if not docs.empty else 0)
    c[3].metric("Wording reviews open", int((reviews["status"] == "proposed").sum()) if not reviews.empty else 0)
    c[4].metric("Proposals awaiting Gate 1",
                int(props["status"].isin(["proposed", "edited"]).sum()) if not props.empty else 0)

    st.subheader("Run the agents")
    a, b, _ = st.columns([1, 1, 2])
    as_of = a.date_input("As of", value=date.today())
    if b.button("🔭 Watcher (R117)", width="stretch"):
        from regpilot.watcher.agent import WatcherAgent

        with st.spinner("Watching R117…"):
            res = WatcherAgent().run({"regulations": ["R117"], "data_dir": DATA, "as_of": as_of.isoformat()})
        st.code("\n".join(res.messages + [f"EVENT {e.type}" for e in res.events] + res.errors) or "done")
    if b.button("📊 Impact (new events)", width="stretch"):
        from regpilot.impact.agent import ImpactAgent

        with st.spinner("Assessing…"):
            res = ImpactAgent().run({"data_dir": DATA, "products": PRODUCTS, "as_of": as_of.isoformat()})
        st.code("\n".join(res.messages + [f"EVENT {e.type}" for e in res.events] + res.errors) or "done")
    if b.button("📊 Re-assess R117 now", width="stretch"):
        from regpilot.impact.agent import ImpactAgent

        with st.spinner("Assessing…"):
            res = ImpactAgent().run({"assess": ["R117"], "data_dir": DATA, "products": PRODUCTS,
                                     "as_of": as_of.isoformat()})
        st.code("\n".join(res.messages + [f"EVENT {e.type}" for e in res.events] + res.errors) or "done")

    left, right = st.columns(2)
    with left:
        st.subheader("Versions")
        st.dataframe(docs, hide_index=True, width="stretch")
    with right:
        st.subheader("Events (signals between agents)")
        ev = q("SELECT created_at, type, regulation, payload FROM events ORDER BY created_at DESC LIMIT 20")
        if not ev.empty:
            ev["version"] = ev["payload"].map(lambda p: json.loads(p)["celex"])
            ev["changes"] = ev["payload"].map(lambda p: (lambda c: f"+{len(c['added'])} −{len(c['removed'])} "
                                                         f"~{len(c['changed'])}" if c else "first")(json.loads(p).get("changes")))
            st.dataframe(ev.drop(columns="payload"), hide_index=True, width="stretch")
    st.subheader("Recent watcher runs")
    st.dataframe(q("SELECT id, regulation, finished_at, status FROM runs ORDER BY id DESC LIMIT 10"),
                 hide_index=True, width="stretch")


# =========================================================================================================
elif page == "📜 Regulation":
    st.header("Regulation: R117")
    docs = q("SELECT id, celex, pub_date, language FROM documents WHERE status='complete' ORDER BY pub_date DESC, language")
    if docs.empty:
        st.info("Nothing stored yet. Run the watcher from the Overview page.")
        st.stop()
    labels = {f"{r.pub_date} · {r.celex} · {r.language}": r.id for r in docs.itertuples()}
    tab_req, tab_diff, tab_clause = st.tabs(["Requirements & limits", "What changed", "Legal text"])

    with tab_req:
        choice = st.selectbox("Version", list(labels))
        doc_id = labels[choice]
        lim = q("SELECT requirement_id, tyre_class, stage, metric, conditions, comparator, value, raw_value, unit, criteria "
                "FROM limits WHERE document_id=? ORDER BY requirement_id, row_no", (doc_id,))
        if lim.empty:
            st.warning("No requirements extracted for this version (e.g. a language the rules don't cover).")
        else:
            f1, f2, f3 = st.columns(3)
            cls = f1.multiselect("Tyre class", sorted(lim["tyre_class"].unique()), default=sorted(lim["tyre_class"].unique()))
            met = f2.multiselect("Metric", sorted(lim["metric"].unique()), default=sorted(lim["metric"].unique()))
            stages = f3.multiselect("Stage", sorted(lim["stage"].fillna("unlabelled").unique()),
                                    default=sorted(lim["stage"].fillna("unlabelled").unique()))
            view = lim[lim["tyre_class"].isin(cls) & lim["metric"].isin(met) & lim["stage"].fillna("unlabelled").isin(stages)]
            view = view.assign(limit=view["comparator"] + " " + view["raw_value"] + " " + view["unit"],
                               readable=view["criteria"].map(lambda c: "✅" if "unparsed" not in (c or "") else "❓ unknown"))
            st.dataframe(view[["requirement_id", "tyre_class", "stage", "conditions", "limit", "readable", "criteria"]],
                         hide_index=True, width="stretch", height=480)
            reqs = q("SELECT requirement_id, data FROM requirements WHERE document_id=? ORDER BY requirement_id", (doc_id,))
            pick = st.selectbox("Requirement details (quotes, adjustments, dates)", reqs["requirement_id"])
            r = json.loads(reqs.set_index("requirement_id").loc[pick, "data"])
            st.markdown("**Adjustments**: " + ("; ".join(f"{a['delta']:+g} {a['unit'] or ''} for {a['applies_to']}"
                                                          for a in r["adjustments"]) or "none"))
            st.markdown("**Applies**: " + ("; ".join(f"{a['scope'].replace('_', ' ')} from {a['date']}"
                                                     for a in r["applicability"]) or "no transition date in this version"))
            for qt in r["quotes"]:
                st.caption(f"{qt['section_key']} · {qt['celex']}")
                st.markdown(f"> {qt['text']}")

    with tab_diff:
        eng = docs[docs["language"] == "eng"]
        opts = {f"{r.pub_date} · {r.celex}": r.id for r in eng.itertuples()}
        if len(opts) < 2:
            st.info("Store at least two versions to compare (Overview → watcher, or `regpilot watch R117 --all-versions`).")
        else:
            keys = list(opts)
            a, b = st.columns(2)
            new = a.selectbox("Newer", keys, index=0)
            old = b.selectbox("Older", keys, index=1)
            from regpilot.watcher.diff import diff

            con = wcon()
            d = diff(wdb.load_requirements(con, opts[old]), wdb.load_requirements(con, opts[new]))
            con.close()
            m = st.columns(4)
            m[0].metric("Added", len(d["added"]))
            m[1].metric("Removed", len(d["removed"]))
            m[2].metric("Changed", len(d["changed"]))
            m[3].metric("Unchanged", len(d["unchanged"]))
            st.markdown("**Added:** " + ", ".join(f"`{i.split(':', 1)[1]}`" for i in d["added"]) or "none")
            st.markdown("**Removed:** " + ", ".join(f"`{i.split(':', 1)[1]}`" for i in d["removed"]) or "none")
            for rid, notes in d["changed"].items():
                with st.expander(f"Changed: {rid}"):
                    for n in notes:
                        st.markdown(f"- {n}")

    with tab_clause:
        choice = st.selectbox("Version ", list(labels))
        number = st.text_input("Clause number", "6.1")
        secs = q("SELECT key, number, heading, text, tables FROM sections WHERE document_id=? AND (key=? OR key LIKE ?) "
                 "ORDER BY ord", (labels[choice], f"main:{number}", f"main:{number}.%"))
        st.caption("Verbatim text as stored by the watcher. Compare with EUR-Lex: "
                   f"https://eur-lex.europa.eu/legal-content/EN/TXT/?uri=CELEX:{choice.split(' · ')[1]}")
        for s in secs.itertuples():
            st.markdown(f"#### {s.number} {s.heading or ''}")
            st.text(s.text if isinstance(s.text, str) else "")
            for t in json.loads(s.tables) if isinstance(s.tables, str) else []:  # NULL arrives as NaN
                rows = t["rows"]
                # shown exactly as printed (merged cells repeated); header rows are the first ones
                st.table(pd.DataFrame(rows, columns=[f"col {i + 1}" for i in range(len(rows[0]))]))


# =========================================================================================================
elif page == "🧠 Wording reviews":
    st.header("Wording reviews (LLM suggestions)")
    st.caption("The rules couldn't read these conditions. An LLM suggested a reading; nothing is used until a person approves. "
               "Pending or rejected → the row matches as 'unknown'.")
    if (DATA / "watcher.db").exists() and st.button("🧠 Ask the LLM about unread wording (Ollama)"):
        from regpilot.watcher.graph import Watcher
        from regpilot.watcher.llm_criteria import make_llm_proposer

        with st.spinner("Asking the LLM…"):
            msgs = Watcher(DATA, proposer=make_llm_proposer()).propose_for_stored("R117")
        st.code("\n".join(msgs))
    revs = q("SELECT * FROM criteria_reviews ORDER BY CASE status WHEN 'proposed' THEN 0 ELSE 1 END, id")
    if revs.empty:
        st.success("Nothing to review: the rules understood every condition (or the LLM step hasn't run yet).")
    for r in revs.itertuples():
        ctx, prop, checks = json.loads(r.context), json.loads(r.proposal or "{}"), json.loads(r.checks)
        fields = {k: v for k, v in prop.items() if v is not None and k not in ("fully_representable", "not_representable",
                                                                               "evidence")}
        with st.container(border=True):
            st.markdown(f"**#{r.id} · `{r.condition_text}`** · status **{r.status}**")
            st.caption(f"{ctx.get('celex')} · clause {ctx.get('clause')} · {ctx.get('tyre_class')} · stage {ctx.get('stage')} · "
                       f"row: {' / '.join(ctx.get('row_conditions', []))} → {ctx.get('value')}")
            if ctx.get("footnote"):
                st.markdown(f"Footnote: _{ctx['footnote']}_")
            st.markdown(f"LLM ({r.model}) proposes: `{fields or 'no fields'}` · evidence: _{prop.get('evidence')}_")
            if prop.get("not_representable"):
                st.markdown(f"LLM says it can't be fully expressed: _{prop['not_representable']}_")
            for c in checks:
                st.warning(c)
            if r.status == "proposed":
                a, b, c = st.columns([2, 1, 1])
                override = a.selectbox("Approve as", ["(the LLM proposal)", "category_of_use = snow", "category_of_use = normal",
                                                      "category_of_use = special_use"], key=f"ov{r.id}")
                if b.button("✅ Approve", key=f"ap{r.id}") and (who := need_reviewer()):
                    from regpilot.core.models import Criteria
                    from regpilot.watcher.graph import Watcher
                    from regpilot.watcher.llm_criteria import LLMCriteria, to_criteria

                    final = (to_criteria(LLMCriteria.model_validate(prop)) if override.startswith("(")
                             else Criteria(category_of_use=override.split(" = ")[1]))
                    if not final.constrained():
                        st.error("The approved reading must set at least one field. Pick an 'Approve as' option.")
                    else:
                        con = wdb.connect(DATA / "watcher.db")
                        wdb.decide_review(con, r.id, "approved", who, final.model_dump(mode="json", exclude_none=True))
                        doc_ids = [x[0] for x in con.execute("SELECT DISTINCT document_id FROM limits WHERE conditions LIKE ?",
                                                             (f"%{r.condition_text}%",))]
                        con.close()
                        outs = [Watcher(DATA).reprocess(d) for d in doc_ids]
                        st.success(f"Approved. Re-extracted {len(doc_ids)} version(s); events: "
                                   f"{[o['event'].type for o in outs if o and o['event']] or 'none (history versions)'}")
                        st.rerun()
                if c.button("❌ Reject", key=f"rj{r.id}") and (who := need_reviewer()):
                    con = wdb.connect(DATA / "watcher.db")
                    wdb.decide_review(con, r.id, "rejected", who, None)
                    con.close()
                    st.rerun()
            else:
                st.caption(f"Decided: {r.status} by {r.decided_by} at {r.decided_at} → {r.final_criteria}")


# =========================================================================================================
elif page == "📊 Impact & Gate 1":
    st.header("Impact proposals & Gate 1")
    props = iq("SELECT id, status, regulation, celex, as_of, fails, unresolved, passes, trigger, created_at "
               "FROM proposals ORDER BY created_at DESC")
    if props.empty:
        st.info("No proposals yet. Overview → 'Impact' or 'Re-assess R117 now'.")
        st.stop()
    st.dataframe(props, hide_index=True, width="stretch")
    pid = st.selectbox("Proposal", props["id"], format_func=lambda i: f"{i} · " + " · ".join(
        str(v) for v in props.set_index("id").loc[i, ["status", "celex", "as_of"]]))
    with store.connect(DATA / "impact.db") as con:
        p, status = store.load_proposal(con, pid)
        log = pd.read_sql_query("SELECT at, action, reviewer, notes, edits FROM gate1_log WHERE proposal_id=? ORDER BY id",
                                con, params=(pid,))
    s = p.summary
    st.markdown(f"**Status: `{status}`** · {p.regulation} version {p.celex} · rated as of **{p.as_of}** · "
                f"trigger `{p.trigger[:12]}`")
    m = st.columns(4)
    m[0].metric("❌ Fail", s["fail"])
    m[1].metric("❓ Not proven compliant", s["unresolved"])
    m[2].metric("🟠 Accepted risk", s["accepted_risk"])
    m[3].metric("✅ Pass", s["pass"])
    st.caption(" · ".join(f"{a.market}: **{a.status}**" for a in p.applicability)
               + (f" · skipped (replaced stages): {', '.join(k.split(':', 1)[1] for k in p.superseded)}" if p.superseded else ""))
    if p.unassessed_ranges:
        st.warning("Not assessed (no market where applicability is established): "
                   + ", ".join(f"{r['brand']} {r['range_name']}" for r in p.unassessed_ranges))

    df = pd.DataFrame([i.model_dump(mode="json") for i in p.impacts])
    df["verdict_shown"] = (df["reviewer_verdict"].fillna(df["verdict"])).map(lambda v: VERDICT_ICON.get(v, v))
    df["when"] = df.apply(lambda r: TIMING_TEXT[r["timing"]] + (f" (all from {r['all_from']})" if r["all_from"]
                                                                and r["timing"] != "in_force" else ""), axis=1)
    df["limit_txt"] = df.apply(lambda r: f"{r['comparator']} {r['limit']:g} {r['unit']}" if pd.notna(r["limit"]) else "-", axis=1)
    df["req"] = df["requirement_id"].str.split(":", n=1).str[1]
    f1, f2, f3, f4 = st.columns(4)
    brands = f1.multiselect("Brand", sorted(df["brand"].unique()), default=sorted(df["brand"].unique()))
    classes = f2.multiselect("Class", sorted(df["tyre_class"].unique()), default=sorted(df["tyre_class"].unique()))
    vs = f3.multiselect("Verdict", sorted(df["verdict_shown"].unique()), default=sorted(df["verdict_shown"].unique()))
    only_new = f4.checkbox("Only requirements new in this version", value=False)
    view = df[df["brand"].isin(brands) & df["tyre_class"].isin(classes) & df["verdict_shown"].isin(vs)]
    if only_new:
        view = view[view["requirement_change"] == "added"]
    order = {"❌ fail": 0}
    view = view.sort_values(by=["verdict_shown", "size_id"], key=lambda c: c.map(lambda v: order.get(v, 1)) if c.name == "verdict_shown" else c)
    st.dataframe(view[["verdict_shown", "brand", "range_name", "designation", "tyre_class", "req", "when", "limit_txt",
                       "test_value", "test_date", "margin", "reason", "reviewer_reason", "requirement_change"]],
                 hide_index=True, width="stretch", height=460,
                 column_config={"verdict_shown": "verdict", "req": "requirement", "limit_txt": "limit",
                                "range_name": "range", "requirement_change": "change"})
    folder = DATA / "gate1" / pid
    d1, d2, _ = st.columns([1, 1, 3])
    if (folder / "impact.csv").exists():
        d1.download_button("⬇️ impact.csv", (folder / "impact.csv").read_bytes(), f"impact_{pid}.csv", "text/csv")
        d2.download_button("⬇️ report.md", (folder / "report.md").read_bytes(), f"report_{pid}.md", "text/markdown")

    st.subheader("Gate 1 decision")
    if status not in ("proposed", "edited"):
        st.info(f"This proposal is **{status}** (final).")
    else:
        with st.form("edit"):
            st.markdown("**Edit one verdict** (the agent's verdict is kept next to yours)")
            keys = [f"{i.size_id} · {i.requirement_id} · {i.brand} {i.range_name} {i.designation} · {i.verdict}" for i in p.impacts]
            target = st.selectbox("Assessment", keys)
            verdict = st.selectbox("Your verdict", sorted(gate1.REVIEWER_VERDICTS))
            reason = st.text_input("Reason (required, for the audit trail)")
            if st.form_submit_button("💾 Save edit") and (who := need_reviewer()):
                size_id, req_id = target.split(" · ")[:2]
                try:
                    gate1.edit(DATA, pid, who, [gate1.Edit(size_id=size_id, requirement_id=req_id, verdict=verdict, reason=reason)])
                    st.rerun()
                except gate1.Gate1Error as e:
                    st.error(str(e))
        notes = st.text_input("Notes (required to reject)")
        a, b, _ = st.columns([1, 1, 2])
        if a.button("✅ Approve proposal", type="primary", width="stretch") and (who := need_reviewer()):
            try:
                gate1.approve(DATA, pid, who, notes or None)
                st.rerun()
            except gate1.Gate1Error as e:
                st.error(str(e))
        if b.button("❌ Reject proposal", width="stretch") and (who := need_reviewer()):
            try:
                gate1.reject(DATA, pid, who, notes)
                st.rerun()
            except gate1.Gate1Error as e:
                st.error(str(e))
    st.subheader("Audit trail")
    if log.empty:
        st.caption("No decisions yet.")
    else:
        st.dataframe(log, hide_index=True, width="stretch")


# =========================================================================================================
elif page == "🛞 Products":
    st.header("Products and their applicable limits")
    st.caption("Synthetic sample catalogue (real family names, invented sizes and tests). "
               "Shows which limit row applies to each size, with no pass/fail. That's on the Impact page.")
    tyres = load_catalogue(PRODUCTS)
    cat = pd.DataFrame([{k: v for k, v in t.items() if k != "tests"} | {"tests": len(t["tests"])} for t in tyres])
    st.dataframe(cat, hide_index=True, width="stretch")
    if (DATA / "watcher.db").exists():
        from regpilot.watcher.reader import WatcherReader

        reqs = WatcherReader(DATA / "watcher.db").current_requirements("R117")
        size = st.selectbox("Size", [f"{t['size_id']} · {t['brand']} {t['range_name']} {t['designation']}" for t in tyres])
        tyre = next(t for t in tyres if t["size_id"] == size.split(" · ")[0])
        rows = []
        for r in reqs:
            if r.tyre_class != tyre["tyre_class"]:
                continue
            sel = select_limit(r, tyre)
            rows.append({"requirement": r.id.split(":", 1)[1], "status": sel.status,
                         "limit": f"{r.comparator} {sel.effective_value:g} {r.unit}" if sel.effective_value is not None else "-",
                         "row": " / ".join(sel.row.conditions) if sel.row else "", "adjustment":
                         "; ".join(f"{a.delta:+g} {a.unit or ''}" for a in sel.adjustments), "why": sel.reason})
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
        st.markdown("**Tests**")
        st.dataframe(pd.DataFrame(tyre["tests"]), hide_index=True, width="stretch")
