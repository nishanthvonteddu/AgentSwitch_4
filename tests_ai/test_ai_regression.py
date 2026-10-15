"""AI-WRITTEN REGRESSION TESTS (written by Claude, 2026-09-16).

These are NOT the team's hand-written tests and must not be claimed as such: the course scores
AI-written tests at zero. They exist to catch regressions in the agent and changes in the platform.
The graded, hand-written tests live in tests/.

Run: python -m pytest tests_ai -v   (set AGENT_TODAY=2026-09-16 for repeatable live results)
"""
import datetime

import pytest

from harness.runner import load_tasks
from harness.verify import Verdict, normalise
from prod_agent import config, domain
from prod_agent.agent import RECORD_FINDING_SCHEMA, ProductionAgent
from prod_agent.mcp_client import McpClient, McpError, RestClient, Session


@pytest.fixture(scope="module")
def suryodaya():
    return McpClient(Session("suryodaya"))


@pytest.fixture(scope="module")
def keystone():
    return McpClient(Session("keystone"))


def _codes(diagnosis):
    return {s["code"] for s in diagnosis["signals"]}


def _tool_names(agent):
    return {t["function"]["name"] for t in agent.tool_specs()}


# ----------------------------------------------------------------------------- offline

def test_error_kind_keeps_server_code():
    assert McpError(-32001, "x", {"code": "row_scope_denied"}).kind == "row_scope_denied"


def test_error_kind_inferred_from_role_message():
    err = McpError(-32602, "Role(s) 'user' cannot perform transition 'Cancel' (requires 'admin')")
    assert err.kind == "permission_denied"


def test_date_parser_rejects_garbage():
    assert domain._date("not-a-date") is None


def test_iso_of_missing_date_is_none():
    assert domain._iso(None) is None
    assert domain._iso(datetime.date(2026, 9, 16)) == "2026-09-16"


def test_entity_words_for_suggestions():
    assert domain._words("MachineDowntimeLog") == {"machine", "downtime", "log"}


def test_undated_blockers_membership(monkeypatch):
    """Membership, and what it decides: diagnose marks a signal blocking exactly when it is an undated blocker."""
    from test_behaviour_fakes import Book, pin_today, schedule_entry, work_order

    assert "subcontract_not_sent" in domain.UNDATED_BLOCKERS
    assert "workstation_downtime" not in domain.UNDATED_BLOCKERS
    pin_today(monkeypatch)
    wo = work_order("WO-2026-00048", "in_progress", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo], "SubcontractOrder": [
                    {"id": "sco-30", "number": "SCO-2026-00030", "work_order_id": wo["id"], "status": "draft"}]},
                schedule=[{**schedule_entry(wo), "operations": [{"workstation_id": "ws-1", "workstation_label": "WS-1"}]}],
                workstation_load=[{"workstation_id": "ws-1", "record_label": "WS-1", "state": "available",
                                   "downtime_minutes": 95.0}])
    weight = {s["code"]: s["blocking"] for s in domain.diagnose(book, "WO-2026-00048")["signals"]}
    assert weight == {"subcontract_not_sent": True, "workstation_downtime": False}


def test_today_can_be_pinned(monkeypatch):
    monkeypatch.setenv("AGENT_TODAY", "2026-01-31")
    monkeypatch.setattr(config, "load_dotenv", lambda *a, **k: None)
    assert config.today() == datetime.date(2026, 1, 31)


def test_unknown_instance_rejected_before_login():
    with pytest.raises(ValueError):
        Session("nowhere")


def test_write_tool_only_offered_in_apply_mode():
    assert "apply_reschedule" not in _tool_names(ProductionAgent(None, llm=object()))
    assert "apply_reschedule" in _tool_names(ProductionAgent(None, llm=object(), apply_mode=True))


def test_apply_without_proposal_is_refused():
    agent = ProductionAgent(None, llm=object(), apply_mode=True)
    assert agent._dispatch("apply_reschedule", {"work_order_id": "nope"})["outcome"] == "refused"


def test_dry_run_agent_cannot_dispatch_write():
    agent = ProductionAgent(None, llm=object())
    assert "error" in agent._dispatch("apply_reschedule", {"work_order_id": "nope"})


def test_finding_schema_supports_refusal():
    assert RECORD_FINDING_SCHEMA["properties"]["outcome"]["enum"] == ["answered", "partial", "refused"]
    assert "refusal_reason" in RECORD_FINDING_SCHEMA["required"]


def test_non_verdict_return_is_unevaluated():
    assert normalise("approve")[0] is Verdict.UNEVALUATED
    assert normalise((Verdict.APPROVE, "ok")) == (Verdict.APPROVE, "ok")


def test_sample_tasks_excluded_from_scoring():
    scored = {t["id"] for t in load_tasks(False)}
    all_tasks = {t["id"] for t in load_tasks(True)}
    assert "sample_finding_recorded" not in scored
    assert "sample_finding_recorded" in all_tasks


def test_task_set_has_refusals_and_resolvable_verifiers():
    import importlib
    tasks = load_tasks(False)
    for task in tasks:
        module, _, func = task["verifier"].partition(":")
        assert callable(getattr(importlib.import_module(module), func)), task["id"]
    assert any(t["id"].startswith("refuse_") for t in tasks)


