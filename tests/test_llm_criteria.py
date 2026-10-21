"""The LLM fallback: proposals only, code checks, human approval, deterministic re-extraction.
A fake proposer stands in for the LLM, so these tests are offline and repeatable."""

import json

from regpilot.core.models import EVENT_UPDATED, Range
from regpilot.watcher import db
from regpilot.watcher.graph import Watcher
from regpilot.watcher.llm_criteria import LLMCriteria, check, footnote_for, to_criteria

ODD = b"Mud and snow tyre"  # wording the rules don't know


def odd_source(source):
    """2025 fixture with one category label replaced by unknown wording."""
    raw = source.content["42025X1453"]
    assert b">Special use tyre<" in raw
    source.content["42025X1453"] = raw.replace(b">Special use tyre<", b">" + ODD + b"<", 1)
    return source


class FakeLLM:
    model = "fake-llm"

    def __init__(self, answer=None, fail=False):
        self.calls, self.fail = [], fail
        self.answer = answer or LLMCriteria(category_of_use="snow", fully_representable=True, evidence="snow tyre")

    def __call__(self, text, context):
        self.calls.append((text, context))
        if self.fail:
            raise TimeoutError("model not reachable")
        return self.answer


def reviews(tmp_path):
    with db.connect(tmp_path / "watcher.db") as con:
        return [dict(r) for r in con.execute("SELECT * FROM criteria_reviews ORDER BY id")]


def odd_rows(state):
    return [lr for r in state["requirements"] for lr in r["limits"] if ODD.decode() in lr["conditions"]]


def test_no_llm_step_when_rules_understand_everything(tmp_path, source):
    llm = FakeLLM()
    Watcher(tmp_path, sources=[source], proposer=llm).run("R117")
    assert llm.calls == [] and reviews(tmp_path) == []


def test_llm_proposes_but_requirements_stay_unknown(tmp_path, source):
    llm = FakeLLM()
    state = Watcher(tmp_path, sources=[odd_source(source)], proposer=llm).run("R117")

    assert [t for t, _ in llm.calls] == [ODD.decode()]  # asked once per distinct text
    ctx = llm.calls[0][1]
    assert ctx["regulation"] == "R117" and ctx["tyre_class"] in ("C1", "C2", "C3")
    [rev] = reviews(tmp_path)
    assert rev["status"] == "proposed" and json.loads(rev["checks"]) == []  # grounded
    # nothing used yet: the row is still unknown and the event says so
    assert all(lr["criteria"]["unparsed"] == [ODD.decode()] for lr in odd_rows(state))
    assert state["events"][0]["payload"]["unknown_conditions"] == [ODD.decode()]


def test_approval_reprocesses_and_emits_updated(tmp_path, source):
    w = Watcher(tmp_path, sources=[odd_source(source)], proposer=FakeLLM())
    state = w.run("R117")
    [rev] = reviews(tmp_path)
    with db.connect(tmp_path / "watcher.db") as con:
        db.decide_review(con, rev["id"], "approved", "tester", {"category_of_use": "snow"})

    out = w.reprocess(state["document_id"])
    assert out["event"].type == EVENT_UPDATED and out["event"].payload["reason"] == "criteria_reviewed"
    assert out["event"].payload["unknown_conditions"] == []
    with db.connect(tmp_path / "watcher.db") as con:
        rows = [lr for r in db.load_requirements(con, state["document_id"]) for lr in r.limits
                if ODD.decode() in lr.conditions]
    assert rows and all(lr.criteria.category_of_use == "snow" and not lr.criteria.unparsed
                        and lr.criteria_source == "human_reviewed" for lr in rows)
    assert w.reprocess(state["document_id"]) is None  # idempotent: nothing changes the second time


def test_rejected_or_pending_proposals_are_never_used(tmp_path, source):
    w = Watcher(tmp_path, sources=[odd_source(source)], proposer=FakeLLM())
    state = w.run("R117")
    assert w.reprocess(state["document_id"]) is None  # pending: no effect
    with db.connect(tmp_path / "watcher.db") as con:
        db.decide_review(con, reviews(tmp_path)[0]["id"], "rejected", "tester", None)
    assert w.reprocess(state["document_id"]) is None  # rejected: no effect


def test_failed_llm_call_is_not_stored_and_retried_later(tmp_path, source):
    w = Watcher(tmp_path, sources=[odd_source(source)], proposer=FakeLLM(fail=True))
    state = w.run("R117")
    assert reviews(tmp_path) == []
    assert any("will retry" in m for m in state["messages"])
    assert state["status"] == "complete"  # the watcher itself still finishes
    w.proposer = FakeLLM()
    w.propose_for_stored("R117")
    assert len(reviews(tmp_path)) == 1


def test_checks_catch_ungrounded_proposals():
    text = "Snow tyre with a speed category greater than 160 km/h"
    good = LLMCriteria(category_of_use="snow", speed_kmh=Range(min=160, min_inclusive=False),
                       fully_representable=True, evidence="speed category greater than 160 km/h")
    assert check(good, text, None) == []
    invented = good.model_copy(update={"speed_kmh": Range(min=190), "evidence": "for fast tyres"})
    problems = check(invented, text, None)
    assert any("190" in p for p in problems) and any("verbatim" in p for p in problems)
    assert check(LLMCriteria(fully_representable=False, not_representable="M+S", evidence="Snow"), "Snow (*1)", None)


def test_footnote_lookup_and_conversion():
    texts = ["12.7. some clause\n(*1) limit value applies also to tyres marked M+S only.\n(*2) other"]
    assert footnote_for("Snow (*1)", texts) == "(*1) limit value applies also to tyres marked M+S only."
    assert footnote_for("Snow", texts) is None
    c = to_criteria(LLMCriteria(category_of_use="snow", fully_representable=True, evidence="Snow"))
    assert c.constrained() == ["category_of_use"]
