"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-10-07).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero.

Seen live on WO-2026-00049 twice (2026-10-01 why_late_wo49_multi_cause, 2026-10-07
complex_partial_material_eta): diagnose returned stopped_without_recorded_reason, and the model
retyped every other cause into its finding but not that one. The loop holds diagnose's result, so
record_finding now adds back any cause the model left out.

Run: python -m pytest tests_ai/test_finding_restores_causes.py -q
"""
from prod_agent.agent import ProductionAgent

DIAG = {"found": True, "signals": [
    {"code": "stopped_without_recorded_reason", "blocking": False, "record": "WO-2026-00049"},
    {"code": "subcontract_not_sent", "blocking": True, "record": "SCO-2026-00024"},
    {"code": "material_request_open", "blocking": False, "record": "MR-2026-00065"},
    {"code": "material_request_open", "blocking": False, "record": "MR-2026-00054"},
]}


def _agent(diag=DIAG, ref="WO-2026-00049"):
    agent = ProductionAgent.__new__(ProductionAgent)
    agent._reads = {("diagnose", ref): diag} if diag is not None else {}
    agent._diagnoses = {}
    agent._numbers = {}  # _order_key reads it: an id is compared by the number a tool result paired it with
    return agent


def test_the_live_omission_is_restored():
    finding = {"work_order": "WO-2026-00049", "blocking_causes": ["subcontract_not_sent"],
               "contributing_causes": ["material_request_open"]}
    out = _agent()._restore_causes(finding)
    assert out["contributing_causes"] == ["material_request_open", "stopped_without_recorded_reason"]
    assert out["blocking_causes"] == ["subcontract_not_sent"]


def test_a_missing_blocking_cause_goes_to_blocking():
    out = _agent()._restore_causes({"work_order": "WO-2026-00049", "blocking_causes": [],
                                    "contributing_causes": ["stopped_without_recorded_reason", "material_request_open"]})
    assert out["blocking_causes"] == ["subcontract_not_sent"]


def test_the_models_own_classification_and_extras_are_kept():
    """A code the model put under blocking stays there, and a code diagnose did not return stays too."""
    finding = {"work_order": "WO-2026-00049",
               "blocking_causes": ["subcontract_not_sent", "material_request_open", "engineering_change_pending"],
               "contributing_causes": ["stopped_without_recorded_reason"]}
    assert _agent()._restore_causes(finding) == finding


def test_repeated_codes_are_added_once():
    out = _agent()._restore_causes({"work_order": "WO-2026-00049", "blocking_causes": ["subcontract_not_sent"],
                                    "contributing_causes": ["stopped_without_recorded_reason"]})
    assert out["contributing_causes"].count("material_request_open") == 1


def test_an_order_this_run_never_diagnosed_is_left_alone():
    finding = {"work_order": "WO-2026-00050", "blocking_causes": [], "contributing_causes": []}
    assert _agent()._restore_causes(finding) == finding
    assert _agent(diag={"found": False, "ref": "WO-X"}, ref="WO-X")._restore_causes(
        {"work_order": "WO-X", "blocking_causes": []}) == {"work_order": "WO-X", "blocking_causes": []}


def test_a_wrong_shape_is_left_for_the_shape_check():
    finding = {"work_order": "WO-2026-00049", "blocking_causes": "subcontract_not_sent"}
    assert _agent()._restore_causes(finding) == finding
