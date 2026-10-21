"""Watcher -> Impact hand-off: criteria, the simplified DB, the reader, limit selection."""

from pathlib import Path

import pytest

from regpilot.core.models import Criteria, Range, SourceQuote
from regpilot.impact.products import load_catalogue
from regpilot.impact.rating import select_limit
from regpilot.watcher import db
from regpilot.watcher.criteria import parse_applies_to, parse_conditions
from regpilot.watcher.graph import Watcher
from regpilot.watcher.reader import WatcherReader
from regpilot.watcher.requirements import extract

PRODUCTS = Path(__file__).parent.parent / "sample_data" / "products"


# --- criteria parsing (pure) ------------------------------------------------------------------
@pytest.mark.parametrize("conditions, expected", [
    (["Over 185 up to 245"], {"section_width_mm": Range(min=185, min_inclusive=False, max=245)}),
    (["185 and lower"], {"section_width_mm": Range(max=185)}),
    (["load capacity index < 87"], {"load_index": Range(max=87, max_inclusive=False)}),
    (["load capacity index ≥ 87", "Tyres other than Run Flat Tyres or Extended Mobility Tyres"],
     {"load_index": Range(min=87), "run_flat": False}),
    (["Tyres with a nominal aspect ratio ≤ 40 and suitable for speeds ≥ 300 km/h"],
     {"aspect_ratio": Range(max=40), "speed_kmh": Range(min=300)}),
    (["Snow tyre that is classified as tyre for use in severe snow conditions and with a speed category "
      "not greater than 160 km/h"], {"category_of_use": "snow", "severe_snow": True, "speed_kmh": Range(max=160)}),
    (["Special use tyre that is classified as tyre for use in severe snow conditions"],
     {"category_of_use": "special_use", "severe_snow": True}),
    (["Tyres other than tyres marked with “C”, “CP” or “LT”"], {"c_cp_lt_marking": False}),
])
def test_conditions_to_criteria(conditions, expected):
    c = parse_conditions(conditions)
    assert c.unparsed == []
    assert {f: getattr(c, f) for f in c.constrained()} == expected


def test_other_column_means_not_traction_only_in_traction_tables():
    assert parse_conditions(["Normal tyre", "Other"], traction_columns=True).traction is False
    assert parse_conditions(["Other"]).unparsed == ["Other"]  # elsewhere 'Other' is not understood


def test_unknown_wording_is_unparsed_not_guessed():
    c = parse_conditions(["Tyres with a moon-dust tread compound"])
    assert c.unparsed and c.matches({"category_of_use": "normal"}) is None


def test_adjustment_alternatives():
    alts = parse_applies_to("snow tyres that are classified as tyre for use in severe snow conditions, "
                            "extra load tyres or reinforced tyres, or any combination of these classifications")
    assert [a.constrained() for a in alts] == [["category_of_use", "severe_snow"], ["extra_load"], ["reinforced"]]


def test_matches_semantics():
    c = Criteria(category_of_use="snow", speed_kmh=Range(min=160, min_inclusive=False))
    assert c.matches({"category_of_use": "snow", "speed_kmh": 240}) is True
    assert c.matches({"category_of_use": "snow", "speed_kmh": 160}) is False  # 'greater than' excludes 160
    assert c.matches({"category_of_use": "snow"}) is None  # speed unknown -> unknown
    assert c.matches({"category_of_use": "normal"}) is False  # a known mismatch decides


def test_all_r117_conditions_are_machine_readable(doc2025, doc2016):
    for doc in (doc2025, doc2016):
        res = extract(doc, "R117")
        rows = [lr for r in res.requirements for lr in r.limits]
        assert rows and not [lr.conditions for lr in rows if lr.criteria.unparsed]
        assert res.warnings == []


def test_2016_sentence_with_two_adjustments(doc2016):
    r = {x.id: x for x in extract(doc2016, "R117").requirements}["R117:6.1:C1:stage-1"]
    assert [(a.delta, a.unit) for a in r.adjustments] == [(1.0, "dB(A)"), (2.0, "dB(A)")]
    assert r.adjustments[1].applies_if_any[0].category_of_use == "special_use"


# --- simplified DB + reader --------------------------------------------------------------------
@pytest.fixture
def watched(tmp_path, source):
    w = Watcher(tmp_path, sources=[source])
    w.run("R117", as_of="2020-01-01")
    w.run("R117")
    return tmp_path


def test_limits_table_and_current_views(watched):
    con = db.connect_readonly(watched / "watcher.db")
    tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert tables == {"documents", "sections", "requirements", "limits", "events", "runs", "criteria_reviews"}
    row = con.execute("SELECT * FROM current_limits WHERE requirement_id='R117:6.3:C1:stage-3' "
                      "AND conditions='load capacity index < 87'").fetchone()
    assert (row["comparator"], row["value"], row["raw_value"], row["celex"]) == ("<=", 10.0, "10,0", "42025X1453")
    assert "load_index" in row["criteria"]
    # the old version's rows are still there, just not "current"
    assert con.execute("SELECT COUNT(DISTINCT document_id) FROM limits").fetchone()[0] == 2
    assert {r[0] for r in con.execute("SELECT DISTINCT document_id FROM current_limits")} == {2}


