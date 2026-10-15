"""record_finding's fixers and guards, and the write path, driven through the real agent and the real domain.

AI-written regression tests (ungraded); the team's hand-written tests live in tests/.

Every test here goes in through ProductionAgent._dispatch — the call the loop makes for each tool call — over an
in-memory book, and checks what reached AgentMemory (what a verifier grades) or the WorkOrder table (what a
planner sees). No guard condition is copied into a test: if a guard stops firing, or fires where it should not,
the persisted row or the write log says so.

Run: python -m pytest tests_ai/test_behaviour_agent_guards.py -q
"""
import pytest
from test_behaviour_fakes import Book, agent_on, finding, pin_today, work_order


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    pin_today(monkeypatch)  # 2026-09-16


def _chain():
    """WO-A (draft, unblocked) and WO-B (draft, consumes WO-A's item): both writable, both move."""
    return Book(tables={
        "WorkOrder": [work_order("WO-2026-00169", "draft", start="2026-09-01", end="2026-09-05"),
                      work_order("WO-2026-00170", "draft", start="2026-09-10", end="2026-09-13", bom_id="bom-170")],
        "BOM": [{"id": "bom-170", "number": "BOM-170", "materials": [{"item_id": "item-WO-2026-00169", "qty": 2}]}]})


A, B = "id-WO-2026-00169", "id-WO-2026-00170"


def _writer(book, **kwargs):
    return agent_on(book, apply_mode=True, approve=lambda p: True, allowed_write_ids={A, B}, **kwargs)


# --------------------------------------------------------------- one finding per run

def test_a_second_finding_in_the_same_run_is_refused_and_never_stored():
    book = Book(tables={"WorkOrder": [work_order("WO-2026-00028", "completed")]})
    agent = agent_on(book)
    first = finding("WO-2026-00028", outcome="refused", is_late=None, refusal_reason="cancel is not this seat's")

    stored = agent._dispatch("record_finding", first)
    again = agent._dispatch("record_finding", finding("WO-2026-00028", outcome="answered"))

    assert stored == {"agent_memory_id": "mem-1", "run_id": agent.run_id}
    assert again == {"error": "finding already recorded for this run"}
    assert [f["outcome"] for f in book.recorded_findings()] == ["refused"]
    assert agent.finding["outcome"] == "refused"


# --------------------------------------------------------------- the chain is not left half-moved

def test_applying_the_named_order_alone_is_sent_back_to_finish_the_chain():
    book = _chain()
    agent = _writer(book)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00169"})
    assert agent._dispatch("apply_reschedule", {"work_order_id": A})["outcome"] == "applied"

    out = agent._dispatch("record_finding", finding("WO-2026-00169"))

    assert out["error"] == "the chain is left inconsistent"
    assert out["unapplied"] == [{"work_order_id": B, "number": "WO-2026-00170",
                                 "new_start": "2026-09-21", "new_end": "2026-09-24"}]
    assert book.memories == []


def test_once_the_dependant_is_written_the_finding_is_stored_with_both_writes():
    book = _chain()
    agent = _writer(book)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00169"})
    agent._dispatch("apply_reschedule", {"work_order_id": A})
    agent._dispatch("record_finding", finding("WO-2026-00169"))
    agent._dispatch("apply_reschedule", {"work_order_id": B})

    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00169"))
    assert [(r["number"], r["outcome"]) for r in book.recorded_findings()[0]["rescheduled"]] == \
        [("WO-2026-00169", "applied"), ("WO-2026-00170", "applied")]


def test_the_chain_guard_warns_once_only():
    book = _chain()
    agent = _writer(book)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00169"})
    agent._dispatch("apply_reschedule", {"work_order_id": A})
    agent._dispatch("record_finding", finding("WO-2026-00169"))
    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00169"))


