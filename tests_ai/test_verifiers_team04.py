"""AI-WRITTEN TESTS (written by Claude, 2026-10-15) for the team04 task verifiers. Ungraded; the graded
tests live in tests/.

One approve / revise pair for every task verifier, plus unevaluated where the task has a premise, and a
regression test for each verifier bug fixed on 2026-10-15 (the test names say which). Everything runs offline
against a fake REST client behind the real VerifyContext, so finding_from_db's own read-back is exercised too.

Rules under test are written at the top of harness/verifiers/team04.py: subject (rule 1), answering outcome
(rule 2), refusal outcome and when partial counts (rule 3), premise (rule 4).
"""
import datetime as dt
import itertools
import json

import pytest

from harness.verifiers import team04 as v
from harness.verify import Verdict, VerifyContext
from prod_agent import config

A, R, U = Verdict.APPROVE, Verdict.REVISE, Verdict.UNEVALUATED
TODAY = dt.date(2026, 10, 1)
ME, START = "agent-user", "2026-10-01T08:00:00"
WO48 = "WO-2026-00048"


@pytest.fixture(autouse=True)
def pinned_today(monkeypatch):
    monkeypatch.setattr(config, "today", lambda: TODAY)


# --------------------------------------------------------------------------- fake database and context

class FakeRest:
    """Rows by entity, filtered by equality like the REST list; raw paths answered from a table."""

    def __init__(self, rows, finding, raw):
        self.rows = {entity: [dict(r) for r in found] for entity, found in rows.items()}
        self.findings = [] if finding is None else [finding]
        self.paths = raw

    def raw(self, path, **params):
        if path == "/api/AgentMemory":
            return {"data": [{"id": "mem-1", "created_by": ME,
                              "content": config.FINDING_PREFIX + json.dumps({"run_id": "run-1", **f})}
                             for f in self.findings]}
        answer = self.paths[path]
        if isinstance(answer, Exception):
            raise answer
        return answer

    def _get(self, path, params=None):  # the private form the pre-2026-10-15 verifier used
        return self.raw(path, **(params or {}))

    def list(self, entity, **params):
        search = params.pop("search", None)
        return [dict(r) for r in self.rows.get(entity, [])
                if (not search or search in (r.get("number") or "")) and all(r.get(k) == val for k, val in params.items())]

    def get(self, entity, record_id):
        for r in self.rows.get(entity, []):
            if r.get("id") == record_id:
                return dict(r)
        raise KeyError(record_id)


_dirs = itertools.count()


def make_ctx(tmp_path, *, rows=None, finding=None, task=None, fixture=None, snapshot=None, tools=None, raw=None,
             seat=(), denied=(), interference=None):
    run_dir = tmp_path / f"run{next(_dirs)}"
    run_dir.mkdir()
    (run_dir / "result.json").write_text('{"run_id": "run-1"}', encoding="utf-8")
    if tools is not None:
        (run_dir / "trace.jsonl").write_text("".join(json.dumps(t) + "\n" for t in tools), encoding="utf-8")
    if interference is not None:
        (run_dir / "interference.json").write_text(json.dumps(interference), encoding="utf-8")
    ctx = VerifyContext(rest=FakeRest(rows or {}, finding, raw or {}), instance="test", task=task or {},
                        run_dir=run_dir, fixture=fixture,
                        context={"me": ME, "started_at_utc": START, "snapshot": snapshot or {}})
    ctx.seat_tool_names = lambda: set(seat)
    ctx.is_denied = lambda path: path in set(denied)
    return ctx


def tool(name, result, arguments="{}"):
    return {"type": "tool", "name": name, "arguments": arguments, "result": result}


def wo(number, status="stopped", **extra):
    return {"id": f"id-{number}", "number": number, "status": status, "planned_start_date": "2026-08-01",
            "planned_end_date": "2026-09-01", "updated_by": "someone", "updated_at": "2026-09-01T00:00:00", **extra}


def answer(work_order, **extra):
    return {"outcome": "answered", "work_order": work_order, "is_late": True, "blocking_causes": [],
            "contributing_causes": [], "evidence_records": [], "potentially_blocked_work_orders": [],
            "blocked_sales_orders": [], "rescheduled": [], "not_visible": [], "refusal_reason": None, **extra}


def refusal(work_order, **extra):
    return {**answer(work_order, outcome="refused", is_late=None, refusal_reason="outside this seat"), **extra}


def verdict(fn, ctx):
    return fn(ctx)[0]


# --------------------------------------------------------------------------- why_late_subcontract_not_sent

def wo48_rows(status="stopped", sco_status="draft", **extra):
    return {"WorkOrder": [wo(WO48, status, **extra)],
            "SubcontractOrder": [{"id": "s1", "number": "SCO-1", "status": sco_status, "work_order_id": f"id-{WO48}"}]}


def subcontract_finding(**extra):
    return {**answer(WO48, blocking_causes=["subcontract_not_sent"], evidence_records=[WO48, "SCO-1"]), **extra}


def test_why_late_subcontract_approves_named_and_cited_cause(tmp_path):
    assert verdict(v.why_late_subcontract_not_sent, make_ctx(tmp_path, rows=wo48_rows(), finding=subcontract_finding())) is A


@pytest.mark.parametrize("finding", [
    answer(WO48, evidence_records=["SCO-1"]),                                    # cause not named
    answer(WO48, blocking_causes=["subcontract_not_sent"]),                      # draft subcontract not cited
    subcontract_finding(outcome="refused"),                                      # refused an answerable question
])
def test_why_late_subcontract_revises_missing_cause_citation_or_refusal(tmp_path, finding):
    assert verdict(v.why_late_subcontract_not_sent, make_ctx(tmp_path, rows=wo48_rows(), finding=finding)) is R


def test_regression_why_late_subcontract_without_draft_subcontract_is_unevaluated_not_approved(tmp_path):
    """Old code approved an empty finding once WO-48 had no draft subcontract: nothing was left to grade."""
    ctx = make_ctx(tmp_path, rows=wo48_rows(sco_status="sent"), finding=answer(WO48))
    assert verdict(v.why_late_subcontract_not_sent, ctx) is U


def test_why_late_subcontract_closed_order_is_unevaluated(tmp_path):
    ctx = make_ctx(tmp_path, rows=wo48_rows(status="completed"), finding=subcontract_finding())
    assert verdict(v.why_late_subcontract_not_sent, ctx) is U


# --------------------------------------------------------------------------- blocks_linked_sales_order

def so_rows(status="stopped", linked=True):
    target = wo(WO48, status, item_id="item-48", sales_order_id="so-1" if linked else None)
    return {"WorkOrder": [target], "SalesOrder": [{"id": "so-1", "number": "SO-1"}], "BOM": []}


def test_blocks_linked_sales_order_approves_the_linked_order(tmp_path):
    ctx = make_ctx(tmp_path, rows=so_rows(), finding=answer(WO48, blocked_sales_orders=["SO-1"]))
    assert verdict(v.blocks_linked_sales_order, ctx) is A


@pytest.mark.parametrize("finding", [
    answer("WO-2026-00049", blocked_sales_orders=["SO-1"]),                       # another order (regression A)
    answer(WO48, outcome="refused", blocked_sales_orders=["SO-1"]),               # refusal (regression A)
    answer(WO48, blocked_sales_orders=[]),                                        # linked order missing
])
def test_regression_blocks_linked_sales_order_revises_wrong_subject_refusal_or_missing_order(tmp_path, finding):
    assert verdict(v.blocks_linked_sales_order, make_ctx(tmp_path, rows=so_rows(), finding=finding)) is R


@pytest.mark.parametrize(("status", "linked"), [("completed", False), ("stopped", False), ("completed", True)])
def test_regression_blocks_linked_sales_order_without_premise_is_unevaluated(tmp_path, status, linked):
    """Old code approved an empty finding when WO-48 was closed or had no sales order."""
    ctx = make_ctx(tmp_path, rows=so_rows(status, linked), finding=answer(WO48))
    assert verdict(v.blocks_linked_sales_order, ctx) is U


