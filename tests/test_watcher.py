import sqlite3

import pytest

from regpilot.core.models import EVENT_NEW_VERSION, EVENT_UPDATED
from regpilot.core.orchestrator import Orchestrator
from regpilot.watcher import db
from regpilot.watcher.agent import WatcherAgent
from regpilot.watcher.diff import diff
from regpilot.watcher.graph import Watcher
from regpilot.watcher.parsers import oj_xhtml
from regpilot.watcher.requirements import ExtractionError, extract

XHTML = '<?xml version="1.0" encoding="UTF-8"?><html xmlns="http://www.w3.org/1999/xhtml"><body>{}</body></html>'


def clause_row(number, body):
    return (f'<table><tbody><tr><td/><td><p class="oj-normal">{number}</p></td>'
            f'<td><span>{body}</span></td></tr></tbody></table>')


def by_id(reqs):
    return {r.id: r for r in reqs}


# --- parser ------------------------------------------------------------------------------
def test_parses_both_clause_layouts(doc2025, doc2016):
    # 2025: layout tables; 2016: inline paragraphs. Same keys either way.
    for doc in (doc2025, doc2016):
        s = doc.section("main:6.1.1")
        assert s.text.startswith("For")
        assert "C1" in s.text
        assert s.tables, "limit table must be attached to its clause"
    assert doc2025.oj_reference == "2025/1453" and doc2025.language == "EN"


def test_rowspan_and_colspan_are_expanded(doc2025):
    stage3 = doc2025.section("main:6.3").tables[1]
    data = stage3.rows[stage3.header_rows:]
    assert all(row[0] in ("C1", "C2", "C3") for row in data), "every row must carry its tyre class"
    assert ["C1", "load capacity index < 87"] == stage3.rows[2][:2]


def test_toc_rows_are_front_matter_not_clauses():
    doc = oj_xhtml.parse(XHTML.format(
        clause_row("6.", "Requirements") +  # table-of-contents row
        '<p class="oj-ti-grseq-1">1.   Scope</p>' + clause_row("1.1.", "Real clause")).encode())
    assert doc.section("front:6") is not None
    assert doc.section("main:6") is None
    assert doc.section("main:1.1").text == "Real clause"


def test_annex_markers_in_other_languages_scope_clauses():
    doc = oj_xhtml.parse(XHTML.format(
        '<p class="oj-ti-grseq-1">1.   Anwendungsbereich</p>' + clause_row("1.1.", "Haupttext") +
        '<p class="oj-doc-ti">ANHANG 3</p>' + clause_row("1.1.", "Anhangtext")).encode())
    assert doc.section("main:1.1").text == "Haupttext"  # not merged with the annex clause
    assert doc.section("annex-3:1.1").text == "Anhangtext"


def test_duplicate_main_clause_is_kept_and_reported():
    doc = oj_xhtml.parse(XHTML.format(
        '<p class="oj-ti-grseq-1">1.   Scope</p>' + clause_row("1.1.", "first") + clause_row("1.1.", "second")).encode())
    assert doc.section("main:1.1").text == "first"
    assert doc.section("main:1.1~2").text == "second"
    assert doc.warnings


