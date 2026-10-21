"""Agent 2 (Reg-Impact): timing, test selection, applicability, the graph, the agent, Gate 1."""

import csv
import shutil
from datetime import date
from pathlib import Path

import pytest

from regpilot.core.models import ApplicabilityDate, SourceQuote
from regpilot.core.orchestrator import Orchestrator
from regpilot.impact import gate1, store
from regpilot.impact.agent import ImpactAgent
from regpilot.impact.graph import ImpactAssessor
from regpilot.impact.judge import RuleBasedJudge
from regpilot.impact.rating import select_test, superseded_by, timing
from regpilot.watcher.agent import WatcherAgent
from regpilot.watcher.graph import Watcher
from regpilot.watcher.reader import WatcherReader

PRODUCTS = Path(__file__).parent.parent / "sample_data" / "products"
TODAY = "2026-10-22"


@pytest.fixture
def watched(tmp_path, source):
    Watcher(tmp_path, sources=[source]).run("R117")  # 2025 version
    return tmp_path


@pytest.fixture
def current(watched):
    return {r.id: r for r in WatcherReader(watched / "watcher.db").current_requirements("R117")}


def verdicts(state):
    return {(i["size_id"], i["requirement_id"]): i for i in state["proposal"]["impacts"]}


# --- pure functions ------------------------------------------------------------------------------
def test_timing_follows_the_transitional_dates(current):
    rr3 = current["R117:6.3:C1:stage-3"]  # new approvals 2024-07-07, all approvals 2026-07-07
    assert timing(rr3, date(2024, 1, 1))[0] == "upcoming"
    assert timing(rr3, date(2025, 1, 1))[0] == "new_approvals_only"
    assert timing(rr3, date(2026, 7, 7))[0] == "in_force"
    assert timing(current["R117:6.1:C1:stage-2"], date(2020, 1, 1))[0] == "in_force"  # no dates in this text


def test_superseded_stage(current):
    reqs = list(current.values())
    assert superseded_by(current["R117:6.2:C1:stage-1"], reqs, date(2026, 10, 22)) == "R117:6.2:C1:stage-2"
    assert superseded_by(current["R117:6.2:C1:stage-1"], reqs, date(2026, 1, 1)) is None
    assert superseded_by(current["R117:6.3:C2:stage-2"], reqs, date(2026, 10, 22)) is None  # C2: 2028


def test_test_record_selection():
    tyre = {"tests": [
        {"metric": "m", "value": 1, "test_date": "2024-01-01", "status": "valid", "valid_until": "2025-01-01"},
        {"metric": "m", "value": 2, "test_date": "2025-06-01", "status": "invalid"},
        {"metric": "m", "value": 3, "test_date": "2027-01-01", "status": "valid"},  # after as_of
    ]}
    assert select_test(tyre, "m", date(2024, 6, 1))[0]["value"] == 1
    assert select_test(tyre, "m", date(2026, 1, 1)) == (None, "expired_test")
    assert select_test({"tests": [tyre["tests"][1]]}, "m", date(2026, 1, 1)) == (None, "invalid_test")
    assert select_test(tyre, "other", date(2026, 1, 1)) == (None, "missing_test")


def test_rule_based_judge_never_assumes():
    j = RuleBasedJudge()
    assert j.assess("R117", "eurlex", "EU").status == "applies"
    assert j.assess("R117", "eurlex", "de").status == "applies"
    assert j.assess("R117", "eurlex", "IN").status == "unknown"
    assert j.assess("R117", "other", "EU").status == "unknown"


