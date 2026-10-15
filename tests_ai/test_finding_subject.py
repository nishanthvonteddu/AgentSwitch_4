"""The finding must be about the order the question asked about.

Seen live 2026-09-30 on downstream_potential_wo73. Asked what WO-2026-00073 blocks downstream, the
run looked up that order and nothing else, then filed the finding against WO-2026-00116 — one of
the downstream orders inside its own answer. The subject and the orders it blocks are separate
fields, and the blocked ones belong in potentially_blocked_work_orders.

The check is skipped while nothing has been looked up. A refusal often reads nothing at all and
names its order straight from the question, so there is no examined set to match it against; that
shape accounted for every false positive when the rule was measured over a full run.

These go through ProductionAgent._dispatch over an in-memory book (test_behaviour_fakes.Book): the lookup
is a real diagnose_work_order call, and the outcome is read from what reached AgentMemory.
"""
import pytest
from test_behaviour_fakes import Book, agent_on, finding, pin_today, work_order


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    pin_today(monkeypatch)


def _two_orders():
    return Book(tables={"WorkOrder": [work_order("WO-2026-00073", "in_progress", end="2026-09-01"),
                                      work_order("WO-2026-00116", "draft", end="2026-09-30")]})


def _looked_up_73(book):
    agent = agent_on(book)
    assert agent._dispatch("diagnose_work_order", {"work_order": "WO-2026-00073"})["found"] is True
    return agent


def test_the_live_failure_is_caught():
    book = _two_orders()
    agent = _looked_up_73(book)

    refused = agent._dispatch("record_finding", finding("WO-2026-00116"))

    assert refused["error"] == "the finding is about a work order this run never looked up"
    assert refused["work_order"] == "WO-2026-00116" and refused["examined"] == ["WO-2026-00073"]
    assert "potentially_blocked_work_orders" in refused["instruction"]
    assert book.memories == [] and agent.finding is None


def test_the_order_the_run_looked_up_passes():
    book = _two_orders()
    agent = _looked_up_73(book)
    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00073"))
    assert [f["work_order"] for f in book.recorded_findings()] == ["WO-2026-00073"]


def test_any_lookup_counts_not_only_diagnose():
    """downstream_impact on the order is a lookup of it too."""
    book = _two_orders()
    agent = agent_on(book)
    agent._dispatch("downstream_impact", {"work_order": "WO-2026-00116"})
    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00116"))


def test_a_refusal_that_read_nothing_is_not_second_guessed():
    """Every false positive over a full run was this shape."""
    book = _two_orders()
    agent = agent_on(book)
    out = agent._dispatch("record_finding", finding("WO-2026-00028", outcome="refused", is_late=None,
                                                    refusal_reason="cancel is outside this seat"))
    assert "agent_memory_id" in out and len(book.memories) == 1


def test_it_warns_once_and_then_lets_the_finding_through():
    """A second refusal could end the run with nothing stored, which scores worse than a wrong subject."""
    book = _two_orders()
    agent = _looked_up_73(book)
    assert "error" in agent._dispatch("record_finding", finding("WO-2026-00116"))
    assert "agent_memory_id" in agent._dispatch("record_finding", finding("WO-2026-00116"))
    assert [f["work_order"] for f in book.recorded_findings()] == ["WO-2026-00116"]


def test_the_rejected_subject_is_not_restored_onto_the_retry():
    book = _two_orders()
    agent = _looked_up_73(book)
    agent._dispatch("record_finding", finding("WO-2026-00116", is_late=True))

    retry = finding("WO-2026-00073")
    del retry["is_late"]
    agent._dispatch("record_finding", retry)

    [stored] = book.recorded_findings()
    assert stored["work_order"] == "WO-2026-00073", "the blamed field must come from the retry"
    assert stored["is_late"] is True, "a field the retry dropped is carried over from the first attempt"