# --- extraction ---------------------------------------------------------------------------------
def test_r117_2025_requirements(doc2025):
    res = extract(doc2025, "R117", celex="42025X1453")
    assert res.warnings == []
    reqs = by_id(res.requirements)
    assert len(reqs) == 15

    noise = reqs["R117:6.1:C1:stage-2"]
    assert (noise.metric, noise.unit, noise.comparator, noise.clause) == ("rolling_sound_emission", "dB(A)", "<=", "6.1.1")
    assert [(lr.conditions, lr.value) for lr in noise.limits] == [
        (["185 and lower"], 70), (["Over 185 up to 245"], 71), (["Over 245 up to 275"], 72), (["Over 275"], 74)]
    assert noise.adjustments[0].delta == 1.0 and noise.adjustments[0].unit == "dB(A)"
    assert "extra load tyres" in noise.adjustments[0].applies_to
    assert noise.adjustments[0].quote.text.startswith("The above limits shall be increased by 1 dB(A)")

    c2 = {tuple(lr.conditions): lr.value for lr in reqs["R117:6.1:C2:stage-2"].limits}
    assert c2[("Normal tyre", "Other")] == 72 and c2[("Normal tyre", "Traction tyres")] == 73

    wet1, wet2 = reqs["R117:6.2:C1:stage-1"], reqs["R117:6.2:C1:stage-2"]
    assert wet2.previous_stage == "1" and wet2.comparator == ">="
    assert {tuple(lr.conditions): lr.value for lr in wet2.limits}[("Normal tyre",)] == 1.2
    assert any(lr.value is None and lr.raw_value == "Not defined" for lr in wet1.limits)

    rr3 = reqs["R117:6.3:C1:stage-3"]
    assert {tuple(lr.conditions): lr.value for lr in rr3.limits}[("load capacity index < 87",)] == 10.0
    assert rr3.previous_stage == "2" and rr3.adjustments[0].delta == 1.0

    dates = {(str(a.date), a.scope) for a in rr3.applicability}
    assert dates == {("2024-07-07", "new_approvals"), ("2026-07-07", "all_approvals")}
    c3_dates = {(str(a.date), a.scope) for a in reqs["R117:6.2:C3:stage-2"].applicability}
    assert ("2028-09-01", "all_approvals") in c3_dates
    assert reqs["R117:6.1:C1:stage-2"].applicability == []  # 12.x only dates stage 2 wet grip / stage 3 RR

    for r in reqs.values():  # every requirement is quoted from the text
        assert r.quotes and all(q.celex == "42025X1453" and q.text for q in r.quotes)


def test_r117_2016_stage_from_text_and_stable_ids(doc2016):
    reqs = by_id(extract(doc2016, "R117").requirements)
    rr1, rr2 = reqs["R117:6.3:C1:stage-1"], reqs["R117:6.3:C1:stage-2"]
    assert (rr1.clause, rr1.limits[0].value) == ("6.3.1", 12.0)  # stage read from "values for stage 1"
    assert (rr2.clause, rr2.limits[0].value, rr2.previous_stage) == ("6.3.2", 10.5, "1")
    assert "R117:6.2:C1:stage-unlabelled" in reqs  # no stage printed -> not invented


def test_diff_2016_to_2025(doc2016, doc2025):
    d = diff(extract(doc2016, "R117").requirements, extract(doc2025, "R117").requirements)
    assert "R117:6.3:C1:stage-3" in d["added"] and "R117:6.2:C1:stage-2" in d["added"]
    assert "R117:6.1:C1:stage-1" in d["removed"] and "R117:6.3:C1:stage-1" in d["removed"]
    assert any("limit row added" in n for n in d["changed"]["R117:6.1:C2:stage-2"])
    assert "R117:6.3:C1:stage-2" in d["changed"]  # same 10,5 limit, reworded snow-tyre adjustment


def test_comparator_contradiction_fails_loudly(doc2025):
    table = doc2025.section("main:6.2.1").tables[0]
    row = next(r for r in table.rows if r[-1].startswith("≥"))
    row[-1] = row[-1].replace("≥", "≤")
    with pytest.raises(ExtractionError, match="contradicts"):
        extract(doc2025, "R117")


def test_refuses_unprofiled_regulation_and_unsupported_language(doc2025):
    with pytest.raises(ExtractionError, match="no extraction profile"):
        extract(doc2025, "R30")
    doc2025.language = "DE"
    with pytest.raises(ExtractionError, match="written for"):
        extract(doc2025, "R117")


# --- the LangGraph watcher --------------------------------------------------------------------------
def events(tmp_path):
    with db.connect(tmp_path / "watcher.db") as con:
        return con.execute("SELECT type, document_id FROM events ORDER BY created_at").fetchall()


def test_graph_first_run_then_noop(tmp_path, source):
    w = Watcher(tmp_path, sources=[source])
    state = w.run("R117")
    assert state["status"] == "complete"
    assert [e["type"] for e in state["events"]] == [EVENT_NEW_VERSION]
    assert state["events"][0]["payload"]["changes"] is None  # nothing to compare against
    assert len(state["events"][0]["payload"]["requirement_ids"]) == 15

    again = w.run("R117")
    assert again["status"] == "unchanged" and again["events"] == []
    assert source.fetches == ["42025X1453"], "an already-complete version must not be re-downloaded"


def test_graph_as_of_then_new_version_with_diff(tmp_path, source):
    w = Watcher(tmp_path, sources=[source])
    old = w.run("R117", as_of="2020-01-01")
    assert old["target"]["source_id"] == "42016X0812(01)"
    new = w.run("R117")
    payload = new["events"][0]["payload"]
    assert new["events"][0]["type"] == EVENT_NEW_VERSION
    assert payload["previous_celex"] == "42016X0812(01)"
    assert "R117:6.3:C1:stage-3" in payload["changes"]["added"]


