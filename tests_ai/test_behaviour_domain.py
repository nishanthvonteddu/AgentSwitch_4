"""What the deterministic domain functions conclude, driven through the real code over an in-memory book.

AI-written regression tests (ungraded); the team's hand-written tests live in tests/.

Each test states a rule the agent's answers rest on — which causes block, when an order is late, which sales
order is recorded exposure and which is only potential, how far a dependant moves and who may write it — and
checks it in the output of diagnose, list_late_work_orders, downstream_impact or propose_reschedule. Nothing
here re-states a production condition inside the test.

The xfail(strict=True) tests at the end record behaviour that looks wrong and is NOT fixed here: each one
asserts what a planner would need, fails today, and turns into an XPASS failure the day the code changes, so
the fix cannot land unnoticed.

Run: python -m pytest tests_ai/test_behaviour_domain.py -q
"""
import pytest
from test_behaviour_fakes import Book, pin_today, schedule_entry, work_order

from prod_agent import domain
from prod_agent.mcp_client import McpError


@pytest.fixture(autouse=True)
def pinned(monkeypatch):
    pin_today(monkeypatch)  # 2026-09-16


def _signals(diag, code):
    return [s for s in diag["signals"] if s["code"] == code]


def _sco(number, wo, status, expected="2026-09-01"):
    return {"id": f"id-{number}", "number": number, "work_order_id": wo["id"], "status": status,
            "expected_delivery_date": expected, "vendor_id": "v-1", "_vendor_id_display": "Anand Platers"}


def _bom_consuming(bom_id, *item_ids):
    return {"id": bom_id, "number": bom_id.upper(), "materials": [{"item_id": i, "qty": 1} for i in item_ids]}


# --------------------------------------------------------------- diagnose: causes and their weight

def test_a_stopped_order_reports_that_no_reason_was_recorded():
    """WO-2026-00049 live: stopped, and the model kept leaving this cause out. diagnose must always return it."""
    wo = work_order("WO-2026-00049", "stopped", end="2026-09-05")
    diag = domain.diagnose(Book(tables={"WorkOrder": [wo]}), "WO-2026-00049")
    [stopped] = _signals(diag, "stopped_without_recorded_reason")
    assert stopped["record"] == "WO-2026-00049" and stopped["blocking"] is False


def test_an_order_that_is_not_stopped_carries_no_stopped_signal():
    wo = work_order("WO-2026-00050", "in_progress", end="2026-09-05")
    assert _signals(domain.diagnose(Book(tables={"WorkOrder": [wo]}), "WO-2026-00050"),
                    "stopped_without_recorded_reason") == []


def test_a_material_shortage_blocks_because_no_ready_date_is_known():
    """A reliable stock check (some components in stock) with one short line: the shortage is a blocker."""
    wo = work_order("WO-2026-00061", "not_started", start="2026-09-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo]}, stock={wo["id"]: [
        {"item_id": "item-bracket", "required": 10, "available": 4, "shortage": 6, "is_critical": True},
        {"item_id": "item-bolt", "required": 40, "available": 40, "shortage": 0, "is_critical": False}]})
    diag = domain.diagnose(book, "WO-2026-00061")

    [short] = _signals(diag, "material_shortage")
    assert short["blocking"] is True and short["record"] == "item-bracket" and short["shortage"] == 6
    assert _signals(diag, "stock_check_unreliable") == []


def test_a_shortage_stops_the_reschedule_date_as_an_undated_blocker():
    """The same rule seen from propose_reschedule: no committable date, and the shortage named as why."""
    wo = work_order("WO-2026-00061", "draft", start="2026-09-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo]}, stock={wo["id"]: [
        {"item_id": "item-bracket", "required": 10, "available": 4, "shortage": 6, "is_critical": True}]})
    [p] = domain.propose_reschedule(book, "WO-2026-00061")["proposals"]
    assert p["writable_by_seat"] is False and p["date_committable"] is False
    assert p["new_start"] is None and "material_shortage" in p["why_not_writable"]


