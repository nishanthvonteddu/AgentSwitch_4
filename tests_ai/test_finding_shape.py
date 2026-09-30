"""record_finding rejects a wrong-shaped field before it reaches the database.

Seen live 2026-09-29 on escalate_blocked_wo48: the model recorded escalations as a list of strings
where the schema declares objects. The finding persisted, the verifier called .get() on a str and
raised, and the task scored unevaluated — which never counts as a pass. A wrong shape caught here
is one the model can still fix; caught later it is a task lost with nothing checked.
"""
import pytest

from prod_agent.agent import _mistyped


def test_the_live_failure_is_caught():
    wrong = _mistyped({"escalations": ["raised an escalation to the plant head"]})
    assert wrong and "escalations" in wrong[0]
    assert "not a string" in wrong[0]


def test_a_correct_escalation_passes():
    assert _mistyped({"escalations": [{"raised": True, "number": "ESC-1"}]}) == []


@pytest.mark.parametrize("field", ["blocking_causes", "evidence_records",
                                   "potentially_blocked_work_orders", "blocked_sales_orders"])
def test_a_string_array_rejects_non_strings(field):
    assert _mistyped({field: [{"code": "x"}]})
    assert _mistyped({field: ["fine", "also fine"]}) == []


def test_an_array_field_given_a_bare_value_is_caught():
    wrong = _mistyped({"evidence_records": "WO-2026-00048"})
    assert wrong and "expected an array" in wrong[0]


def test_an_object_field_given_a_string_is_caught():
    wrong = _mistyped({"cost": "no cost recorded"})
    assert wrong and "expected an object" in wrong[0]


def test_rescheduled_entries_must_be_objects():
    assert _mistyped({"rescheduled": ["WO-1 moved"]})
    assert _mistyped({"rescheduled": [{"number": "WO-1", "outcome": "applied"}]}) == []


def test_null_is_left_to_the_null_check():
    """_nulled_required owns that case and gives a better message for it."""
    assert _mistyped({"escalations": None, "cost": None}) == []


def test_unknown_fields_are_ignored():
    assert _mistyped({"something_the_schema_does_not_declare": 42}) == []


def test_a_well_formed_finding_passes_whole():
    assert _mistyped({
        "outcome": "answered", "work_order": "WO-2026-00048", "is_late": True, "currency": "INR",
        "blocking_causes": ["subcontract_not_sent"], "contributing_causes": [],
        "evidence_records": ["SCO-2026-00030"], "potentially_blocked_work_orders": [],
        "blocked_sales_orders": ["SO-2026-00092"], "rescheduled": [],
        "escalations": [{"raised": True}], "not_visible": [], "refusal_reason": None,
        "cost": {"expected": 1.0, "actual": 2.0, "variance": 1.0}, "capacity": None,
    }) == []
