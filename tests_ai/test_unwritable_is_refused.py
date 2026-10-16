"""When a write was asked for and nothing could be written, the record must say refused.

Seen live 2026-09-30 on refuse_locked_wo48_reschedule. Told to reschedule WO-2026-00048 and "write
the change now", the run proposed, found the date uncommittable — engineering_change_pending,
subcontract_not_sent — wrote nothing, and filed outcome='answered' with refusal_reason=None. A
reader of that record would think the request had been carried out.

propose_reschedule already returns the platform's own why_not_writable, so the refusal and its
reason are both facts the loop holds before the model is asked for them.

These go through ProductionAgent._dispatch over an in-memory book (test_behaviour_fakes.Book): the proposal
comes from the real propose_reschedule, and the outcome is read from what reached AgentMemory.
"""
import pytest
from test_behaviour_fakes import Book, agent_on, finding, pin_today, work_order


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    pin_today(monkeypatch)


B = "id-WO-2026-00170"


def _locked():
    """WO-2026-00048: draft, with a subcontract never sent, so no date can be committed."""
    wo = work_order("WO-2026-00048", "draft", start="2026-08-01", end="2026-08-20")
    return Book(tables={"WorkOrder": [wo], "SubcontractOrder": [
        {"id": "sco-30", "number": "SCO-2026-00030", "work_order_id": wo["id"], "status": "draft",
         "expected_delivery_date": None, "vendor_id": "v-1"}]})


def _chain():
    """WO-169 (draft, unblocked) and WO-170 (draft, consumes WO-169's item): both writable."""
    return Book(tables={
        "WorkOrder": [work_order("WO-2026-00169", "draft", start="2026-09-01", end="2026-09-05"),
                      work_order("WO-2026-00170", "draft", start="2026-09-10", end="2026-09-13", bom_id="bom-170")],
        "BOM": [{"id": "bom-170", "number": "BOM-170", "materials": [{"item_id": "item-WO-2026-00169", "qty": 2}]}]})


def test_the_live_failure_is_caught():
    """Nothing written, filed as answered with no refusal_reason: sent back, and nothing stored."""
    book = _locked()
    agent = agent_on(book, apply_mode=True, approve=lambda p: True)
    proposed = agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00048"})
    assert [p["writable_by_seat"] for p in proposed["proposals"]] == [False]

    out = agent._dispatch("record_finding", finding("WO-2026-00048", blocking_causes=["subcontract_not_sent"]))

    assert out["error"] == "nothing could be written and the finding does not say so"
    assert out["why_not_writable"] == ["no committable date: subcontract_not_sent"]
    assert book.memories == [] and agent.finding is None


def test_the_retry_does_not_inherit_the_blamed_outcome():
    """The refusal blames outcome and refusal_reason, so the retry's own values are the ones stored."""
    book = _locked()
    agent = agent_on(book, apply_mode=True, approve=lambda p: True)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00048"})
    agent._dispatch("record_finding", finding("WO-2026-00048", evidence_records=["SCO-2026-00030"]))

    retry = finding("WO-2026-00048", outcome="refused", refusal_reason="no committable date: subcontract_not_sent")
    del retry["evidence_records"]
    agent._dispatch("record_finding", retry)

    [stored] = book.recorded_findings()
    assert stored["outcome"] == "refused" and stored["refusal_reason"].startswith("no committable date")
    assert "SCO-2026-00030" in stored["evidence_records"]


def test_a_correct_refusal_passes():
    book = _locked()
    agent = agent_on(book, apply_mode=True, approve=lambda p: True)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00048"})
    out = agent._dispatch("record_finding", finding("WO-2026-00048", outcome="refused",
                                                    refusal_reason="no committable date: subcontract_not_sent"))
    assert "agent_memory_id" in out
    assert book.recorded_findings()[0]["outcome"] == "refused"


def test_a_run_that_wrote_something_is_left_alone():
    """WO-170 was proposed on its own and written; proposing WO-48 then marks WO-170 a dependant it cannot
    date. Every proposal held is now unwritable, but the run did write, so answering is right."""
    book = _locked()
    book.tables["WorkOrder"].append(work_order("WO-2026-00170", "draft", start="2026-08-25", end="2026-08-28",
                                               bom_id="bom-170"))
    book.tables["BOM"] = [{"id": "bom-170", "number": "BOM-170", "materials": [{"item_id": "item-WO-2026-00048"}]}]
    agent = agent_on(book, apply_mode=True, approve=lambda p: True)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00170"})
    assert agent._dispatch("apply_reschedule", {"work_order_id": B})["outcome"] == "applied"
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00048"})
    assert not any(p["writable_by_seat"] for p in agent._proposals.values())

    out = agent._dispatch("record_finding", finding("WO-2026-00048"))

    assert "agent_memory_id" in out
    assert book.recorded_findings()[0]["rescheduled"][0]["outcome"] == "applied"


def test_a_writable_proposal_is_not_a_refusal():
    book = _chain()
    agent = agent_on(book, apply_mode=True, approve=lambda p: True, allowed_write_ids=set())
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00169"})
    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00169"))


def test_a_read_only_run_is_not_second_guessed():
    """Nothing was asked to be written, so answering is the right outcome."""
    book = _locked()
    agent = agent_on(book)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00048"})
    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00048"))


def test_it_warns_once_and_then_lets_the_finding_through():
    book = _locked()
    agent = agent_on(book, apply_mode=True, approve=lambda p: True)
    agent._dispatch("propose_reschedule", {"work_order": "WO-2026-00048"})
    assert "error" in agent._dispatch("record_finding", finding("WO-2026-00048"))
    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00048"))
    assert [f["outcome"] for f in book.recorded_findings()] == ["answered"]