def test_a_failed_stock_check_is_reported_not_read_as_no_shortage():
    wo = work_order("WO-2026-00062", "not_started", start="2026-09-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo]},
                stock={wo["id"]: McpError(-32000, "stock service down", {"code": "upstream_error"})})
    diag = domain.diagnose(book, "WO-2026-00062")
    assert _signals(diag, "material_shortage") == []
    assert [s["error"] for s in _signals(diag, "stock_check_failed")] == ["stock service down"]


# --------------------------------------------------------------- diagnose: lateness

def test_an_order_the_schedule_projects_late_is_late_before_its_due_date():
    """Due 2026-09-30, but finite_schedule's verdict is late: the planner must hear it now, not on the 30th."""
    wo = work_order("WO-2026-00070", "in_progress", start="2026-09-10", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo]}, schedule=[schedule_entry(wo, "late", projected_finish="2026-10-04")])
    diag = domain.diagnose(book, "WO-2026-00070")
    assert diag["is_late"] is True
    assert diag["schedule"]["verdict"] == "late" and diag["schedule"]["projected_finish"] == "2026-10-04"


def test_an_order_on_schedule_and_not_yet_due_is_not_late():
    wo = work_order("WO-2026-00070", "in_progress", start="2026-09-10", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo]}, schedule=[schedule_entry(wo, "on_time")])
    assert domain.diagnose(book, "WO-2026-00070")["is_late"] is False


def test_a_past_due_open_order_is_late_and_says_by_how_much():
    wo = work_order("WO-2026-00071", "not_started", start="2026-08-20", end="2026-09-06")
    diag = domain.diagnose(Book(tables={"WorkOrder": [wo]}), "WO-2026-00071")
    assert diag["is_late"] is True and diag["days_past_due"] == 10


def test_a_completed_order_past_its_date_is_not_late():
    wo = work_order("WO-2026-00028", "completed", start="2026-05-01", end="2026-05-10")
    diag = domain.diagnose(Book(tables={"WorkOrder": [wo]}), "WO-2026-00028")
    assert diag["is_late"] is False and diag["days_past_due"] is None


# --------------------------------------------------------------- list_late_work_orders

def _late_book(**kwargs):
    return Book(tables={"WorkOrder": [
        work_order("WO-PAST", "not_started", start="2026-09-01", end="2026-09-10"),
        work_order("WO-STOPPED", "stopped", start="2026-07-20", end="2026-08-01"),
        work_order("WO-FUTURE", "in_progress", start="2026-09-10", end="2026-09-30"),
        work_order("WO-DONE", "completed", start="2026-08-20", end="2026-09-01"),
        work_order("WO-CANCELLED", "cancelled", start="2026-08-20", end="2026-09-02")]}, **kwargs)


def test_late_list_holds_past_due_open_orders_most_overdue_first():
    rows = domain.list_late_work_orders(_late_book())
    assert [r["number"] for r in rows] == ["WO-STOPPED", "WO-PAST"]
    assert [r["days_past_due"] for r in rows] == [46, 6]


def test_late_list_is_narrowed_by_status_at_the_server():
    """Confirmed over MCP on Suryodaya 2026-09-29: status=draft,not_started returned 78 = 40 + 38."""
    book = _late_book()
    domain.list_late_work_orders(book)
    sent = [args for name, args in book.calls if name == "WorkOrder.list"]
    assert sent and all(args["status"] == "draft,in_progress,not_started,stopped" for args in sent)


def test_late_list_still_excludes_closed_orders_when_the_server_ignores_the_status_filter():
    """A server filter this platform ignores must not silently widen the result."""
    rows = domain.list_late_work_orders(_late_book(ignored_filters={"status"}))
    assert [r["number"] for r in rows] == ["WO-STOPPED", "WO-PAST"]


