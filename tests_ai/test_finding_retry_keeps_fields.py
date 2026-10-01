"""A refused record_finding must not cost the model the fields it already got right.

Seen live 2026-09-30 on most_overdue_open_why_late. The first attempt carried
contributing_causes ['stopped_without_recorded_reason', 'operation_not_started'] and a cost of
zeroes. The cost guard refused it. The retry set cost to null and rebuilt the rest of the payload
from scratch, dropping contributing_causes, so the finding that persisted was missing a cause the
database proves and the task scored revise. The refusal was meant to improve the answer and made
it worse.

Every guard in record_finding ends with "call record_finding again", so this is a property of all
of them, not of the cost check.
"""
from prod_agent.agent import ProductionAgent


def _agent() -> ProductionAgent:
    agent = ProductionAgent.__new__(ProductionAgent)
    agent._rejected_finding_args = {}
    return agent


def test_the_live_failure_does_not_repeat():
    agent = _agent()
    first = {"work_order": "WO-2026-00047", "is_late": True,
             "blocking_causes": ["subcontract_not_sent"],
             "contributing_causes": ["stopped_without_recorded_reason", "operation_not_started"],
             "cost": {"expected": 0, "actual": 0}}
    agent._reject_finding(first, {"error": "cost must be null"}, blame=("cost",))

    retry = agent._restore_dropped({"work_order": "WO-2026-00047", "is_late": True,
                                    "blocking_causes": ["subcontract_not_sent"], "cost": None})
    assert retry["contributing_causes"] == ["stopped_without_recorded_reason", "operation_not_started"]


def test_the_field_that_caused_the_refusal_is_not_restored():
    """Carrying the blamed value back would refuse the retry forever."""
    agent = _agent()
    agent._reject_finding({"cost": {"expected": 0, "actual": 0}, "outcome": "answered"},
                          {"error": "cost must be null"}, blame=("cost",))
    assert agent._restore_dropped({"outcome": "answered"}).get("cost") is None


def test_a_retry_may_still_change_what_it_sent_before():
    agent = _agent()
    agent._reject_finding({"is_late": True, "outcome": "answered"}, {"error": "x"})
    assert agent._restore_dropped({"is_late": False, "outcome": "answered"})["is_late"] is False


def test_nothing_is_invented_before_a_refusal():
    assert _agent()._restore_dropped({"outcome": "answered"}) == {"outcome": "answered"}


def test_every_blamed_field_is_dropped_not_just_the_first():
    agent = _agent()
    agent._reject_finding({"a": 1, "b": 2, "keep": 3}, {"error": "x"}, blame=["a", "b"])
    restored = agent._restore_dropped({})
    assert restored == {"keep": 3}