# ----------------------------------------------------------------------------- Suryodaya (live)

def test_wo48_is_late(suryodaya):
    d = domain.diagnose(suryodaya, "WO-2026-00048")
    assert d["is_late"] is True
    assert d["days_past_due"] > 0


def test_wo48_cites_unsent_subcontracts(suryodaya):
    d = domain.diagnose(suryodaya, "WO-2026-00048")
    cited = {s["record"] for s in d["signals"] if s["code"] == "subcontract_not_sent"}
    assert {"SCO-2026-00030", "SCO-2026-00076"} <= cited


def test_wo48_customer_impact_known(suryodaya):
    assert domain.downstream_impact(suryodaya, "WO-2026-00048")["customer_impact"] == "known"


def test_wo49_reports_all_causes(suryodaya):
    codes = _codes(domain.diagnose(suryodaya, "WO-2026-00049"))
    assert {"stopped_without_recorded_reason", "subcontract_not_sent", "material_request_open"} <= codes


def test_sales_orders_read_only(suryodaya):
    assert suryodaya.has_tool("SalesOrder.get")
    assert not suryodaya.has_tool("SalesOrder.update")


def test_bug_b3_fixed_admin_cancel_tool_not_listed(suryodaya):
    # Bug B3 fixed 2026-09-17: admin-only transitions are no longer listed for manufacturing_user.
    assert not suryodaya.has_tool("WorkOrder.cancel.not_started.cancelled")


def test_payroll_entity_not_in_seat(suryodaya):
    cap = domain.seat_capability(suryodaya, "SalarySlip.list")
    assert cap["in_catalogue"] is False
    assert cap.get("outside_seat") is True and "warning" not in cap


def test_wrong_operation_name_is_not_a_visibility_limit(suryodaya):
    cap = domain.seat_capability(suryodaya, "JobCard.read")
    assert cap["entity_in_catalogue"] is True
    assert "JobCard.list" in cap["available_tools"]


def test_late_list_most_overdue_first(suryodaya):
    late = domain.list_late_work_orders(suryodaya)
    ends = [w["planned_end_date"] for w in late]
    assert ends == sorted(ends)
    assert late[0]["number"] == "WO-2026-00047"


def test_resolve_by_number_and_id_agree(suryodaya):
    by_number = domain.resolve_work_order(suryodaya, "WO-2026-00048")
    assert domain.resolve_work_order(suryodaya, by_number["id"])["number"] == "WO-2026-00048"


def test_completed_order_not_late(suryodaya):
    assert domain.diagnose(suryodaya, "WO-2026-00028")["is_late"] is False


def test_bug_b1_fixed_rest_job_cards_readable_suryodaya(suryodaya):
    # The parameter declares the REST client's live dependency to the offline gate.
    # Bug B1 fixed 2026-09-17: the REST door now agrees with MCP.
    assert RestClient(Session("suryodaya")).raw("/api/JobCard", limit=1)["data"]


def test_purchase_orders_still_outside_seat(suryodaya):
    assert not suryodaya.has_tool("PurchaseOrder.list")
    with pytest.raises(McpError) as exc:
        RestClient(Session("suryodaya")).raw("/api/PurchaseOrder", limit=1)
    assert exc.value.code == 403


# ----------------------------------------------------------------------------- Keystone (live)

def test_wo3_not_started_past_start(keystone):
    d = domain.diagnose(keystone, "WO-2026-00003")
    assert d["is_late"] is True
    assert "not_started_past_planned_start" in _codes(d)


def test_keystone_country(keystone):
    assert domain.company_context(keystone)["country"] == "United States"


def test_keystone_sales_orders_readable_not_writable(keystone):
    # Keystone gained sales_viewer on 2026-09-17.
    assert domain.seat_capability(keystone, "SalesOrder.get")["in_catalogue"] is True
    assert not keystone.has_tool("SalesOrder.update")


def test_keystone_wo77_has_no_recorded_cost(keystone):
    # Keystone was reseeded on 2026-09-17; WO-2026-00077 carries no cost.
    wo = domain.diagnose(keystone, "WO-2026-00077")["work_order"]
    assert not wo["expected_cost"]
    assert not wo["actual_cost"]


def test_bug_b1_fixed_rest_job_cards_readable_keystone(keystone):
    # The parameter declares the REST client's live dependency to the offline gate.
    assert RestClient(Session("keystone")).raw("/api/JobCard", limit=1)["data"]


def test_bug_b1_fixed_job_card_tool_usable_keystone(keystone):
    assert keystone.call("JobCard.list", {"limit": 1})["data"]


def test_diagnose_reports_current_operation(keystone):
    d = domain.diagnose(keystone, "WO-2026-00010")
    assert d["current_operation"]["job_card"].startswith("JC-")
    assert "operation_not_started" in _codes(d)


def test_downtime_summary_ranks_breakdowns(keystone):
    s = domain.downtime_summary(keystone, 90, "breakdown")
    minutes = [w["minutes"] for w in s["workstations"]]
    assert s["available"] and minutes == sorted(minutes, reverse=True) and minutes[0] > 0