def test_late_list_includes_an_order_the_schedule_projects_late():
    future = work_order("WO-PROJECTED", "in_progress", start="2026-09-10", end="2026-09-30")
    book = Book(tables={"WorkOrder": [future]}, schedule=[schedule_entry(future, "late", "2026-10-05")])
    [row] = domain.list_late_work_orders(book)
    assert row["number"] == "WO-PROJECTED" and row["projected_finish"] == "2026-10-05"


# --------------------------------------------------------------- downstream_impact

def _chain_book(tools=("SalesOrder.get",), failing=()):
    """WO-A makes item-WO-A; WO-B's BOM consumes it. Each carries its own sales order."""
    target = work_order("WO-A", "draft", start="2026-09-01", end="2026-09-05", sales_order_id="so-1")
    consumer = work_order("WO-B", "draft", start="2026-09-10", end="2026-09-13", bom_id="bom-b",
                          sales_order_id="so-2")
    return Book(tables={
        "WorkOrder": [target, consumer],
        "BOM": [_bom_consuming("bom-b", "item-WO-A"), _bom_consuming("bom-WO-A", "item-raw")],
        "SalesOrder": [{"id": "so-1", "number": "SO-1", "_party_id_display": "Kaveri Motors", "delivery_date": "2026-09-25",
                        "grand_total": 120000, "status": "to_deliver", "delivered_status": "not_delivered"},
                       {"id": "so-2", "number": "SO-2", "_party_id_display": "Deccan Pumps", "delivery_date": "2026-10-02",
                        "grand_total": 54000, "status": "to_deliver", "delivered_status": "not_delivered"}]},
        tools=tools, failing=failing)


def test_only_the_late_orders_own_sales_order_is_linked_exposure():
    out = domain.downstream_impact(_chain_book(), "WO-A")
    by_number = {s["number"]: s for s in out["blocked_sales_orders"]}
    assert by_number["SO-1"]["confidence"] == "linked" and by_number["SO-1"]["via_work_order"] == "WO-A"
    assert by_number["SO-1"]["customer"] == "Kaveri Motors"
    assert out["customer_impact"] == "known"


def test_a_sales_order_reached_through_a_consumer_is_only_potential():
    out = domain.downstream_impact(_chain_book(), "WO-A")
    so2 = next(s for s in out["blocked_sales_orders"] if s["number"] == "SO-2")
    assert so2["confidence"] == "potential" and so2["via_work_order"] == "WO-B"
    [consumer] = out["potentially_blocked_work_orders"]
    assert consumer["number"] == "WO-B" and consumer["confidence"] == "potential"
    assert consumer["consumes_output_of"] == "WO-A"


def test_without_sales_order_access_customer_impact_is_undeterminable():
    out = domain.downstream_impact(_chain_book(tools=()), "WO-A")
    assert out["customer_impact"].startswith("undeterminable")
    assert out["blocked_sales_orders"] == []


# --------------------------------------------------------------- propose_reschedule

def _proposals(book, ref="WO-A"):
    return {p["number"]: p for p in domain.propose_reschedule(book, ref)["proposals"]}


def test_a_dependant_starts_the_day_after_its_upstream_finishes():
    """WO-A (4 days) moves to start today, 2026-09-16, and ends 09-20; WO-B must start 09-21, not on the 20th."""
    got = _proposals(_chain_book())
    assert (got["WO-A"]["new_start"], got["WO-A"]["new_end"]) == ("2026-09-16", "2026-09-20")
    assert (got["WO-B"]["new_start"], got["WO-B"]["new_end"]) == ("2026-09-21", "2026-09-24")
    assert got["WO-B"]["reason"] == "must start after WO-A finishes"


def test_a_dependant_already_starting_after_its_upstream_is_left_alone():
    book = _chain_book()
    book.tables["WorkOrder"][1].update(planned_start_date="2026-09-25", planned_end_date="2026-09-28")
    assert "WO-B" not in _proposals(book)