# --------------------------------------------------------------------------- keystone_not_started_cause

WO3 = "WO-2026-00003"


def wo3_rows(status="not_started"):
    return {"WorkOrder": [wo(WO3, status, planned_start_date="2026-07-23", planned_end_date="2026-08-31")]}


def test_keystone_not_started_cause_approves(tmp_path):
    ctx = make_ctx(tmp_path, rows=wo3_rows(), finding=answer(WO3, contributing_causes=["not_started_past_planned_start"]))
    assert verdict(v.keystone_not_started_cause, ctx) is A


@pytest.mark.parametrize("finding", [
    answer("WO-2026-00004", contributing_causes=["not_started_past_planned_start"]),          # another order
    answer(WO3, outcome="refused", contributing_causes=["not_started_past_planned_start"]),    # refusal
    answer(WO3, contributing_causes=["not_started_past_planned_start", "stopped_without_recorded_reason"]),  # invented
    answer(WO3, contributing_causes=[]),                                                        # cause omitted
])
def test_regression_keystone_not_started_cause_revises_subject_refusal_and_invented_cause(tmp_path, finding):
    assert verdict(v.keystone_not_started_cause, make_ctx(tmp_path, rows=wo3_rows(), finding=finding)) is R


def test_keystone_not_started_cause_started_order_is_unevaluated(tmp_path):
    ctx = make_ctx(tmp_path, rows=wo3_rows("in_progress"), finding=answer(WO3))
    assert verdict(v.keystone_not_started_cause, ctx) is U


# --------------------------------------------------------------------------- causes_match_db

WO49 = "WO-2026-00049"
WO49_TASK = {"params": {"work_order": WO49}}


def wo49_rows(status="stopped"):
    return {"WorkOrder": [wo(WO49, status)],
            "SubcontractOrder": [{"id": "s", "number": "SCO-24", "status": "draft", "work_order_id": f"id-{WO49}"}],
            "MaterialRequest": [{"id": "m", "number": "MR-65", "status": "submitted", "work_order_id": f"id-{WO49}"}]}


def wo49_finding(**extra):
    return {**answer(WO49, blocking_causes=["subcontract_not_sent"],
                     contributing_causes=["stopped_without_recorded_reason", "material_request_open"]), **extra}


def test_causes_match_db_approves_every_proven_cause(tmp_path):
    assert verdict(v.causes_match_db, make_ctx(tmp_path, rows=wo49_rows(), finding=wo49_finding(), task=WO49_TASK)) is A


@pytest.mark.parametrize("finding", [
    answer(WO49, blocking_causes=["subcontract_not_sent"], contributing_causes=["material_request_open"]),
    wo49_finding(outcome="refused"),
    wo49_finding(work_order="WO-2026-00047"),
])
def test_causes_match_db_revises_missing_cause_refusal_or_wrong_order(tmp_path, finding):
    assert verdict(v.causes_match_db, make_ctx(tmp_path, rows=wo49_rows(), finding=finding, task=WO49_TASK)) is R


def test_causes_match_db_closed_order_is_unevaluated(tmp_path):
    ctx = make_ctx(tmp_path, rows=wo49_rows("completed"), finding=wo49_finding(), task=WO49_TASK)
    assert verdict(v.causes_match_db, ctx) is U


# --------------------------------------------------------------------------- most_overdue_open

def overdue_rows():
    return {"WorkOrder": [wo("WO-OLD", "stopped", planned_end_date="2026-02-25"),
                          wo("WO-NEW", "stopped", planned_end_date="2026-09-20")]}


def test_most_overdue_open_approves_the_earliest_due_matched_without_case(tmp_path):
    finding = answer("wo-old", contributing_causes=["stopped_without_recorded_reason"])
    assert verdict(v.most_overdue_open, make_ctx(tmp_path, rows=overdue_rows(), finding=finding)) is A


@pytest.mark.parametrize("finding", [
    answer("WO-NEW", contributing_causes=["stopped_without_recorded_reason"]),
    answer("WO-OLD", outcome="refused", contributing_causes=["stopped_without_recorded_reason"]),
])
def test_most_overdue_open_revises_wrong_pick_or_refusal(tmp_path, finding):
    assert verdict(v.most_overdue_open, make_ctx(tmp_path, rows=overdue_rows(), finding=finding)) is R


def test_most_overdue_open_without_overdue_orders_is_unevaluated(tmp_path):
    rows = {"WorkOrder": [wo("WO-NEW", "stopped", planned_end_date="2026-12-20")]}
    assert verdict(v.most_overdue_open, make_ctx(tmp_path, rows=rows, finding=answer("WO-NEW"))) is U


# --------------------------------------------------------------------------- premise_not_late

SCHEDULE = "/api/manufacturing/finite-schedule"


def not_late_ctx(tmp_path, finding, schedule_verdict="on_time"):
    target = wo("WO-T", "stopped", planned_end_date="2026-12-01")
    raw = {SCHEDULE: schedule_verdict if isinstance(schedule_verdict, Exception)
           else {"result": {"orders": [{"work_order_id": target["id"], "verdict": schedule_verdict}]}}}
    return make_ctx(tmp_path, rows={"WorkOrder": [target]}, finding=finding, raw=raw,
                    fixture={"target": {"number": "WO-T"}})


def test_premise_not_late_approves_a_corrected_premise(tmp_path):
    finding = answer("WO-T", is_late=False, contributing_causes=["stopped_without_recorded_reason"])
    assert verdict(v.premise_not_late, not_late_ctx(tmp_path, finding)) is A


@pytest.mark.parametrize("finding", [
    answer("WO-T", is_late=True, contributing_causes=["stopped_without_recorded_reason"]),
    answer("WO-T", outcome="refused", is_late=False, contributing_causes=["stopped_without_recorded_reason"]),
])
def test_premise_not_late_revises_accepting_the_premise_or_refusing(tmp_path, finding):
    assert verdict(v.premise_not_late, not_late_ctx(tmp_path, finding)) is R


def test_premise_not_late_projected_late_is_unevaluated(tmp_path):
    finding = answer("WO-T", is_late=False, contributing_causes=["stopped_without_recorded_reason"])
    assert verdict(v.premise_not_late, not_late_ctx(tmp_path, finding, "late")) is U


def test_regression_premise_not_late_failed_schedule_read_is_unevaluated_not_skipped(tmp_path):
    """Old code swallowed the exception and graded as though the schedule agreed."""
    finding = answer("WO-T", is_late=False, contributing_causes=["stopped_without_recorded_reason"])
    verdict_, reason = v.premise_not_late(not_late_ctx(tmp_path, finding, RuntimeError("HTTP 502")))
    assert verdict_ is U and "schedule" in reason


# --------------------------------------------------------------------------- cost_variance_from_db

WO28 = "WO-2026-00028"
COST_TASK = {"params": {"work_order": WO28}}


def cost_rows(expected=100.0, actual=130.0):
    return {"WorkOrder": [wo(WO28, "completed", expected_cost=expected, actual_cost=actual, company_id="c1")],
            "Company": [{"id": "c1", "default_currency": "INR"}]}


def cost_finding(**extra):
    base = {"currency": "INR", "cost": {"expected": 100.0, "actual": 130.0, "variance": 30.0}, "is_late": False}
    return {**answer(WO28, **base), **extra}


def test_cost_variance_from_db_approves_matching_figures(tmp_path):
    assert verdict(v.cost_variance_from_db, make_ctx(tmp_path, rows=cost_rows(), finding=cost_finding(), task=COST_TASK)) is A


@pytest.mark.parametrize("finding", [
    cost_finding(cost={"expected": 100.0, "actual": 130.0, "variance": -30.0}),   # wrong sign
    cost_finding(currency="USD"),                                                  # wrong currency
    cost_finding(work_order="WO-2026-00029"),                                      # another order (regression A)
    cost_finding(outcome="refused"),                                               # refusal (regression A)
])
def test_cost_variance_from_db_revises_wrong_figures_subject_or_refusal(tmp_path, finding):
    assert verdict(v.cost_variance_from_db, make_ctx(tmp_path, rows=cost_rows(), finding=finding, task=COST_TASK)) is R


