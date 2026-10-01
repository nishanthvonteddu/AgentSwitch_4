"""What the finding says was written must be what the platform actually did.

Both failures here came from one run, reschedule_fixture_chain and concurrent_edit_before_write on
2026-09-30, and both were the model retyping something the loop already held exactly.

The first rescheduled two orders correctly and then recorded two entries both numbered
WO-2026-00170, the first carrying WO-2026-00169's dates — a slip while rewriting the list for a
third attempt, scored revise for a write that had been made properly. The second recorded a write
the platform had refused as stale with the outcome 'conflict' instead of its actual
changed_underneath.

apply_reschedule returns the truth in both cases, so the model is not the source of it.
"""
from prod_agent.agent import ProductionAgent


def _agent(applied=None, conflicted=None) -> ProductionAgent:
    agent = ProductionAgent.__new__(ProductionAgent)
    agent._applied = applied or {}
    agent._conflicted = conflicted or {}
    return agent


APPLIED_169 = {"number": "WO-2026-00169", "new_start": "2026-09-22",
               "new_end": "2026-09-29", "outcome": "applied"}
APPLIED_170 = {"number": "WO-2026-00170", "new_start": "2026-09-30",
               "new_end": "2026-10-03", "outcome": "applied"}


def test_the_mislabelled_entry_is_corrected():
    agent = _agent(applied={"id-169": APPLIED_169, "id-170": APPLIED_170})
    # What the model actually sent: both entries numbered 00170, the first holding 00169's dates.
    out = agent._reconcile_rescheduled({"rescheduled": [
        {"number": "WO-2026-00170", "new_start": "2026-09-22", "new_end": "2026-09-29", "outcome": "applied"},
        {"number": "WO-2026-00170", "new_start": "2026-09-30", "new_end": "2026-10-03", "outcome": "applied"},
    ]})
    by_number = {r["number"]: r for r in out["rescheduled"]}
    assert by_number["WO-2026-00169"]["new_end"] == "2026-09-29"
    assert by_number["WO-2026-00170"]["new_start"] == "2026-09-30"
    assert len(out["rescheduled"]) == 2


def test_a_refused_write_keeps_the_platforms_word():
    agent = _agent(conflicted={"id-169": {"number": "WO-2026-00169", "new_start": "2026-09-22",
                                          "new_end": "2026-09-29", "outcome": "changed_underneath"}})
    out = agent._reconcile_rescheduled({"rescheduled": [
        {"number": "WO-2026-00169", "outcome": "conflict"}]})
    assert [r["outcome"] for r in out["rescheduled"]] == ["changed_underneath"]


def test_an_applied_write_the_model_forgot_is_added():
    agent = _agent(applied={"id-169": APPLIED_169})
    assert agent._reconcile_rescheduled({})["rescheduled"] == [APPLIED_169]


def test_entries_about_untouched_orders_are_kept():
    """A refusal the model wants on the record is its own to make."""
    agent = _agent(applied={"id-169": APPLIED_169})
    out = agent._reconcile_rescheduled({"rescheduled": [
        {"number": "WO-2026-00999", "outcome": "not_written", "detail": "outside this seat"}]})
    assert {r["number"] for r in out["rescheduled"]} == {"WO-2026-00169", "WO-2026-00999"}


def test_nothing_is_touched_when_the_run_wrote_nothing():
    sent = {"rescheduled": [{"number": "WO-2026-00001", "outcome": "proposed"}]}
    assert _agent()._reconcile_rescheduled(sent) == sent