def test_only_a_draft_order_is_writable_by_this_seat():
    book = _chain_book()
    book.tables["WorkOrder"].append(work_order("WO-C", "not_started", start="2026-09-12", end="2026-09-14",
                                               bom_id="bom-b"))
    got = _proposals(book)
    assert got["WO-A"]["writable_by_seat"] is True and got["WO-A"]["why_not_writable"] is None
    assert got["WO-B"]["writable_by_seat"] is True
    assert got["WO-C"]["writable_by_seat"] is False
    assert got["WO-C"]["why_not_writable"].startswith("status 'not_started': dates are locked")
    assert got["WO-C"]["date_committable"] is True, "the date is fine; only the status stops the write"


def test_a_not_started_target_is_proposed_but_not_writable():
    book = _chain_book()
    book.tables["WorkOrder"][0]["status"] = "not_started"
    got = _proposals(book)
    assert got["WO-A"]["writable_by_seat"] is False and "not_started" in got["WO-A"]["why_not_writable"]


# --------------------------------------------------------------- suspected defects, recorded not fixed

@pytest.mark.xfail(strict=True, reason="defect: every SalesOrder.get failing still reports 'none linked', which the "
                                       "prompt tells the model means no customer is affected")
def test_customer_impact_is_not_none_linked_when_every_sales_order_read_fails():
    out = domain.downstream_impact(_chain_book(failing={"SalesOrder.get"}), "WO-A")
    assert len(out["not_visible_to_this_seat"]) == 2
    assert not out["customer_impact"].startswith("none linked")


@pytest.mark.xfail(strict=True, reason="defect: a shortage from a stock check flagged stock_check_unreliable is "
                                       "still reported as a blocking cause")
def test_a_shortage_from_an_unreliable_stock_check_does_not_block():
    wo = work_order("WO-2026-00063", "not_started", start="2026-09-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo]}, stock={wo["id"]: [
        {"item_id": "item-a", "required": 4, "available": 0, "shortage": 4, "is_critical": True},
        {"item_id": "item-b", "required": 2, "available": 0, "shortage": 2, "is_critical": False}]})
    diag = domain.diagnose(book, "WO-2026-00063")
    assert _signals(diag, "stock_check_unreliable")
    assert not any(s["blocking"] for s in _signals(diag, "material_shortage"))


@pytest.mark.xfail(strict=True, reason="defect: a subcontract already received or in quality check is flagged "
                                       "subcontract_overdue once its expected date has passed")
@pytest.mark.parametrize("status", ["received", "quality_check"])
def test_a_received_subcontract_is_not_overdue(status):
    wo = work_order("WO-2026-00064", "in_progress", start="2026-08-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo], "SubcontractOrder": [_sco("SCO-2026-00040", wo, status)]})
    assert _signals(domain.diagnose(book, "WO-2026-00064"), "subcontract_overdue") == []


@pytest.mark.xfail(strict=True, reason="defect: a dependant of an in_progress target gets why_not_writable "
                                       "'no committable date: ' with no cause after the colon")
def test_a_dependant_of_a_running_order_says_why_it_cannot_be_written():
    book = _chain_book()
    book.tables["WorkOrder"][0]["status"] = "in_progress"
    why = _proposals(book)["WO-B"]["why_not_writable"]
    assert why.split(":", 1)[-1].strip(), f"empty reason: {why!r}"


@pytest.mark.xfail(strict=True, reason="defect: a draft past its planned end is counted late, though the platform's "
                                       "own late query is status in (not_started, in_progress, stopped)")
def test_a_past_due_draft_is_not_late():
    wo = work_order("WO-2026-00065", "draft", start="2026-08-01", end="2026-08-10")
    book = Book(tables={"WorkOrder": [wo]})
    assert domain.diagnose(book, "WO-2026-00065")["is_late"] is False
    assert domain.list_late_work_orders(book) == []