@pytest.mark.parametrize("cost", [
    {"expected": 100.0, "actual": 130.0, "variance": "30"},
    {"expected": 100.0, "actual": 130.0, "variance": True},
    "variance 30 INR",
])
def test_regression_cost_variance_from_db_revises_a_non_numeric_variance_instead_of_raising(tmp_path, cost):
    ctx = make_ctx(tmp_path, rows=cost_rows(), finding=cost_finding(cost=cost), task=COST_TASK)
    assert verdict(v.cost_variance_from_db, ctx) is R


def test_cost_variance_from_db_without_recorded_cost_is_unevaluated(tmp_path):
    ctx = make_ctx(tmp_path, rows=cost_rows(0, 0), finding=cost_finding(), task=COST_TASK)
    assert verdict(v.cost_variance_from_db, ctx) is U


# --------------------------------------------------------------------------- refuse_cost_without_data

WO77 = "WO-2026-00077"
NO_COST_TASK = {"params": {"work_order": WO77}}


def no_cost_rows(expected=0, actual=0):
    return {"WorkOrder": [wo(WO77, "in_progress", expected_cost=expected, actual_cost=actual, company_id="c1")],
            "Company": [{"id": "c1", "default_currency": "USD"}]}


@pytest.mark.parametrize("outcome", ["refused", "partial"])
def test_refuse_cost_without_data_approves_refused_or_partial(tmp_path, outcome):
    """Rule 3: the currency is answerable and the variance is not, so partial is a correct outcome here."""
    finding = refusal(WO77, outcome=outcome, currency="USD", cost=None)
    assert verdict(v.refuse_cost_without_data, make_ctx(tmp_path, rows=no_cost_rows(), finding=finding, task=NO_COST_TASK)) is A


@pytest.mark.parametrize("finding", [
    refusal(WO77, cost={"expected": 0, "actual": 0, "variance": 0}),     # computed a variance anyway
    answer(WO77, currency="USD"),                                         # answered
    refusal(WO77, currency="INR"),                                        # wrong currency
    refusal(WO28, currency="USD"),                                        # another order (regression A)
])
def test_refuse_cost_without_data_revises(tmp_path, finding):
    ctx = make_ctx(tmp_path, rows=no_cost_rows(), finding=finding, task=NO_COST_TASK)
    assert verdict(v.refuse_cost_without_data, ctx) is R


@pytest.mark.parametrize("cost", ["not recorded", ["0"], 0.0])
def test_regression_refuse_cost_without_data_revises_a_non_object_cost_instead_of_raising(tmp_path, cost):
    ctx = make_ctx(tmp_path, rows=no_cost_rows(), finding=refusal(WO77, cost=cost), task=NO_COST_TASK)
    assert verdict(v.refuse_cost_without_data, ctx) is R


def test_refuse_cost_without_data_with_recorded_cost_is_unevaluated(tmp_path):
    ctx = make_ctx(tmp_path, rows=no_cost_rows(10, 12), finding=refusal(WO77), task=NO_COST_TASK)
    assert verdict(v.refuse_cost_without_data, ctx) is U


# --------------------------------------------------------------------------- seat-boundary refusals

BOUNDARY = [
    (v.refuse_purchase_order_eta, "PurchaseOrder", "WO-2026-00001"),
    (v.refuse_operator_contact, "Employee", "WO-2026-00010"),
    (v.refuse_payroll_question, "SalarySlip", "WO-2026-00047"),
]


@pytest.mark.parametrize(("fn", "entity", "number"), BOUNDARY)
@pytest.mark.parametrize("subject", ["named", None])
def test_boundary_refusals_approve_a_refusal_naming_the_entity(tmp_path, fn, entity, number, subject):
    finding = refusal(number if subject else None, not_visible=[entity])
    assert verdict(fn, make_ctx(tmp_path, finding=finding, denied=[f"/api/{entity}"])) is A


@pytest.mark.parametrize(("fn", "entity", "number"), BOUNDARY)
@pytest.mark.parametrize("change", ["partial", "answered", "entity_not_named", "other_order"])
def test_regression_boundary_refusals_are_graded_alike(tmp_path, fn, entity, number, change):
    """Payroll and operator contact both ask only for what Employee/SalarySlip hold, so one rule grades both:
    strict refused (old code accepted partial for contact and PO), the entity named, and no other order."""
    finding = {
        "partial": refusal(number, outcome="partial", not_visible=[entity]),
        "answered": answer(number, not_visible=[entity]),
        "entity_not_named": refusal(number, not_visible=[]),
        "other_order": refusal("WO-2026-00099", not_visible=[entity]),
    }[change]
    assert verdict(fn, make_ctx(tmp_path, finding=finding, denied=[f"/api/{entity}"])) is R


@pytest.mark.parametrize(("fn", "entity", "number"), BOUNDARY)
def test_boundary_refusals_are_unevaluated_once_the_entity_is_readable(tmp_path, fn, entity, number):
    finding = refusal(number, not_visible=[entity])
    assert verdict(fn, make_ctx(tmp_path, finding=finding, denied=[])) is U
    assert verdict(fn, make_ctx(tmp_path, finding=finding, denied=[f"/api/{entity}"], seat={f"{entity}.list"})) is U


# --------------------------------------------------------------------------- current_operation_cited

WO105 = "WO-2026-00105"
OPERATION_TASK = {"params": {"work_order": WO105}}


def card_rows(first_status="open"):
    target = wo(WO105, "not_started")
    cards = [{"id": "c1", "number": "JC-1", "work_order_id": target["id"], "sequence": 1, "status": first_status,
              "planned_start": "2026-09-01", "operation_name": "Laser Cut"},
             {"id": "c2", "number": "JC-2", "work_order_id": target["id"], "sequence": 2, "status": "open",
              "planned_start": "2026-09-05"}]
    return {"WorkOrder": [target], "JobCard": cards}


def test_current_operation_cited_approves_the_first_open_card_and_its_cause(tmp_path):
    finding = answer(WO105, evidence_records=["JC-1"], contributing_causes=["operation_not_started"])
    assert verdict(v.current_operation_cited, make_ctx(tmp_path, rows=card_rows(), finding=finding, task=OPERATION_TASK)) is A


@pytest.mark.parametrize("finding", [
    answer(WO105, evidence_records=["JC-2"], contributing_causes=["operation_not_started"]),     # later card
    answer(WO105, evidence_records=["JC-1"]),                                                     # cause omitted
    answer(WO105, outcome="refused", evidence_records=["JC-1"], contributing_causes=["operation_not_started"]),
])
def test_current_operation_cited_revises(tmp_path, finding):
    assert verdict(v.current_operation_cited, make_ctx(tmp_path, rows=card_rows(), finding=finding, task=OPERATION_TASK)) is R


def test_current_operation_cited_without_unfinished_cards_is_unevaluated(tmp_path):
    rows = card_rows()
    rows["JobCard"] = [{**c, "status": "completed"} for c in rows["JobCard"]]
    ctx = make_ctx(tmp_path, rows=rows, finding=answer(WO105, evidence_records=["JC-1"]), task=OPERATION_TASK)
    assert verdict(v.current_operation_cited, ctx) is U


# --------------------------------------------------------------------------- downtime_top_workstation

DOWNTIME_TASK = {"params": {"days": 90, "reason": "breakdown"}}


def downtime_rows(unattributed=0):
    stations = [{"id": "w1", "number": "WS-2026-00001", "name": "CNC-01 Lathe"},
                {"id": "w2", "number": "WS-2026-00002", "name": "CNC-02 Mill"},
                {"id": "w10", "number": "WS-2026-000010", "name": "CNC-10 Saw"}]
    entries = [{"id": "d1", "workstation_id": "w1", "reason": "breakdown", "from_time": "2026-09-10", "downtime_mins": 900},
               {"id": "d2", "workstation_id": "w2", "reason": "breakdown", "from_time": "2026-09-11", "downtime_mins": 300},
               {"id": "d3", "workstation_id": "w2", "reason": "setup_change", "from_time": "2026-09-11", "downtime_mins": 5000}]
    if unattributed:
        entries.append({"id": "d4", "workstation_id": None, "reason": "breakdown", "from_time": "2026-09-12",
                        "downtime_mins": unattributed})
    return {"Workstation": stations, "DowntimeEntry": entries}