def test_a_dependant_the_caller_will_not_approve_is_not_demanded():
    book = _chain()
    agent = agent_on(book, apply_mode=True, approve=lambda p: True, allowed_write_ids={A})
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00169"})
    agent._dispatch("apply_reschedule", {"work_order_id": A})
    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00169"))


# --------------------------------------------------------------- a conflicting edit stops every later write

def test_after_a_conflicting_edit_no_further_write_is_attempted():
    book = _chain()
    agent = _writer(book)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00169"})
    book.tables["WorkOrder"][0]["updated_at"] = "2026-09-16T09:30:00Z"  # another team edits WO-169

    first = agent._dispatch("apply_reschedule", {"work_order_id": A})
    second = agent._dispatch("apply_reschedule", {"work_order_id": B})

    assert first["outcome"] == "changed_underneath"
    assert second["outcome"] == "refused" and "not retrying" in second["detail"]
    assert book.writes == [], "nothing may be written once the book is known to have moved underneath the run"
    assert agent.conflicts == ["WO-2026-00169"]


def test_the_conflict_reaches_the_finding_in_the_platforms_own_words():
    book = _chain()
    agent = _writer(book)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00169"})
    book.tables["WorkOrder"][0]["updated_at"] = "2026-09-16T09:30:00Z"
    agent._dispatch("apply_reschedule", {"work_order_id": A})

    agent._dispatch("record_finding", finding("WO-2026-00169", rescheduled=[{"number": "WO-2026-00169",
                                                                             "outcome": "conflict"}]))

    assert book.recorded_findings()[0]["rescheduled"][0]["outcome"] == "changed_underneath"


# --------------------------------------------------------------- the diagnosis is the source of the causes

def _wo49():
    """WO-2026-00049: stopped, a subcontract never sent, an open material request."""
    wo = work_order("WO-2026-00049", "stopped", start="2026-08-01", end="2026-08-30")
    return Book(tables={"WorkOrder": [wo],
                        "SubcontractOrder": [{"id": "sco-24", "number": "SCO-2026-00024", "work_order_id": wo["id"],
                                              "status": "draft", "expected_delivery_date": None, "vendor_id": "v-1"}],
                        "MaterialRequest": [{"id": "mr-5", "number": "MR-2026-00005", "work_order_id": wo["id"],
                                             "status": "submitted", "required_by_date": "2026-09-30"}]})


def test_a_cause_the_model_left_out_is_restored_into_the_stored_finding():
    """why_late_wo49_multi_cause: the model retyped every cause except stopped_without_recorded_reason."""
    book = _wo49()
    agent = agent_on(book)
    agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00049"})

    agent._dispatch("record_finding", finding("WO-2026-00049", blocking_causes=["subcontract_not_sent"],
                                              contributing_causes=["material_request_open"]))

    [stored] = book.recorded_findings()
    assert stored["blocking_causes"] == ["subcontract_not_sent"]
    assert stored["contributing_causes"] == ["material_request_open", "stopped_without_recorded_reason"]
    assert {"SCO-2026-00024", "MR-2026-00005", "WO-2026-00049"} <= set(stored["evidence_records"])


def test_causes_are_restored_even_when_the_run_never_diagnosed_the_order():
    """2026-10-12: the model reached its finding through propose_reschedule's dates alone and named no cause."""
    book = _wo49()
    agent = agent_on(book)

    agent._dispatch("record_finding", finding("WO-2026-00049"))

    [stored] = book.recorded_findings()
    assert stored["blocking_causes"] == ["subcontract_not_sent"]
    assert set(stored["contributing_causes"]) == {"stopped_without_recorded_reason", "material_request_open"}


def test_a_refusal_gets_no_causes_added():
    book = _wo49()
    agent = agent_on(book)
    agent._dispatch("record_finding", finding("WO-2026-00049", outcome="refused", is_late=None,
                                              refusal_reason="payroll is outside this seat"))
    [stored] = book.recorded_findings()
    assert stored["blocking_causes"] == [] and stored["contributing_causes"] == []