# --- the graph ------------------------------------------------------------------------------------
def test_assessment_as_of_today(watched):
    state = ImpactAssessor(watched, PRODUCTS).run("R117", as_of=TODAY)
    v = verdicts(state)
    fails = {k for k, i in v.items() if i["verdict"] == "fail"}
    assert fails == {("S07", "R117:6.3:C1:stage-3"), ("S10", "R117:6.3:C2:stage-3"), ("S11", "R117:6.3:C3:stage-3")}
    assert v[("S07", "R117:6.3:C1:stage-3")]["timing"] == "in_force"
    assert v[("S07", "R117:6.3:C1:stage-3")]["margin"] == pytest.approx(-0.3)
    assert v[("S10", "R117:6.3:C2:stage-3")]["timing"] == "new_approvals_only"
    assert v[("S02", "R117:6.3:C1:stage-3")]["verdict"] == "expired_test"
    assert v[("S05", "R117:6.1:C2:stage-2")]["verdict"] == "invalid_test"
    assert v[("S11", "R117:6.1:C3:stage-2")]["verdict"] == "incomparable"
    assert v[("S03", "R117:6.3:C1:stage-3")]["limit"] == 10.0  # low-profile high-speed row
    assert v[("S04", "R117:6.3:C1:stage-3")]["adjustments"]  # severe snow +1
    assert set(state["proposal"]["superseded"]) == {"R117:6.2:C1:stage-1", "R117:6.3:C1:stage-2"}
    assert ("S01", "R117:6.2:C1:stage-1") not in v
    assert state["event"]["type"] == "impact.proposed"


def test_assessment_before_the_c1_deadline(watched):
    v = verdicts(ImpactAssessor(watched, PRODUCTS).run("R117", as_of="2026-01-01"))
    s07 = v[("S07", "R117:6.3:C1:stage-3")]
    assert (s07["verdict"], s07["timing"], s07["all_from"]) == ("fail", "new_approvals_only", "2026-07-07")
    assert ("S01", "R117:6.2:C1:stage-1") in v  # not superseded yet


def test_same_inputs_same_proposal_no_duplicates(watched):
    a = ImpactAssessor(watched, PRODUCTS)
    first, second = a.run("R117", as_of=TODAY), a.run("R117", as_of=TODAY)
    assert first["proposal"]["id"] == second["proposal"]["id"]
    assert second["status"] == "exists" and second["event"] is None
    with store.connect(watched / "impact.db") as con:
        assert con.execute("SELECT COUNT(*) FROM proposals").fetchone()[0] == 1
    assert (watched / "gate1" / first["proposal"]["id"] / "report.md").exists()


def test_never_writes_watcher_db_or_products(watched, tmp_path):
    products = tmp_path / "products"
    shutil.copytree(PRODUCTS, products)
    before = {p.name: p.read_bytes() for p in products.iterdir()}
    wdb = (watched / "watcher.db").read_bytes()
    ImpactAssessor(watched, products).run("R117", as_of=TODAY)
    assert {p.name: p.read_bytes() for p in products.iterdir()} == before
    assert (watched / "watcher.db").read_bytes() == wdb


def test_range_without_applicable_market_is_not_assessed(watched, tmp_path):
    products = tmp_path / "products"
    shutil.copytree(PRODUCTS, products)
    rows = list(csv.DictReader((products / "range_markets.csv").open()))
    with (products / "range_markets.csv").open("w", newline="") as f:  # Turanza 6: India only
        w = csv.DictWriter(f, fieldnames=["range_id", "market"])
        w.writeheader()
        w.writerows([r for r in rows if r["range_id"] != "BS-TUR6"] + [{"range_id": "BS-TUR6", "market": "IN"}])
    state = ImpactAssessor(watched, products).run("R117", as_of=TODAY)
    assert not [i for i in state["proposal"]["impacts"] if i["range_name"] == "Turanza 6"]
    assert {"brand": "Bridgestone", "range_name": "Turanza 6", "markets": ["IN"]} in state["proposal"]["unassessed_ranges"]


# --- the agent: event-driven ---------------------------------------------------------------------------
def test_orchestrator_watcher_then_impact(tmp_path, source):
    ctx = {"regulations": ["R117"], "data_dir": tmp_path, "products": PRODUCTS}
    first = Orchestrator([WatcherAgent(sources=[source]), ImpactAgent()]).run({**ctx, "as_of": "2020-01-01"})
    assert [e.type for e in first["events"]] == ["regulation.new_version", "impact.proposed"]  # 2016 version

    later = Orchestrator([WatcherAgent(sources=[source]), ImpactAgent()]).run({**ctx, "as_of": TODAY})
    proposal = later["events"][-1].payload["proposal"]
    assert later["events"][-1].type == "impact.proposed" and proposal["celex"] == "42025X1453"
    s07 = next(i for i in proposal["impacts"] if (i["size_id"], i["requirement_id"]) == ("S07", "R117:6.3:C1:stage-3"))
    assert s07["requirement_change"] == "added" and s07["verdict"] == "fail"  # the new Stage 3 causes this fail

    again = Orchestrator([WatcherAgent(sources=[source]), ImpactAgent()]).run({**ctx, "as_of": TODAY})
    assert again["events"] == []  # nothing new: no new proposal