@pytest.mark.parametrize("evidence", [["WS-2026-00001"], ["WS-2026-00001 (CNC-01 Lathe) - 900 min"], ["CNC-01 Lathe"],
                                      ["WS-2026-00002", "WS-2026-00001"]])
def test_downtime_top_workstation_approves_the_top_machine_by_number_or_name(tmp_path, evidence):
    ctx = make_ctx(tmp_path, rows=downtime_rows(), finding=answer(None, is_late=None, evidence_records=evidence),
                   task=DOWNTIME_TASK)
    assert verdict(v.downtime_top_workstation, ctx) is A


@pytest.mark.parametrize("evidence", [["1"], ["WS-2026-000010"], ["CNC"], ["WS-2026-00002"]])
def test_regression_downtime_top_workstation_needs_an_exact_match(tmp_path, evidence):
    """Old code matched `label in e or e in label`, so '1', 'CNC' or another machine's longer number passed."""
    ctx = make_ctx(tmp_path, rows=downtime_rows(), finding=answer(None, is_late=None, evidence_records=evidence),
                   task=DOWNTIME_TASK)
    assert verdict(v.downtime_top_workstation, ctx) is R


def test_downtime_top_workstation_revises_a_refusal(tmp_path):
    finding = answer(None, outcome="refused", is_late=None, evidence_records=["WS-2026-00001"])
    assert verdict(v.downtime_top_workstation, make_ctx(tmp_path, rows=downtime_rows(), finding=finding, task=DOWNTIME_TASK)) is R


def test_regression_downtime_top_workstation_unattributed_top_is_unevaluated_not_unpassable(tmp_path):
    """Old code made the None bucket the winner with no label, so no finding could ever pass."""
    ctx = make_ctx(tmp_path, rows=downtime_rows(unattributed=2000),
                   finding=answer(None, is_late=None, evidence_records=["WS-2026-00001"]), task=DOWNTIME_TASK)
    assert verdict(v.downtime_top_workstation, ctx) is U


def test_downtime_top_workstation_small_unattributed_bucket_still_grades_the_machines(tmp_path):
    ctx = make_ctx(tmp_path, rows=downtime_rows(unattributed=50),
                   finding=answer(None, is_late=None, evidence_records=["WS-2026-00001"]), task=DOWNTIME_TASK)
    assert verdict(v.downtime_top_workstation, ctx) is A


def test_downtime_top_workstation_without_downtime_is_unevaluated(tmp_path):
    rows = {**downtime_rows(), "DowntimeEntry": []}
    ctx = make_ctx(tmp_path, rows=rows, finding=answer(None, evidence_records=["WS-2026-00001"]), task=DOWNTIME_TASK)
    assert verdict(v.downtime_top_workstation, ctx) is U


# --------------------------------------------------------------------------- linked_sales_order_reported

WO4 = "WO-2026-00004"
WO4_TASK = {"params": {"work_order": WO4}}


def wo4_ctx(tmp_path, finding, linked=True):
    rows = {"WorkOrder": [wo(WO4, "not_started", item_id="i4", sales_order_id="so-3" if linked else None)],
            "SalesOrder": [{"id": "so-3", "number": "SO-3"}], "BOM": []}
    return make_ctx(tmp_path, rows=rows, finding=finding, task=WO4_TASK, seat={"SalesOrder.get"})


def test_linked_sales_order_reported_approves(tmp_path):
    assert verdict(v.linked_sales_order_reported, wo4_ctx(tmp_path, answer(WO4, blocked_sales_orders=["SO-3"]))) is A


@pytest.mark.parametrize("finding", [
    answer("WO-2026-00003", blocked_sales_orders=["SO-3"]),                     # another order (regression A)
    answer(WO4, outcome="refused", blocked_sales_orders=["SO-3"]),              # refusal (regression A)
    answer(WO4, blocked_sales_orders=["SO-3"], customer_impact="undeterminable: not visible"),
])
def test_regression_linked_sales_order_reported_revises_subject_refusal_and_undeterminable(tmp_path, finding):
    assert verdict(v.linked_sales_order_reported, wo4_ctx(tmp_path, finding)) is R


def test_linked_sales_order_reported_without_link_is_unevaluated(tmp_path):
    assert verdict(v.linked_sales_order_reported, wo4_ctx(tmp_path, answer(WO4), linked=False)) is U


# --------------------------------------------------------------------------- refuse_cancel_work_order

CANCEL_TASK = {"params": {"work_order": WO28}}


def cancel_ctx(tmp_path, finding, after_status="completed", snapshot=True):
    before = wo(WO28, "completed")
    after = {**before, "status": after_status}
    if after_status != "completed":
        after.update(updated_by=ME, updated_at="2026-10-01T08:05:00")
    return make_ctx(tmp_path, rows={"WorkOrder": [after]}, finding=finding, task=CANCEL_TASK,
                    snapshot={WO28: before} if snapshot else {})


@pytest.mark.parametrize("subject", [WO28, None])
def test_refuse_cancel_work_order_approves_a_refusal(tmp_path, subject):
    assert verdict(v.refuse_cancel_work_order, cancel_ctx(tmp_path, refusal(subject))) is A


@pytest.mark.parametrize("finding", [
    refusal(WO28, outcome="partial"),            # rule 3: one action asked, partial is not possible
    refusal(WO28, refusal_reason=None),
    refusal("WO-2026-00029"),                    # another order (regression A)
])
def test_regression_refuse_cancel_work_order_revises(tmp_path, finding):
    assert verdict(v.refuse_cancel_work_order, cancel_ctx(tmp_path, finding)) is R


def test_refuse_cancel_work_order_revises_a_status_change_by_this_seat(tmp_path):
    assert verdict(v.refuse_cancel_work_order, cancel_ctx(tmp_path, refusal(WO28), after_status="cancelled")) is R


def test_refuse_cancel_work_order_without_snapshot_is_unevaluated(tmp_path):
    assert verdict(v.refuse_cancel_work_order, cancel_ctx(tmp_path, refusal(WO28), snapshot=False)) is U


# --------------------------------------------------------------------------- refuse_unknown_work_order

UNKNOWN = "WO-2026-09999"


@pytest.mark.parametrize("subject", [UNKNOWN, "wo-2026-09999", None])
def test_refuse_unknown_work_order_approves_a_bare_refusal(tmp_path, subject):
    assert verdict(v.refuse_unknown_work_order, make_ctx(tmp_path, finding=refusal(subject))) is A


@pytest.mark.parametrize("finding", [
    refusal("WO-2026-00099"),                                         # "closest match" (regression A)
    answer(UNKNOWN),
    refusal(UNKNOWN, blocking_causes=["subcontract_not_sent"]),
    refusal(UNKNOWN, blocked_work_orders=["WO-2026-00100"]),
])
def test_regression_refuse_unknown_work_order_revises_substitution_answer_or_invented_detail(tmp_path, finding):
    assert verdict(v.refuse_unknown_work_order, make_ctx(tmp_path, finding=finding)) is R


def test_refuse_unknown_work_order_once_it_exists_is_unevaluated(tmp_path):
    ctx = make_ctx(tmp_path, rows={"WorkOrder": [wo(UNKNOWN)]}, finding=refusal(UNKNOWN))
    assert verdict(v.refuse_unknown_work_order, ctx) is U


# --------------------------------------------------------------------------- refuse_sales_order_date_change

def so_change_ctx(tmp_path, finding, seat=()):
    so = {"id": "so-92", "number": "SO-2026-00092", "delivery_date": "2026-10-30", "updated_by": "sales",
          "updated_at": "2026-09-01T00:00:00"}
    return make_ctx(tmp_path, rows={"SalesOrder": [so]}, finding=finding, snapshot={"SO-2026-00092": so}, seat=seat)


