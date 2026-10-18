"""What the deterministic domain functions conclude, driven through the real code over an in-memory book.

AI-written regression tests (ungraded); the team's hand-written tests live in tests/.

Each test states a rule the agent's answers rest on — which causes block, when an order is late, which sales
order is recorded exposure and which is only potential, how far a dependant moves and who may write it — and
checks it in the output of diagnose, list_late_work_orders, downstream_impact or propose_reschedule. Nothing
here re-states a production condition inside the test.

The sections after propose_reschedule pin five decisions that were strict xfails until they were made: customer
impact is "unknown"/"partly unknown", never "none linked", when a linked sales order cannot be read; an all-zero
stock check is stock_unverified (blocking, feasibility "unknown"), not a shortage; a subcontract whose goods are
back is not overdue; a dependant of a running or closed target gets a real date or a real reason; and a past-due
draft is late on purpose, with not_released saying why.

Run: python -m pytest tests_ai/test_behaviour_domain.py -q
"""
import pytest
from test_behaviour_fakes import Book, pin_today, schedule_entry, work_order

from prod_agent import agent, domain
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
    assert _signals(diag, "stock_unverified") == []


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


def _no_empty_why(proposals):
    """A proposal that cannot be committed always says what stops it: never "no committable date: " and nothing."""
    for p in proposals.values() if isinstance(proposals, dict) else proposals:
        if p["why_not_writable"] is not None:
            assert p["why_not_writable"].split(":", 1)[-1].strip(), f"empty reason: {p}"


# --------------------------------------------------------------- customer impact when sales orders cannot be read

def _with_mto_consumer(book, start="2026-09-11", end="2026-09-14"):
    """Adds WO-C: make-to-order, no sales order, consuming WO-A's output like WO-B."""
    book.tables["WorkOrder"].append(work_order("WO-C", "draft", start=start, end=end, bom_id="bom-b",
                                               production_strategy="make_to_order"))
    return book


def test_customer_impact_is_unknown_when_every_sales_order_read_fails():
    """Was 'none linked', which the prompt lets the model read as no customer affected. Both orders carry one."""
    out = domain.downstream_impact(_chain_book(failing={"SalesOrder.get"}), "WO-A")
    assert len(out["not_visible_to_this_seat"]) == 2
    impact = out["customer_impact"]
    assert impact.startswith("unknown: ")
    assert "so-1 via WO-A: permission_denied" in impact and "so-2 via WO-B: permission_denied" in impact
    assert out["blocked_sales_orders"] == []


def test_an_unread_sales_order_is_named_by_its_number_when_the_work_order_carries_it():
    book = _chain_book(failing={"SalesOrder.get"})
    book.tables["WorkOrder"][0]["_sales_order_id_display"] = "SO-1"
    impact = domain.downstream_impact(book, "WO-A")["customer_impact"]
    assert "SO-1 (id so-1) via WO-A: permission_denied" in impact and "so-2 via WO-B" in impact


def test_customer_impact_is_partly_unknown_when_some_sales_orders_read_and_some_fail():
    book = _chain_book()
    book.tables["SalesOrder"] = book.tables["SalesOrder"][:1]  # SO-2 no longer readable: not_found
    out = domain.downstream_impact(book, "WO-A")
    impact = out["customer_impact"]
    assert impact.startswith("partly unknown: ")
    assert "read SO-1 via WO-A" in impact and "could not read so-2 via WO-B: not_found" in impact
    assert [s["number"] for s in out["blocked_sales_orders"]] == ["SO-1"]


def test_partly_unknown_keeps_both_causes_an_unlinked_make_to_order_and_a_failed_read():
    book = _with_mto_consumer(_chain_book())
    book.tables["SalesOrder"] = book.tables["SalesOrder"][:1]
    impact = domain.downstream_impact(book, "WO-A")["customer_impact"]
    assert impact.startswith("partly unknown: ")
    assert "make-to-order without a linked sales order: WO-C" in impact
    assert "read SO-1 via WO-A" in impact and "could not read so-2 via WO-B: not_found" in impact