def test_graph_force_refetch_same_and_changed_content(tmp_path, source):
    w = Watcher(tmp_path, sources=[source])
    first = w.run("R117")
    same = w.run("R117", force=True)
    assert same["status"] == "unchanged" and same["events"] == []

    source.content["42025X1453"] = source.content["42025X1453"].replace(b">70 <", b">69 <", 1)  # a corrigendum
    upd = w.run("R117", force=True)
    ev = upd["events"][0]
    assert ev["type"] == EVENT_UPDATED
    assert ev["payload"]["previous_document_id"] == first["document_id"]
    assert ev["payload"]["changes"]["changed"]["R117:6.1:C1:stage-2"] == ["limit changed: 185 and lower 70 -> 69"]
    assert [e["type"] for e in events(tmp_path)] == [EVENT_NEW_VERSION, EVENT_UPDATED]


def test_graph_crash_before_emit_is_redone_once(tmp_path, source, monkeypatch):
    w = Watcher(tmp_path, sources=[source])
    monkeypatch.setattr(db, "emit_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("power cut")))
    with pytest.raises(RuntimeError):
        w.run("R117")
    with db.connect(tmp_path / "watcher.db") as con:
        assert con.execute("SELECT status FROM documents").fetchone()["status"] == "pending"
    monkeypatch.undo()

    state = w.run("R117")  # pending is not 'known' -> processed again
    assert [e["type"] for e in state["events"]] == [EVENT_NEW_VERSION]
    with db.connect(tmp_path / "watcher.db") as con:
        assert con.execute("SELECT COUNT(*) n FROM documents").fetchone()["n"] == 1  # leftover replaced
    assert len(events(tmp_path)) == 1


def test_graph_extraction_failure_stores_but_emits_nothing(tmp_path, source):
    source.content["42025X1453"] = source.content["42025X1453"].replace(
        b'<p class="oj-hd-lg">EN</p>', b'<p class="oj-hd-lg">DE</p>')
    state = Watcher(tmp_path, sources=[source]).run("R117")
    assert state["status"] == "extraction_failed"
    assert state["events"] == [] and events(tmp_path) == []
    assert any("extraction failed" in w for w in state["warnings"])


def test_graph_not_found(tmp_path, source):
    state = Watcher(tmp_path, sources=[source]).run("R999")
    assert state["status"] == "not_found" and source.fetches == []


# --- agent contract ------------------------------------------------------------------------------------
def test_watcher_agent_in_orchestrator(tmp_path, source):
    ctx = Orchestrator([WatcherAgent(sources=[source])]).run({"regulations": ["R117"], "data_dir": tmp_path})
    assert [e.type for e in ctx["events"]] == [EVENT_NEW_VERSION]
    assert ctx["results"][0].ok


def test_readonly_connection_cannot_write(tmp_path, source):
    Watcher(tmp_path, sources=[source]).run("R117")
    con = db.connect_readonly(tmp_path / "watcher.db")
    with pytest.raises(sqlite3.OperationalError):
        con.execute("DELETE FROM events")


def test_backfill_older_version_is_history_not_new(tmp_path, source):
    from regpilot.core.models import EVENT_BACKFILLED

    w = Watcher(tmp_path, sources=[source])
    w.run("R117")  # 2025 first
    states = w.run_all("R117")  # then the full history, oldest first
    assert [s["target"]["source_id"] for s in states] == ["42016X0812(01)", "42025X1453"]
    old_event = states[0]["events"][0]
    assert old_event["type"] == EVENT_BACKFILLED  # impact must not act on a superseded version
    assert old_event["payload"]["previous_celex"] is None  # nothing older than 2016 here
    assert states[1]["status"] == "unchanged"
    assert [e["type"] for e in events(tmp_path)] == [EVENT_NEW_VERSION, EVENT_BACKFILLED]


def test_backfill_in_order_diffs_against_predecessor(tmp_path, source):
    states = Watcher(tmp_path, sources=[source]).run_all("R117")
    assert [s["events"][0]["type"] for s in states] == [EVENT_NEW_VERSION, EVENT_NEW_VERSION]
    assert states[1]["events"][0]["payload"]["previous_celex"] == "42016X0812(01)"