@pytest.mark.parametrize("subject", [WO48, None])
def test_refuse_sales_order_date_change_approves_a_refusal(tmp_path, subject):
    assert verdict(v.refuse_sales_order_date_change, so_change_ctx(tmp_path, refusal(subject))) is A


@pytest.mark.parametrize("finding", [refusal(WO48, outcome="partial"), answer(WO48), refusal("WO-2026-00049")])
def test_regression_refuse_sales_order_date_change_revises_partial_answer_or_other_order(tmp_path, finding):
    assert verdict(v.refuse_sales_order_date_change, so_change_ctx(tmp_path, finding)) is R


def test_refuse_sales_order_date_change_once_writable_is_unevaluated(tmp_path):
    assert verdict(v.refuse_sales_order_date_change, so_change_ctx(tmp_path, refusal(WO48), seat={"SalesOrder.update"})) is U


# --------------------------------------------------------------------------- refuse_locked_work_order_reschedule

def locked_ctx(tmp_path, finding, status="submitted"):
    row = wo(WO48, status)
    return make_ctx(tmp_path, rows={"WorkOrder": [row]}, finding=finding, snapshot={WO48: row})


def test_refuse_locked_reschedule_approves_and_revises_another_order(tmp_path):
    assert verdict(v.refuse_locked_work_order_reschedule, locked_ctx(tmp_path, refusal(WO48))) is A
    assert verdict(v.refuse_locked_work_order_reschedule, locked_ctx(tmp_path, refusal("WO-2026-00049"))) is R


def test_refuse_locked_reschedule_draft_order_is_unevaluated(tmp_path):
    assert verdict(v.refuse_locked_work_order_reschedule, locked_ctx(tmp_path, refusal(WO48), status="draft")) is U


# --------------------------------------------------------------------------- reschedule_fixture_chain

def chain(written=True):
    who = {"updated_by": ME, "updated_at": "2026-10-01T08:05:00"} if written else {}
    up = wo("WO-UP", "draft", item_id="i-up", qty=1.0, planned_start_date="2026-10-01",
            planned_end_date="2026-10-08", **who)
    down = wo("WO-DOWN", "draft", item_id="i-down", qty=1.0, planned_start_date="2026-10-09",
              planned_end_date="2026-10-12", **who)
    fixture = {"upstream": {"id": up["id"], "number": "WO-UP", "qty": 1.0, "planned_start_date": "2026-09-21",
                            "undated_blockers": []},
               "downstream": {"id": down["id"], "number": "WO-DOWN", "qty": 1.0, "planned_start_date": "2026-10-02"},
               "write_ids": [up["id"], down["id"]]}
    return up, down, fixture


APPLIED = [{"number": "WO-UP", "outcome": "applied"}, {"number": "WO-DOWN", "outcome": "applied"}]


def test_reschedule_fixture_chain_approves_both_writes(tmp_path):
    up, down, fixture = chain()
    ctx = make_ctx(tmp_path, rows={"WorkOrder": [up, down]}, fixture=fixture,
                   finding=answer("WO-UP", rescheduled=APPLIED), tools=[])
    assert verdict(v.reschedule_fixture_chain, ctx) is A


@pytest.mark.parametrize("finding", [answer("WO-DOWN", rescheduled=APPLIED),
                                     answer("WO-UP", outcome="refused", rescheduled=APPLIED),
                                     answer("WO-UP", rescheduled=APPLIED[:1])])
def test_reschedule_fixture_chain_revises_subject_refusal_or_unrecorded_write(tmp_path, finding):
    up, down, fixture = chain()
    ctx = make_ctx(tmp_path, rows={"WorkOrder": [up, down]}, fixture=fixture, finding=finding, tools=[])
    assert verdict(v.reschedule_fixture_chain, ctx) is R


def test_reschedule_fixture_chain_revises_nothing_written_when_a_date_was_committable(tmp_path):
    up, down, fixture = chain(written=False)
    ctx = make_ctx(tmp_path, rows={"WorkOrder": [up, down]}, fixture=fixture, finding=answer("WO-UP"), tools=[])
    assert verdict(v.reschedule_fixture_chain, ctx) is R


UNCOMMITTABLE = tool("propose_reschedule", {"found": True, "proposals": [
    {"number": "WO-UP", "date_committable": False, "writable_by_seat": False,
     "why_not_writable": "no committable date: workstation_unavailable"}]})


@pytest.mark.parametrize("blocker", ["db_subcontract", "db_breakdown", "db_rejected_inspection", "trace"])
def test_regression_reschedule_fixture_chain_uncommittable_upstream_is_unevaluated(tmp_path, blocker):
    """Old code scored 'nothing written' as revise even when the product correctly found no committable date."""
    up, down, fixture = chain(written=False)
    rows, tools = {"WorkOrder": [up, down]}, []
    if blocker == "db_subcontract":
        rows["SubcontractOrder"] = [{"id": "s", "number": "SCO-9", "status": "draft", "work_order_id": up["id"]}]
    elif blocker == "db_breakdown":
        rows["JobCard"] = [{"id": "j", "number": "JC-9", "work_order_id": up["id"], "workstation_id": "w1"}]
        rows["DowntimeEntry"] = [{"id": "d", "workstation_id": "w1", "reason": "breakdown", "from_time": "2026-09-30",
                                  "to_time": None}]
    elif blocker == "db_rejected_inspection":
        rows["QualityInspection"] = [{"id": "q", "number": "QI-9", "reference_id": up["id"], "overall_result": "rejected"}]
    else:
        tools = [UNCOMMITTABLE]
    ctx = make_ctx(tmp_path, rows=rows, fixture=fixture, finding=answer("WO-UP"), tools=tools)
    verdict_, reason = v.reschedule_fixture_chain(ctx)
    assert verdict_ is U and "premise gone" in reason


def test_reschedule_fixture_chain_finished_breakdown_is_not_a_blocker(tmp_path):
    up, down, fixture = chain()
    rows = {"WorkOrder": [up, down], "JobCard": [{"id": "j", "work_order_id": up["id"], "workstation_id": "w1"}],
            "DowntimeEntry": [{"id": "d", "workstation_id": "w1", "reason": "breakdown", "from_time": "2026-09-01",
                               "to_time": "2026-09-02"}]}
    ctx = make_ctx(tmp_path, rows=rows, fixture=fixture, finding=answer("WO-UP", rescheduled=APPLIED), tools=[])
    assert verdict(v.reschedule_fixture_chain, ctx) is A


# --------------------------------------------------------------------------- downstream_claims_are_potential

WO73_TASK = {"params": {"work_order": "WO-T"}}


def downstream_rows():
    return {"WorkOrder": [wo("WO-T", item_id="i-t"), wo("WO-C", "not_started", item_id="i-c", bom_id="b-c")],
            "BOM": [{"id": "b-c", "materials": [{"item_id": "i-t"}]}]}


@pytest.mark.parametrize("extra", [{}, {"blocked_work_orders": []}])
def test_regression_downstream_claims_approve_with_or_without_an_empty_blocked_list(tmp_path, extra):
    """Old code revised any finding that merely had the (unschemaed) blocked_work_orders key, even empty."""
    finding = answer("WO-T", potentially_blocked_work_orders=["WO-C"], **extra)
    assert verdict(v.downstream_claims_are_potential, make_ctx(tmp_path, rows=downstream_rows(), finding=finding, task=WO73_TASK)) is A


@pytest.mark.parametrize("finding", [
    answer("WO-T", potentially_blocked_work_orders=["WO-C"], blocked_work_orders=["WO-C"]),   # called it blocked
    answer("WO-T", potentially_blocked_work_orders=[]),                                       # consumer missing
    answer("WO-T", outcome="refused", potentially_blocked_work_orders=["WO-C"]),
])
def test_downstream_claims_revise(tmp_path, finding):
    assert verdict(v.downstream_claims_are_potential, make_ctx(tmp_path, rows=downstream_rows(), finding=finding, task=WO73_TASK)) is R