def test_unknown_keeps_an_unlinked_make_to_order_beside_the_failed_reads():
    impact = domain.downstream_impact(_with_mto_consumer(_chain_book(failing={"SalesOrder.get"})), "WO-A")["customer_impact"]
    assert impact.startswith("unknown: ")
    assert "so-1 via WO-A: permission_denied" in impact and "make-to-order without a linked sales order: WO-C" in impact


def test_make_to_order_alone_reads_as_before():
    impact = domain.downstream_impact(_with_mto_consumer(_chain_book()), "WO-A")["customer_impact"]
    assert impact == "partly unknown: make-to-order without a linked sales order: WO-C"


def test_none_linked_only_when_no_order_in_the_chain_carries_a_sales_order():
    book = _chain_book(failing={"SalesOrder.get"})
    for w in book.tables["WorkOrder"]:
        w["sales_order_id"] = None
    out = domain.downstream_impact(book, "WO-A")
    assert out["customer_impact"].startswith("none linked")
    assert not any(name == "SalesOrder.get" for name, _ in book.calls)


def test_a_seat_without_sales_orders_is_still_undeterminable_not_unknown():
    out = domain.downstream_impact(_chain_book(tools=(), failing={"SalesOrder.get"}), "WO-A")
    assert out["customer_impact"] == "undeterminable: sales orders are not visible to this seat"


def test_the_prompt_reads_unknown_customer_impact_as_unknown():
    assert 'If it starts with "undeterminable", "unknown" or "partly unknown"' in agent.SYSTEM_PROMPT


# --------------------------------------------------------------- stock that reads 0 everywhere

def _stock(wo, *items):
    return Book(tables={"WorkOrder": [wo]}, stock={wo["id"]: list(items)})


def _line(item, required, available):
    return {"item_id": item, "required": required, "available": available,
            "shortage": max(required - available, 0), "is_critical": item == "item-a"}


def test_an_all_zero_stock_check_is_unverified_not_a_shortage():
    """Was a blocking material_shortage per line beside a stock_check_unreliable that said not to trust them."""
    wo = work_order("WO-2026-00063", "not_started", start="2026-09-20", end="2026-09-30")
    diag = domain.diagnose(_stock(wo, _line("item-a", 4, 0), _line("item-b", 2, 0)), "WO-2026-00063")
    assert _signals(diag, "material_shortage") == [] and _signals(diag, "stock_check_unreliable") == []
    [unverified] = _signals(diag, "stock_unverified")
    assert unverified["blocking"] is True, "no date can be committed until stock is confirmed"
    assert [(i["item_id"], i["required"], i["shortage"]) for i in unverified["items"]] == [("item-a", 4, 4), ("item-b", 2, 2)]
    assert "could not be confirmed" in unverified["note"] and "empty store" in unverified["note"]


def test_stock_unverified_is_a_bare_cause_code_and_an_undated_blocker():
    assert agent.CAUSE_CODE.fullmatch(domain.STOCK_UNVERIFIED) and domain.STOCK_UNVERIFIED in domain.UNDATED_BLOCKERS


def test_one_component_in_stock_makes_the_check_believable_so_shortages_block():
    wo = work_order("WO-2026-00063", "not_started", start="2026-09-20", end="2026-09-30")
    diag = domain.diagnose(_stock(wo, _line("item-a", 4, 0), _line("item-b", 2, 1)), "WO-2026-00063")
    assert _signals(diag, "stock_unverified") == []
    assert [(s["record"], s["blocking"]) for s in _signals(diag, "material_shortage")] == [("item-a", True), ("item-b", True)]


def test_all_zero_with_nothing_required_raises_no_stock_signal():
    wo = work_order("WO-2026-00063", "not_started", start="2026-09-20", end="2026-09-30")
    diag = domain.diagnose(_stock(wo, _line("item-a", 0, 0)), "WO-2026-00063")
    assert not [s for s in diag["signals"] if s["code"] in ("stock_unverified", "material_shortage")]


def test_unverified_stock_stops_the_reschedule_date():
    wo = work_order("WO-2026-00063", "draft", start="2026-09-20", end="2026-09-30")
    [p] = domain.propose_reschedule(_stock(wo, _line("item-a", 4, 0)), "WO-2026-00063")["proposals"]
    assert p["date_committable"] is False and p["why_not_writable"] == "no committable date: stock_unverified"


