"""When a write was asked for and nothing could be written, the record must say refused.

Seen live 2026-09-30 on refuse_locked_wo48_reschedule. Told to reschedule WO-2026-00048 and "write
the change now", the run proposed, found the date uncommittable — engineering_change_pending,
subcontract_not_sent — wrote nothing, and filed outcome='answered' with refusal_reason=None. A
reader of that record would think the request had been carried out.

propose_reschedule already returns the platform's own why_not_writable, so the refusal and its
reason are both facts the loop holds before the model is asked for them.
"""
from prod_agent.agent import ProductionAgent


def _agent(proposals, applied=None, apply_mode=True) -> ProductionAgent:
    agent = ProductionAgent.__new__(ProductionAgent)
    agent.apply_mode = apply_mode
    agent._proposals = proposals
    agent._applied = applied or {}
    agent._refusal_warned = False
    return agent


def _fires(agent, outcome) -> bool:
    """Mirror of the guard's condition in record_finding."""
    return bool(agent.apply_mode and agent._proposals and not agent._applied
                and not any(p.get("writable_by_seat") for p in agent._proposals.values())
                and outcome not in (None, "refused") and not agent._refusal_warned)


LOCKED = {"wo-48": {"number": "WO-2026-00048", "writable_by_seat": False,
                    "why_not_writable": "no committable date: engineering_change_pending"}}
WRITABLE = {"wo-48": {"number": "WO-2026-00048", "writable_by_seat": True}}


def test_the_live_failure_is_caught():
    assert _fires(_agent(LOCKED), "answered")


def test_a_correct_refusal_passes():
    assert not _fires(_agent(LOCKED), "refused")


def test_a_run_that_wrote_something_is_left_alone():
    agent = _agent(LOCKED, applied={"wo-49": {"number": "WO-2026-00049", "outcome": "applied"}})
    assert not _fires(agent, "answered")


def test_a_writable_proposal_is_not_a_refusal():
    assert not _fires(_agent(WRITABLE), "answered")


def test_a_read_only_run_is_not_second_guessed():
    """Nothing was asked to be written, so answering is the right outcome."""
    assert not _fires(_agent(LOCKED, apply_mode=False), "answered")


def test_it_warns_once_and_then_lets_the_finding_through():
    agent = _agent(LOCKED)
    assert _fires(agent, "answered")
    agent._refusal_warned = True
    assert not _fires(agent, "answered")
