"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-12).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

Second official harness run, 2026-10-12, deepseek-v4p1-flash: three of eight tasks reached their 30k
token limit and filed a forced finding. why_late_wo48_subcontract passed only because the model, under
that forced call, still typed the draft subcontract numbers into evidence_records, which its verifier
requires. The record that proves each cause is already in the loop's diagnosis, so these pin that a
finding cites it whatever the model retyped.

The diagnosis is the real domain.diagnose over an in-memory book (test_behaviour_fakes.Book), so the signals
have exactly the shape and blocking flags production gives them: a material shortage is an undated blocker,
and a finite_schedule cause with no workstation carries no record.

Run: python -m pytest tests_ai/test_evidence_is_cited.py -q
"""
import pytest
from test_behaviour_fakes import Book, agent_on, finding, pin_today, schedule_entry, work_order

WO48 = work_order("WO-2026-00048", "in_progress", start="2026-08-01", end="2026-08-20")


def _sco(number):
    return {"id": f"id-{number}", "number": number, "work_order_id": WO48["id"], "status": "draft",
            "expected_delivery_date": None, "vendor_id": "v-1", "_vendor_id_display": "Anand Platers"}


@pytest.fixture
def agent(monkeypatch):
    pin_today(monkeypatch)
    book = Book(tables={"WorkOrder": [WO48], "SubcontractOrder": [_sco("SCO-2026-00011"), _sco("SCO-2026-00012")]},
                stock={WO48["id"]: [{"item_id": "item-7", "required": 5, "available": 2, "shortage": 3,
                                     "is_critical": True}]},
                schedule=[schedule_entry(WO48, "late", causes=[
                    {"code": "operator_shortage", "workstation_label": None, "minutes": 30}])])
    a = agent_on(book)
    diag = a._dispatch("diagnose_work_order", {"work_order": WO48["number"]})
    assert [(s["code"], s["blocking"], s["record"]) for s in diag["signals"]] == [
        ("subcontract_not_sent", True, "SCO-2026-00011"), ("subcontract_not_sent", True, "SCO-2026-00012"),
        ("material_shortage", True, "item-7"), ("schedule_operator_shortage", False, None)]
    return a


def _finding(**over):
    return {**finding(WO48["number"], blocking_causes=["subcontract_not_sent"]), **over}


def test_every_record_that_proves_a_named_cause_is_cited(agent):
    out = agent._cite_evidence(_finding())
    assert out["evidence_records"] == ["SCO-2026-00011", "SCO-2026-00012"]


def test_the_models_own_citations_stay_first_and_are_not_repeated(agent):
    out = agent._cite_evidence(_finding(evidence_records=["WO-2026-00048", "SCO-2026-00012"]))
    assert out["evidence_records"] == ["WO-2026-00048", "SCO-2026-00012", "SCO-2026-00011"]


def test_a_cause_the_finding_does_not_name_is_not_cited(agent):
    out = agent._cite_evidence(_finding(blocking_causes=["material_shortage"]))
    assert out["evidence_records"] == ["item-7"]


def test_a_signal_with_no_record_adds_nothing(agent):
    finding_ = _finding(blocking_causes=[], contributing_causes=["schedule_operator_shortage"])
    assert agent._cite_evidence(finding_) == finding_


@pytest.mark.parametrize("outcome", ["refused", None])
def test_a_refusal_or_an_unfinished_finding_is_left_alone(agent, outcome):
    finding_ = _finding(outcome=outcome)
    assert agent._cite_evidence(finding_) == finding_


def test_a_wrong_shape_is_left_for_the_shape_guard(agent):
    finding_ = _finding(evidence_records="SCO-2026-00011")
    assert agent._cite_evidence(finding_) == finding_


def test_an_order_never_diagnosed_is_left_alone(agent):
    finding_ = _finding(work_order="WO-2026-00049")
    assert agent._cite_evidence(finding_) == finding_


def test_the_whole_record_path_cites_the_subcontracts(agent):
    """Through record_finding itself, as the forced finding at the token limit would arrive."""
    agent._dispatch("record_finding", _finding())

    assert agent.finding["evidence_records"][:2] == ["SCO-2026-00011", "SCO-2026-00012"]
    [stored] = agent.mcp.recorded_findings()
    assert stored["evidence_records"][:2] == ["SCO-2026-00011", "SCO-2026-00012"]
    # The causes the model left out are restored too, each with the weight diagnose gave it, and cited.
    assert stored["blocking_causes"] == ["subcontract_not_sent", "material_shortage"]
    assert stored["contributing_causes"] == ["schedule_operator_shortage"]
    assert "item-7" in stored["evidence_records"]