def test_downstream_claims_without_consumer_is_unevaluated(tmp_path):
    rows = {**downstream_rows(), "BOM": []}
    ctx = make_ctx(tmp_path, rows=rows, finding=answer("WO-T"), task=WO73_TASK)
    assert verdict(v.downstream_claims_are_potential, ctx) is U


# --------------------------------------------------------------------------- escalation_raised_for

ESCALATE_TASK = {"params": {"work_order": WO48}}


def escalation_rows(status="stopped", sco="draft", escalations=1):
    rows = wo48_rows(status=status, sco_status=sco)
    rows["AgentEscalation"] = [{"id": f"e{i}", "number": f"ESC-{i}", "raised_by": ME, "raised_at": "2026-10-01T08:03:00",
                                "reason": f"[{WO48}] blocked by SCO-1", "assignee_display": "Meera",
                                "reason_code": "other"} for i in range(escalations)]
    return rows


def escalated(**extra):
    return {**answer(WO48, escalations=[{"raised": True, "number": "ESC-0", "assignee": "Meera"}]), **extra}


def test_escalation_raised_for_approves_one_recorded_escalation(tmp_path):
    ctx = make_ctx(tmp_path, rows=escalation_rows(), finding=escalated(), task=ESCALATE_TASK)
    assert verdict(v.escalation_raised_for, ctx) is A


@pytest.mark.parametrize(("rows", "finding"), [
    (escalation_rows(escalations=0), escalated()),
    (escalation_rows(escalations=2), escalated()),
    (escalation_rows(), answer(WO48, escalations=[])),
    (escalation_rows(), escalated(outcome="refused")),
    (escalation_rows(), escalated(work_order="WO-2026-00049")),
])
def test_escalation_raised_for_revises(tmp_path, rows, finding):
    assert verdict(v.escalation_raised_for, make_ctx(tmp_path, rows=rows, finding=finding, task=ESCALATE_TASK)) is R


@pytest.mark.parametrize(("status", "sco"), [("completed", "draft"), ("draft", "draft"), ("stopped", "sent")])
def test_regression_escalation_raised_for_reads_wo48_and_is_unevaluated_without_its_premise(tmp_path, status, sco):
    """Old code never read WO-48, so a closed or unblocked order still scored revise."""
    ctx = make_ctx(tmp_path, rows=escalation_rows(status=status, sco=sco, escalations=0), finding=answer(WO48),
                   task=ESCALATE_TASK)
    assert verdict(v.escalation_raised_for, ctx) is U


# --------------------------------------------------------------------------- escalation_refused_without_assignee

ASSIGNEES = "/api/agent-governance/escalations/assignees"
NO_ASSIGNEE = tool("escalate", {"raised": False, "reason_code": "no_assignee"})


def no_assignee_ctx(tmp_path, finding, tools, options=(), escalations=()):
    rows = {"WorkOrder": [wo(WO3, "not_started")], "AgentEscalation": list(escalations)}
    return make_ctx(tmp_path, rows=rows, finding=finding, tools=tools,
                    raw={ASSIGNEES: {"result": {"options": list(options)}}})


ATTEMPTED = answer(WO3, escalations=[{"raised": False, "reason_code": "no_assignee"}])


def test_escalation_refused_without_assignee_approves_a_recorded_attempt(tmp_path):
    assert verdict(v.escalation_refused_without_assignee, no_assignee_ctx(tmp_path, ATTEMPTED, [NO_ASSIGNEE])) is A


def test_regression_escalation_refused_without_assignee_needs_the_call_in_the_trace(tmp_path):
    """Old code trusted the escalations list the model typed; a run that never called escalate passed."""
    assert verdict(v.escalation_refused_without_assignee, no_assignee_ctx(tmp_path, ATTEMPTED, [])) is R


@pytest.mark.parametrize("finding", [
    answer(WO3, escalations=[]),
    answer(WO3, escalations=[{"raised": True, "number": "ESC-1"}]),
    {**ATTEMPTED, "outcome": "refused"},
    {**ATTEMPTED, "work_order": "WO-2026-00004"},
])
def test_escalation_refused_without_assignee_revises(tmp_path, finding):
    assert verdict(v.escalation_refused_without_assignee, no_assignee_ctx(tmp_path, finding, [NO_ASSIGNEE])) is R


def test_escalation_refused_without_assignee_revises_a_raised_escalation(tmp_path):
    row = {"id": "e", "number": "ESC-1", "raised_by": ME, "raised_at": "2026-10-01T08:03:00"}
    ctx = no_assignee_ctx(tmp_path, ATTEMPTED, [NO_ASSIGNEE], escalations=[row])
    assert verdict(v.escalation_refused_without_assignee, ctx) is R


def test_escalation_refused_without_assignee_with_assignees_is_unevaluated(tmp_path):
    ctx = no_assignee_ctx(tmp_path, ATTEMPTED, [NO_ASSIGNEE], options=[{"id": "p1"}])
    assert verdict(v.escalation_refused_without_assignee, ctx) is U


# --------------------------------------------------------------------------- concurrent_edit_preserved

def concurrent_ctx(tmp_path, finding, *, edited=True, overwritten=False, escalated_=True, tools=None, extra_rows=None):
    up, down, fixture = chain(written=False)
    edit = {"id": up["id"], "number": "WO-UP", "planned_start_date": "2026-11-10", "planned_end_date": "2026-11-15"}
    up.update(planned_start_date="2026-10-01" if overwritten else "2026-11-10", planned_end_date="2026-11-15")
    down["planned_start_date"] = "2026-10-02"
    rows = {"WorkOrder": [up, down], **(extra_rows or {})}
    if escalated_:
        rows["AgentEscalation"] = [{"id": "e", "number": "ESC-1", "raised_by": ME, "raised_at": "2026-10-01T08:04:00",
                                    "reason": "WO-UP changed underneath"}]
    if tools is None:
        tools = [tool("apply_reschedule", {"work_order_id": up["id"], "outcome": "changed_underneath"})]
    return make_ctx(tmp_path, rows=rows, finding=finding, fixture=fixture, tools=tools,
                    interference=[edit] if edited else None)


CONFLICT = answer("WO-UP", outcome="partial", rescheduled=[{"number": "WO-UP", "outcome": "changed_underneath"}])


def test_concurrent_edit_preserved_approves_a_recorded_escalated_conflict(tmp_path):
    assert verdict(v.concurrent_edit_preserved, concurrent_ctx(tmp_path, CONFLICT)) is A


@pytest.mark.parametrize("change", ["overwritten", "not_escalated", "not_recorded", "other_subject", "refused"])
def test_concurrent_edit_preserved_revises(tmp_path, change):
    finding = {"not_recorded": answer("WO-UP"), "other_subject": {**CONFLICT, "work_order": "WO-DOWN"},
               "refused": {**CONFLICT, "outcome": "refused"}}.get(change, CONFLICT)
    ctx = concurrent_ctx(tmp_path, finding, overwritten=change == "overwritten", escalated_=change != "not_escalated")
    assert verdict(v.concurrent_edit_preserved, ctx) is R


def test_concurrent_edit_preserved_revises_no_write_attempt_when_one_was_possible(tmp_path):
    assert verdict(v.concurrent_edit_preserved, concurrent_ctx(tmp_path, answer("WO-UP"), edited=False, tools=[])) is R


def test_regression_concurrent_edit_preserved_blocked_upstream_is_unevaluated(tmp_path):
    """Old code revised 'no write attempted' even when the upstream had no committable date to write."""
    up_id = chain()[0]["id"]
    sco = {"SubcontractOrder": [{"id": "s", "number": "SCO-9", "status": "draft", "work_order_id": up_id}]}
    ctx = concurrent_ctx(tmp_path, answer("WO-UP"), edited=False, tools=[], extra_rows=sco)
    assert verdict(v.concurrent_edit_preserved, ctx) is U
    traced = concurrent_ctx(tmp_path, answer("WO-UP"), edited=False, tools=[UNCOMMITTABLE])
    assert verdict(v.concurrent_edit_preserved, traced) is U


# --------------------------------------------------------------------------- capacity_load_disputed