def test_cursor_mode_ignores_backfilled_history(tmp_path, source):
    w = Watcher(tmp_path, sources=[source])
    w.run("R117")
    w.run_all("R117")  # stores 2016 as history -> regulation.backfilled
    res = ImpactAgent().run({"data_dir": tmp_path, "products": PRODUCTS, "as_of": TODAY})
    assert [e.payload["celex"] for e in res.events] == ["42025X1453"]
    assert ImpactAgent().run({"data_dir": tmp_path, "products": PRODUCTS, "as_of": TODAY}).events == []


# --- Gate 1 ---------------------------------------------------------------------------------------------
@pytest.fixture
def proposal_id(watched):
    return ImpactAssessor(watched, PRODUCTS).run("R117", as_of=TODAY)["proposal"]["id"]


def test_gate1_edit_approve_and_audit(watched, proposal_id):
    edit = gate1.Edit(size_id="S10", requirement_id="R117:6.3:C2:stage-3", verdict="accepted_risk",
                      reason="all-approvals date is 2028-09-01; reformulation planned")
    p = gate1.edit(watched, proposal_id, "reviewer-a", [edit])
    assert p.summary["fail"] == 2 and p.summary["accepted_risk"] == 1
    i = next(x for x in p.impacts if x.size_id == "S10" and x.requirement_id.endswith("C2:stage-3"))
    assert (i.verdict, i.reviewer_verdict) == ("fail", "accepted_risk")  # the agent's verdict is kept

    gate1.approve(watched, proposal_id, "reviewer-b", "ok")
    with store.connect(watched / "impact.db") as con:
        log = [(r["action"], r["reviewer"]) for r in con.execute("SELECT * FROM gate1_log ORDER BY id")]
        status = con.execute("SELECT status FROM proposals WHERE id=?", (proposal_id,)).fetchone()[0]
    assert log == [("edit", "reviewer-a"), ("approve", "reviewer-b")] and status == "approved"
    assert (watched / "gate1" / proposal_id / "review.json").exists()

    with pytest.raises(gate1.Gate1Error, match="cannot be edited"):
        gate1.edit(watched, proposal_id, "reviewer-c", [edit])
    with pytest.raises(gate1.Gate1Error, match="already approved"):
        gate1.reject(watched, proposal_id, "reviewer-c", "changed my mind")


@pytest.mark.parametrize("edit, message", [
    (gate1.Edit(size_id="S99", requirement_id="R117:6.3:C1:stage-3", verdict="pass", reason="a long enough reason"),
     "no assessment"),
    (gate1.Edit(size_id="S07", requirement_id="R117:6.3:C1:stage-3", verdict="great", reason="a long enough reason"),
     "verdict must be"),
    (gate1.Edit(size_id="S07", requirement_id="R117:6.3:C1:stage-3", verdict="pass", reason="ok"), "reason"),
])
def test_gate1_edit_validation(watched, proposal_id, edit, message):
    with pytest.raises(gate1.Gate1Error, match=message):
        gate1.edit(watched, proposal_id, "reviewer", [edit])


def test_gate1_reject_needs_notes_and_name(watched, proposal_id):
    with pytest.raises(gate1.Gate1Error, match="notes"):
        gate1.reject(watched, proposal_id, "reviewer", "  ")
    with pytest.raises(gate1.Gate1Error, match="reviewer name"):
        gate1.approve(watched, proposal_id, "  ")


def test_catch_up_assesses_only_the_newest_version(tmp_path, source):
    w = Watcher(tmp_path, sources=[source])
    w.run("R117", as_of="2020-01-01")  # 2016 -> new_version
    w.run("R117")  # 2025 -> new_version   (impact was "down" for both)
    res = ImpactAgent().run({"data_dir": tmp_path, "products": PRODUCTS, "as_of": TODAY})
    assert [e.payload["celex"] for e in res.events] == ["42025X1453"]
    assert any("outdated" in m and "42016X0812(01)" in m for m in res.messages)
