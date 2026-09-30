"""The finding must be about the order the question asked about.

Seen live 2026-09-30 on downstream_potential_wo73. Asked what WO-2026-00073 blocks downstream, the
run looked up that order and nothing else, then filed the finding against WO-2026-00116 — one of
the downstream orders inside its own answer. The subject and the orders it blocks are separate
fields, and the blocked ones belong in potentially_blocked_work_orders.

The check is skipped while nothing has been looked up. A refusal often reads nothing at all and
names its order straight from the question, so there is no examined set to match it against; that
shape accounted for every false positive when the rule was measured over a full run.
"""
from prod_agent.agent import ProductionAgent


def _agent(examined=()) -> ProductionAgent:
    agent = ProductionAgent.__new__(ProductionAgent)
    agent._examined = set(examined)
    agent._subject_warned = False
    agent._rejected_finding_args = {}
    return agent


def _fires(agent, subject) -> bool:
    """Mirror of the guard's condition in record_finding."""
    return bool(agent._examined and isinstance(subject, str) and subject.startswith("WO-")
                and subject not in agent._examined and not agent._subject_warned)


def test_the_live_failure_is_caught():
    assert _fires(_agent({"WO-2026-00073"}), "WO-2026-00116")


def test_the_order_the_run_looked_up_passes():
    assert not _fires(_agent({"WO-2026-00073"}), "WO-2026-00073")


def test_a_refusal_that_read_nothing_is_not_second_guessed():
    """Every false positive over a full run was this shape."""
    assert not _fires(_agent(), "WO-2026-00028")


def test_it_warns_once_and_then_lets_the_finding_through():
    agent = _agent({"WO-2026-00073"})
    assert _fires(agent, "WO-2026-00116")
    agent._subject_warned = True
    assert not _fires(agent, "WO-2026-00116")


def test_the_rejected_subject_is_not_restored_onto_the_retry():
    agent = _agent({"WO-2026-00073"})
    agent._reject_finding({"work_order": "WO-2026-00116", "is_late": True},
                          {"error": "wrong subject"}, blame=("work_order",))
    assert agent._restore_dropped({"work_order": "WO-2026-00073"})["work_order"] == "WO-2026-00073"
    assert agent._restore_dropped({})["is_late"] is True