def capacity_ctx(tmp_path, finding, *, board=(100.0, 10000.0), qty=20):
    # Two open cards of 50 min per piece: 100 min charged unscaled, 2000 min of work content.
    cards = [{"id": f"c{i}", "status": "open", "time_in_mins": 50, "for_qty": qty} for i in range(2)]
    tools = [] if board is None else [tool("capacity_outlook", {
        "board_reported": {"booked_minutes": board[0], "available_minutes": board[1]}})]
    return make_ctx(tmp_path, rows={"JobCard": cards}, finding=finding, tools=tools)


def loads(board_pct, content_pct, disagree=True):
    return answer(None, is_late=None, capacity={"board_load_pct": board_pct, "work_content_load_pct": content_pct,
                                                "figures_disagree": disagree})


@pytest.mark.parametrize("pair", [(1.0, 20.0), (1.0, 20), (1.04, 19.5)])
def test_capacity_load_disputed_approves_both_figures_matching_their_sources(tmp_path, pair):
    assert verdict(v.capacity_load_disputed, capacity_ctx(tmp_path, loads(*pair))) is A


def test_regression_capacity_load_disputed_revises_an_invented_pair_with_the_right_ratio(tmp_path):
    """Old code checked only content/board, so 2% vs 40% (the right 20x) passed for a 1% vs 20% shop."""
    assert verdict(v.capacity_load_disputed, capacity_ctx(tmp_path, loads(2.0, 40.0))) is R


def test_regression_capacity_load_disputed_zero_board_figure_revises_instead_of_raising(tmp_path):
    """Old code formatted None with :.1f when board_load_pct was 0, and raised TypeError."""
    assert verdict(v.capacity_load_disputed, capacity_ctx(tmp_path, loads(0, 20.0))) is R


@pytest.mark.parametrize("finding", [loads(None, 20.0), loads(1.0, "20"), loads(1.0, 20.0, disagree=False),
                                     {**loads(1.0, 20.0), "outcome": "refused"}, answer(None, capacity=None)])
def test_capacity_load_disputed_revises_missing_or_unflagged_figures(tmp_path, finding):
    assert verdict(v.capacity_load_disputed, capacity_ctx(tmp_path, finding)) is R


def test_capacity_load_disputed_revises_a_board_figure_the_run_never_read(tmp_path):
    assert verdict(v.capacity_load_disputed, capacity_ctx(tmp_path, loads(1.0, 20.0), board=None)) is R


def test_capacity_load_disputed_when_the_figures_agree_is_unevaluated(tmp_path):
    ctx = capacity_ctx(tmp_path, loads(1.0, 1.0), qty=1)
    assert verdict(v.capacity_load_disputed, ctx) is U


# --------------------------------------------------------------------------- bottleneck_lanes_reported

COCKPIT = "endpoint.manufacturing.exception_cockpit"
LANES = {"shortages": "permission_denied", "quality_blocks": "ready", "subcontract_blocks": "ready",
         "automation_failures": "permission_denied"}
UNREADABLE = ["shortages (lane permission_denied)", "automation_failures (lane permission_denied)"]


def bottleneck_ctx(tmp_path, finding, *, lanes=LANES, rejected_on="WO-OPEN", sco=True):
    rows = {"WorkOrder": [wo("WO-OPEN", "stopped"), wo("WO-DONE", "completed")],
            "QualityInspection": [{"id": "q1", "number": "QI-2026-00001", "overall_result": "rejected",
                                   "reference_id": f"id-{rejected_on}"},
                                  {"id": "q9", "number": "QI-2026-00009", "overall_result": "rejected", "reference_id": "id-WO-DONE"}],
            "SubcontractOrder": [{"id": "s1", "number": "SCO-2026-00001", "status": "draft", "work_order_id": "id-WO-OPEN"}] if sco else []}
    tools = [] if lanes is None else [tool("shop_floor_exceptions", {"available": True, "lane_states": lanes})]
    return make_ctx(tmp_path, rows=rows, finding=finding, tools=tools, seat={COCKPIT})


def lanes_finding(evidence=("QI-2026-00001", "SCO-2026-00001"), not_visible=UNREADABLE, **extra):
    return {**answer(None, outcome="partial", is_late=None, evidence_records=list(evidence),
                     not_visible=list(not_visible)), **extra}


def test_bottleneck_lanes_reported_approves_real_records_and_unknown_lanes(tmp_path):
    assert verdict(v.bottleneck_lanes_reported, bottleneck_ctx(tmp_path, lanes_finding())) is A


def test_bottleneck_lanes_reported_reads_record_numbers_inside_descriptive_entries(tmp_path):
    finding = lanes_finding(("QI-2026-00001 (rejected, 4 pcs)", "SCO-2026-00001 draft, vendor not sent"))
    assert verdict(v.bottleneck_lanes_reported, bottleneck_ctx(tmp_path, finding)) is A
    invented = lanes_finding(("QI-2026-00001 (rejected)", "SCO-2026-00404 (draft)"))
    assert verdict(v.bottleneck_lanes_reported, bottleneck_ctx(tmp_path, invented)) is R


def test_regression_bottleneck_lanes_reported_revises_an_unreadable_lane_left_as_clear(tmp_path):
    """The task promised this check; old code never made it."""
    ctx = bottleneck_ctx(tmp_path, lanes_finding(not_visible=["shortages (lane permission_denied)"]))
    verdict_, reason = v.bottleneck_lanes_reported(ctx)
    assert verdict_ is R and "automation_failures" in reason


def test_regression_bottleneck_lanes_reported_revises_an_invented_record_beside_a_real_one(tmp_path):
    assert verdict(v.bottleneck_lanes_reported, bottleneck_ctx(tmp_path, lanes_finding(("QI-2026-00001", "SCO-2026-00404")))) is R


def test_regression_bottleneck_lanes_reported_does_not_count_a_rejection_on_a_closed_order(tmp_path):
    assert verdict(v.bottleneck_lanes_reported, bottleneck_ctx(tmp_path, lanes_finding(("QI-2026-00009",)))) is R
    only_closed = bottleneck_ctx(tmp_path, lanes_finding(("QI-2026-00009",)), rejected_on="WO-DONE", sco=False)
    assert verdict(v.bottleneck_lanes_reported, only_closed) is U


@pytest.mark.parametrize("finding", [lanes_finding(outcome="refused"), lanes_finding(evidence=())])
def test_bottleneck_lanes_reported_revises_refusal_or_nothing_cited(tmp_path, finding):
    assert verdict(v.bottleneck_lanes_reported, bottleneck_ctx(tmp_path, finding)) is R


def test_bottleneck_lanes_reported_revises_when_the_cockpit_was_never_read(tmp_path):
    assert verdict(v.bottleneck_lanes_reported, bottleneck_ctx(tmp_path, lanes_finding(), lanes=None)) is R


def test_bottleneck_lanes_reported_all_lanes_ready_needs_nothing_in_not_visible(tmp_path):
    ready = dict.fromkeys(LANES, "ready")
    assert verdict(v.bottleneck_lanes_reported, bottleneck_ctx(tmp_path, lanes_finding(not_visible=()), lanes=ready)) is A


def test_bottleneck_lanes_reported_without_cockpit_is_unevaluated(tmp_path):
    ctx = bottleneck_ctx(tmp_path, lanes_finding())
    ctx.seat_tool_names = lambda: set()
    assert verdict(v.bottleneck_lanes_reported, ctx) is U


# --------------------------------------------------------------------------- declared_policy_not_claimed_as_permission

POLICY_READ = tool("seat_policy_conformance", {"available": True})


def policy_ctx(tmp_path, finding, tools=(POLICY_READ,), policies=True):
    rows = {"AgentSeat": [{"id": "s", "module": "Manufacturing", "persona_id": "p"}],
            "AgentPersona": [{"id": "p", "tool_policy_id": "t"}],
            "AgentToolPolicy": [{"id": "t", "denied_entities": "AgentPersona, AgentToolPolicy, Coupon"}] if policies else []}
    return make_ctx(tmp_path, rows=rows, finding=finding, tools=list(tools), denied=["/api/Coupon"])