def test_feasibility_is_unknown_when_unverified_stock_is_the_only_blocker():
    wo = work_order("WO-2026-00063", "not_started", start="2026-09-20", end="2026-09-30")
    out = domain.order_feasible_by(_stock(wo, _line("item-a", 4, 0)), "WO-2026-00063", "2026-10-30")
    assert out["verdict"] == "unknown" and out["blocking_causes"] == ["stock_unverified"]
    assert "stock could not be confirmed" in out["reason"] and "check stock" in out["reason"]


def test_feasibility_is_no_when_another_undated_blocker_stands_beside_unverified_stock():
    wo = work_order("WO-2026-00063", "not_started", start="2026-09-20", end="2026-09-30")
    book = _stock(wo, _line("item-a", 4, 0))
    book.tables["SubcontractOrder"] = [_sco("SCO-2026-00041", wo, "draft")]
    out = domain.order_feasible_by(book, "WO-2026-00063", "2026-10-30")
    assert out["verdict"] == "no" and out["blocking_causes"] == ["stock_unverified", "subcontract_not_sent"]


def test_feasibility_is_no_behind_a_real_shortage():
    wo = work_order("WO-2026-00063", "not_started", start="2026-09-20", end="2026-09-30")
    out = domain.order_feasible_by(_stock(wo, _line("item-a", 4, 0), _line("item-b", 2, 2)), "WO-2026-00063", "2026-10-30")
    assert out["verdict"] == "no" and out["blocking_causes"] == ["material_shortage"]


def test_feasibility_is_no_for_a_past_date_even_when_only_stock_is_unverified():
    wo = work_order("WO-2026-00063", "not_started", start="2026-09-20", end="2026-09-30")
    out = domain.order_feasible_by(_stock(wo, _line("item-a", 4, 0)), "WO-2026-00063", "2026-09-01")
    assert out["verdict"] == "no" and "already past" in out["reason"]


# --------------------------------------------------------------- subcontract stages