def test_reader_events_and_requirements(watched):
    reader = WatcherReader(watched / "watcher.db")
    evs = reader.events()
    assert [e.payload["celex"] for e in evs] == ["42016X0812(01)", "42025X1453"]
    assert reader.events(since=evs[0].created_at.isoformat()) == evs[1:]
    reqs = reader.requirements(evs[1].payload["document_id"])
    assert len(reqs) == 15 and reqs[0].limits[0].criteria is not None
    assert [r.id for r in reader.current_requirements("R117")] == [r.id for r in reqs]


# --- limit selection for real product sizes (synthetic catalogue) -----------------------------
@pytest.fixture
def catalogue():
    return {t["size_id"]: t for t in load_catalogue(PRODUCTS)}


@pytest.fixture
def current(watched):
    return {r.id: r for r in WatcherReader(watched / "watcher.db").current_requirements("R117")}


def test_low_profile_high_speed_gets_its_specific_row(current, catalogue):
    sel = select_limit(current["R117:6.3:C1:stage-3"], catalogue["S03"])  # 245/35 ZR20 95Y XL
    assert sel.status == "selected" and sel.effective_value == 10.0
    assert "aspect ratio" in " ".join(sel.row.conditions)


def test_severe_snow_xl_adjustments(current, catalogue):
    cc2 = catalogue["S04"]  # Michelin CrossClimate 2 205/55 R16 94V XL (severe snow)
    noise = select_limit(current["R117:6.1:C1:stage-2"], cc2)
    assert (noise.effective_value, len(noise.adjustments)) == (72.0, 1)  # 71 + 1 (counted once)
    rr = select_limit(current["R117:6.3:C1:stage-3"], cc2)
    assert rr.effective_value == 10.0  # 9.0 + 1 N/kN for severe snow
    wet = select_limit(current["R117:6.2:C1:stage-2"], cc2)
    assert wet.effective_value == 1.1 and "Speed category greater than 160" in " / ".join(wet.row.conditions)


def test_run_flat_c2_and_c3_rows(current, catalogue):
    assert select_limit(current["R117:6.3:C1:stage-3"], catalogue["S08"]).effective_value == 10.0  # run flat
    assert select_limit(current["R117:6.3:C2:stage-3"], catalogue["S10"]).effective_value == 8.5
    assert select_limit(current["R117:6.3:C3:stage-3"], catalogue["S11"]).effective_value == 6.0


def test_missing_attribute_gives_unknown_never_a_guess(current, catalogue):
    tyre = dict(catalogue["S04"], speed_kmh=None)  # severe snow tyre, speed unknown
    sel = select_limit(current["R117:6.2:C1:stage-2"], tyre)
    assert sel.status == "unknown" and "speed_kmh" in sel.reason
    other = dict(catalogue["S01"], extra_load=None)
    assert select_limit(current["R117:6.1:C1:stage-2"], other).status == "unknown"  # can't tell if +1 applies


def test_wrong_class_is_no_match(current, catalogue):
    assert select_limit(current["R117:6.3:C1:stage-3"], catalogue["S11"]).status == "no_match"


@pytest.mark.parametrize("label, category", [
    ("Normal", "normal"), ("Snow", "snow"), ("Special", "special_use"), ("Normal (road type) tyre", "normal")])
def test_older_short_category_labels(label, category):
    assert parse_conditions([label]).category_of_use == category


def test_footnote_marker_stays_unknown():
    # 2011: "Snow (*1)" with footnote "limit value applies also to tyres marked M+S only"
    assert parse_conditions(["Snow (*1)"]).unparsed == ["Snow (*1)"]


def test_traction_sentence_becomes_two_adjustments():
    from regpilot.watcher.requirements import read_adjustments

    line = ("In case of traction tyres, the above limits shall be increased by 1 dB(A) for category of use "
            "normal and special, and by 2 dB(A) for category of use snow.")
    warnings = []
    adj = read_adjustments([line], lambda t: SourceQuote(text=t, section_key="main:6.1.2"), warnings, "test")
    assert [(a.delta, [(c.category_of_use, c.traction) for c in a.applies_if_any]) for a in adj] == [
        (1.0, [("normal", True), ("special_use", True)]), (2.0, [("snow", True)])]
    assert warnings == []


def test_unreadable_adjustment_sentence_is_kept_as_unknown():
    from regpilot.watcher.requirements import read_adjustments

    warnings = []
    adj = read_adjustments(["The limits shall be increased by a reasonable margin for exotic tyres."],
                           lambda t: SourceQuote(text=t, section_key="x"), warnings, "test")
    assert adj[0].delta is None and adj[0].applies_if_any[0].unparsed and warnings