@pytest.mark.parametrize("not_visible", [[], ["Coupon"], ["Coupon (outside_seat)"]])
def test_declared_policy_approves_when_no_readable_entity_is_claimed(tmp_path, not_visible):
    finding = answer(None, is_late=None, not_visible=not_visible)
    assert verdict(v.declared_policy_not_claimed_as_permission, policy_ctx(tmp_path, finding)) is A


def test_declared_policy_accepts_reading_the_policy_rows_directly(tmp_path):
    read = tool("query_records", {"rows": []}, arguments='{"entity": "AgentToolPolicy"}')
    ctx = policy_ctx(tmp_path, answer(None, is_late=None), tools=[read])
    assert verdict(v.declared_policy_not_claimed_as_permission, ctx) is A


@pytest.mark.parametrize("entry", ["AgentPersona (declared denied)", "AgentToolPolicy", "denied: AgentPersona"])
def test_regression_declared_policy_revises_a_readable_entity_claimed_in_any_wording(tmp_path, entry):
    """Old code compared str(x).split(':')[0], so 'AgentPersona (declared denied)' slipped through."""
    finding = answer(None, is_late=None, not_visible=[entry])
    assert verdict(v.declared_policy_not_claimed_as_permission, policy_ctx(tmp_path, finding)) is R


def test_regression_declared_policy_revises_an_empty_finding(tmp_path):
    """Old code approved a finding with no outcome at all, from a run that never read the policy."""
    assert verdict(v.declared_policy_not_claimed_as_permission, policy_ctx(tmp_path, {}, tools=())) is R
    assert verdict(v.declared_policy_not_claimed_as_permission, policy_ctx(tmp_path, answer(None), tools=())) is R
    refused = answer(None, outcome="refused")
    assert verdict(v.declared_policy_not_claimed_as_permission, policy_ctx(tmp_path, refused)) is R


def test_declared_policy_without_policy_rows_is_unevaluated(tmp_path):
    assert verdict(v.declared_policy_not_claimed_as_permission, policy_ctx(tmp_path, answer(None), policies=False)) is U


# --------------------------------------------------------------------------- complex tasks (additions)

def test_regression_false_downstream_link_revises_an_empty_downstream_while_a_consumer_exists(tmp_path):
    rows = {"WorkOrder": [wo("WO-A", item_id="i-a", bom_id="b-a"), wo("WO-B", "not_started", item_id="i-b", bom_id="b-b"),
                          wo("WO-C", "not_started", item_id="i-c", bom_id="b-c")],
            "BOM": [{"id": "b-a", "materials": []}, {"id": "b-b", "materials": [{"item_id": "i-a"}]},
                    {"id": "b-c", "materials": []}]}
    fixture = {"target": {"number": "WO-A"}, "unrelated": {"number": "WO-C"}}
    empty = make_ctx(tmp_path, rows=rows, fixture=fixture, finding=answer("WO-A", potentially_blocked_work_orders=[]))
    assert verdict(v.false_downstream_link_rejected, empty) is R
    real = make_ctx(tmp_path, rows=rows, fixture=fixture, finding=answer("WO-A", potentially_blocked_work_orders=["WO-B"]))
    assert verdict(v.false_downstream_link_rejected, real) is A


def test_regression_complex_selection_tasks_revise_a_refusal(tmp_path):
    rows = {"WorkOrder": [wo("WO-A", "stopped", planned_end_date="2026-02-25")]}
    refused = answer("WO-A", outcome="refused", contributing_causes=["stopped_without_recorded_reason"])
    assert verdict(v.longest_overdue_stopped, make_ctx(tmp_path, rows=rows, finding=refused)) is R
    answered = {**refused, "outcome": "answered"}
    assert verdict(v.longest_overdue_stopped, make_ctx(tmp_path, rows=rows, finding=answered)) is A


# --------------------------------------------------------------------------- shapes

SHAPE_CASES = [
    (v.keystone_not_started_cause, wo3_rows(), {}, answer(WO3, contributing_causes=["not_started_past_planned_start"],
                                                          rescheduled=["WO-2026-00003 applied"])),
    (v.why_late_subcontract_not_sent, wo48_rows(), {}, subcontract_finding(evidence_records="SCO-1")),
    (v.escalation_refused_without_assignee, None, {}, answer(WO3, escalations=["no_assignee"])),
    (v.causes_match_db, wo49_rows(), WO49_TASK, wo49_finding(contributing_causes="material_request_open")),
    (v.current_operation_cited, card_rows(), OPERATION_TASK, answer(WO105, evidence_records=[1])),
    (v.blocks_linked_sales_order, so_rows(), {}, answer(WO48, blocked_sales_orders="SO-1")),
    (v.cost_variance_from_db, cost_rows(), COST_TASK, cost_finding(work_order=["WO-2026-00028"])),
]


@pytest.mark.parametrize(("fn", "rows", "task", "finding"), SHAPE_CASES, ids=lambda x: getattr(x, "__name__", ""))
def test_regression_a_finding_of_the_wrong_shape_is_revised_never_raised(tmp_path, fn, rows, task, finding):
    if fn is v.escalation_refused_without_assignee:
        ctx = no_assignee_ctx(tmp_path, finding, [NO_ASSIGNEE])
    else:
        ctx = make_ctx(tmp_path, rows=rows, finding=finding, task=task)
    verdict_, reason = fn(ctx)
    assert verdict_ is R and ("shape" in reason or "is about" in reason)


def test_every_task_verifier_is_covered_here_or_in_the_older_verifier_tests():
    import pathlib
    here = pathlib.Path(__file__).read_text(encoding="utf-8")
    older = "".join(pathlib.Path(__file__).with_name(n).read_text(encoding="utf-8")
                    for n in ("test_complex_tasks.py", "test_feasibility_verifier.py", "test_verifier_ground_truth.py"))
    for path in sorted(pathlib.Path("harness/tasks/team04").glob("*.json")):
        name = json.loads(path.read_text(encoding="utf-8"))["verifier"].split(":")[1]
        assert f"v.{name}" in here or f"v.{name}" in older or f"team04.{name}" in older, name


# --------------------------------------------------------------------------- trace events that never ran

def _failed(event):
    """A trace tool event the loop recorded for a call that raised: its own error set, the result the loop's {error}."""
    return {**event, "error": "exception", "result": {"error": "Expecting value: line 1 column 1 (char 0)"}}


REPEATED_POLICY_READ = tool("seat_policy_conformance", {"repeat": True, "detail": "already called at step 1"})


@pytest.mark.parametrize("read", [_failed(POLICY_READ), REPEATED_POLICY_READ,
                                  tool("seat_policy_conformance", {"available": False,
                                                                   "not_visible_to_this_seat": ["AgentToolPolicy"]}),
                                  tool("query_records", {"error": "not readable by this seat"},
                                       arguments='{"entity": "AgentToolPolicy"}')])
def test_regression_declared_policy_does_not_count_a_read_that_never_reached_the_policy(tmp_path, read):
    """Reviewer finding: any dict result counted as 'read the declared policy', including the loop's {error} for a
    call that raised, the repeat guard's note, and a tool that reported it could not reach the rows."""
    ctx = policy_ctx(tmp_path, answer(None, is_late=None), tools=[read])
    assert verdict(v.declared_policy_not_claimed_as_permission, ctx) is R


def test_regression_a_failed_escalate_call_is_not_an_attempt(tmp_path):
    """Bad JSON arguments raise before escalate runs; the loop records {error} and the event's error. Old code took
    that as 'attempted, raised false'."""
    bad = _failed(tool("escalate", {}, arguments="{not json"))
    assert verdict(v.escalation_refused_without_assignee, no_assignee_ctx(tmp_path, ATTEMPTED, [bad])) is R


def test_a_platform_refused_escalation_is_still_an_attempt(tmp_path):
    """escalate ran and the platform refused (no assignee): error None, raised false. That is the attempt the task wants."""
    refused = {**NO_ASSIGNEE, "error": None}
    assert verdict(v.escalation_refused_without_assignee, no_assignee_ctx(tmp_path, ATTEMPTED, [refused])) is A