@pytest.mark.parametrize("status, expected, code, blocking", [
    ("draft", "2026-09-01", "subcontract_not_sent", True),
    ("draft", "2026-09-30", "subcontract_not_sent", True),
    ("submitted", "2026-09-01", "subcontract_overdue", True),
    ("materials_sent", "2026-09-01", "subcontract_overdue", True),
    ("in_progress", "2026-09-01", "subcontract_overdue", True),
    ("submitted", "2026-09-30", "subcontract_pending", False),
    ("materials_sent", "2026-09-30", "subcontract_pending", False),
    ("in_progress", "2026-09-30", "subcontract_pending", False),
    ("received", "2026-09-01", "subcontract_received", False),
    ("received", "2026-09-30", "subcontract_received", False),
    ("quality_check", "2026-09-01", "subcontract_in_inspection", False),
    ("quality_check", "2026-09-30", "subcontract_in_inspection", False),
])
def test_each_subcontract_stage_gives_its_signal(status, expected, code, blocking):
    wo = work_order("WO-2026-00064", "in_progress", start="2026-08-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo], "SubcontractOrder": [_sco("SCO-2026-00040", wo, status, expected)]})
    [s] = [s for s in domain.diagnose(book, "WO-2026-00064")["signals"] if s["code"].startswith("subcontract_")]
    assert (s["code"], s["blocking"], s["record"]) == (code, blocking, "SCO-2026-00040")
    assert (s["status"], s["expected_delivery_date"], s["vendor"]) == (status, expected, "Anand Platers")
    assert agent.CAUSE_CODE.fullmatch(s["code"])
    # Only a subcontract still out with the vendor has a ready date; goods back carry none to wait for.
    assert s.get("ready_date") == (expected if code == "subcontract_pending" else None)


@pytest.mark.parametrize("status", ["received", "quality_check"])
def test_a_received_subcontract_is_not_overdue(status):
    """Was subcontract_overdue (blocking) once the expected date passed, though the goods were already back."""
    wo = work_order("WO-2026-00064", "in_progress", start="2026-08-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo], "SubcontractOrder": [_sco("SCO-2026-00040", wo, status)]})
    diag = domain.diagnose(book, "WO-2026-00064")
    assert _signals(diag, "subcontract_overdue") == []
    assert not any(s["blocking"] for s in diag["signals"] if s["code"].startswith("subcontract_"))


def test_a_subcontract_in_inspection_says_no_finish_date_is_recorded():
    wo = work_order("WO-2026-00064", "in_progress", start="2026-08-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo], "SubcontractOrder": [_sco("SCO-2026-00040", wo, "quality_check")]})
    [s] = _signals(domain.diagnose(book, "WO-2026-00064"), "subcontract_in_inspection")
    assert "in inspection" in s["note"] and "no inspection finish date is recorded" in s["note"]


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_a_closed_subcontract_gives_no_signal(status):
    wo = work_order("WO-2026-00064", "in_progress", start="2026-08-20", end="2026-09-30")
    book = Book(tables={"WorkOrder": [wo], "SubcontractOrder": [_sco("SCO-2026-00040", wo, status)]})
    assert not [s for s in domain.diagnose(book, "WO-2026-00064")["signals"] if s["code"].startswith("subcontract_")]


@pytest.mark.parametrize("status", ["received", "quality_check"])
def test_goods_back_from_the_subcontractor_do_not_stop_the_reschedule_date(status):
    wo = work_order("WO-2026-00064", "draft", start="2026-09-01", end="2026-09-05")
    book = Book(tables={"WorkOrder": [wo], "SubcontractOrder": [_sco("SCO-2026-00040", wo, status)]})
    [p] = domain.propose_reschedule(book, "WO-2026-00064")["proposals"]
    assert p["date_committable"] is True and p["writable_by_seat"] is True


# --------------------------------------------------------------- reschedule around a target that is running or closed

def _grandchild(book, start="2026-09-12", end="2026-09-15"):
    """Adds WO-D, whose BOM consumes WO-B's output."""
    book.tables["WorkOrder"].append(work_order("WO-D", "draft", start=start, end=end, bom_id="bom-d"))
    book.tables["BOM"].append(_bom_consuming("bom-d", "item-WO-B"))
    return book


def test_a_dependant_of_a_running_order_says_why_it_cannot_be_written():
    """Was why_not_writable 'no committable date: ' with nothing after the colon."""
    book = _chain_book()
    book.tables["WorkOrder"][0]["status"] = "in_progress"
    got = _proposals(book)
    assert "WO-A" not in got, "a running target has nothing to move"
    assert got["WO-B"]["date_committable"] is False and got["WO-B"]["writable_by_seat"] is False
    assert got["WO-B"]["reason"] == "waits on WO-A, which is in progress; its finish date is not known"
    assert got["WO-B"]["why_not_writable"] == "no committable date: WO-A is in progress and its finish date is not known"
    _no_empty_why(got)


def test_a_running_order_with_a_projected_finish_dates_its_dependants_from_it():
    book = _chain_book()
    target = book.tables["WorkOrder"][0]
    target["status"] = "in_progress"
    book.schedule = [schedule_entry(target, "late", projected_finish="2026-09-25")]
    got = _proposals(book)
    assert "WO-A" not in got
    assert (got["WO-B"]["new_start"], got["WO-B"]["new_end"]) == ("2026-09-26", "2026-09-29")
    assert got["WO-B"]["writable_by_seat"] is True and got["WO-B"]["date_committable"] is True
    assert "platform's projected finish for WO-A" in got["WO-B"]["reason"] and "2026-09-25" in got["WO-B"]["reason"]


def test_a_projected_finish_already_past_counts_from_today():
    book = _chain_book()
    target = book.tables["WorkOrder"][0]
    target["status"] = "in_progress"
    book.schedule = [schedule_entry(target, "late", projected_finish="2026-09-10")]
    proposal = _proposals(book)["WO-B"]
    assert proposal["new_start"] == "2026-09-17"
    # Review: the reason called today's date the platform's projection. It must give the platform's own date.
    assert "projected WO-A to finish 2026-09-10, already past" in proposal["reason"]
    assert "counts from today, 2026-09-16" in proposal["reason"]


def test_a_future_projected_finish_is_named_as_the_platforms_date():
    book = _chain_book()
    target = book.tables["WorkOrder"][0]
    target["status"] = "in_progress"
    book.schedule = [schedule_entry(target, "late", projected_finish="2026-09-25")]
    proposal = _proposals(book)["WO-B"]
    assert proposal["new_start"] == "2026-09-26"
    assert "date from the platform's projected finish for WO-A, 2026-09-25" in proposal["reason"]


def test_a_running_order_with_an_undated_blocker_dates_nothing_even_with_a_projection():
    """finite_schedule's projection cannot see a blocker with no date, so it cannot carry the dependants."""
    book = _chain_book()
    target = book.tables["WorkOrder"][0]
    target["status"] = "in_progress"
    book.schedule = [schedule_entry(target, "late", projected_finish="2026-09-25")]
    book.tables["SubcontractOrder"] = [_sco("SCO-2026-00042", target, "draft")]
    got = _proposals(book)
    assert got["WO-B"]["date_committable"] is False
    assert got["WO-B"]["why_not_writable"] == "no committable date: subcontract_not_sent"


@pytest.mark.parametrize("status", ["completed", "cancelled"])
def test_a_closed_target_constrains_no_dependant(status):
    """Its dependants keep their dates; one waiting on a dependant still follows that dependant's own planned end."""
    book = _grandchild(_chain_book())
    book.tables["WorkOrder"][0]["status"] = status
    got = _proposals(book)
    assert "WO-A" not in got and "WO-B" not in got
    # WO-B is planned to end 2026-09-13 and WO-D starts on the 12th, so WO-D moves to the 14th.
    assert (got["WO-D"]["new_start"], got["WO-D"]["new_end"]) == ("2026-09-14", "2026-09-17")
    assert got["WO-D"]["reason"] == "must start after WO-B finishes"


def test_a_stopped_target_is_still_proposed_but_not_writable():
    book = _chain_book()
    book.tables["WorkOrder"][0]["status"] = "stopped"
    got = _proposals(book)
    assert (got["WO-A"]["new_start"], got["WO-A"]["new_end"]) == ("2026-09-16", "2026-09-20")
    assert got["WO-A"]["date_committable"] is True and got["WO-A"]["writable_by_seat"] is False
    assert got["WO-A"]["why_not_writable"].startswith("status 'stopped'")
    assert got["WO-B"]["new_start"] == "2026-09-21"


def test_waiting_on_a_running_dependant_names_it_rather_than_the_target():
    book = _grandchild(_chain_book())
    book.tables["WorkOrder"][1]["status"] = "in_progress"
    got = _proposals(book)
    assert got["WO-D"]["reason"] == "waits on WO-B, which is in progress; its finish date is not known"
    assert got["WO-D"]["why_not_writable"] == "no committable date: WO-B is in progress and its finish date is not known"


def test_a_blocked_target_passes_its_blockers_down_the_chain():
    book = _grandchild(_chain_book())
    book.tables["SubcontractOrder"] = [_sco("SCO-2026-00043", book.tables["WorkOrder"][0], "draft")]
    got = _proposals(book)
    assert got["WO-A"]["why_not_writable"] == "no committable date: subcontract_not_sent"
    assert got["WO-B"]["reason"] == "waits on WO-A, whose date cannot be committed"
    assert got["WO-B"]["why_not_writable"] == got["WO-D"]["why_not_writable"] == "no committable date: subcontract_not_sent"
    _no_empty_why(got)


# --------------------------------------------------------------- drafts are late on purpose

def test_a_past_due_draft_is_late_on_purpose_and_says_it_is_unreleased():
    """The platform's late query leaves drafts out; we count them, because the harness's own late orders are past-due
    drafts (late_draft_chain, complex_planted_instruction). not_released tells the planner it was never released."""
    wo = work_order("WO-2026-00065", "draft", start="2026-08-01", end="2026-08-10")
    book = Book(tables={"WorkOrder": [wo]})
    diag = domain.diagnose(book, "WO-2026-00065")
    assert "draft" in domain.OPEN_WO
    assert diag["is_late"] is True and diag["days_past_due"] == 37
    [unreleased] = _signals(diag, "not_released")
    assert unreleased["record"] == "WO-2026-00065" and unreleased["blocking"] is False
    assert [r["number"] for r in domain.list_late_work_orders(book)] == ["WO-2026-00065"]
