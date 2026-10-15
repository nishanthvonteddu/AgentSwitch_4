"""Reads are bounded, and a partial read never passes for a whole one.

An unfiltered `list_all` on a large table is thousands of sequential calls whose result is then cut
mid-JSON by the tool-content limit, leaving the model a fragment that looks complete. These cover both
halves: the ceiling that stops the paging, and the note that makes a cut visible.
"""
import json

from test_behaviour_fakes import DATE_OPERATORS, Book, work_order

from prod_agent import domain
from prod_agent.agent import TOOL_CONTENT_LIMIT, _tool_content
from prod_agent.mcp_client import LIST_ALL_MAX_ROWS, McpClient, RowPage


class PagingMcp(McpClient):
    """A client with a table of `total` rows and no network."""

    def __init__(self, total, page_size=200):
        self.total, self.page_size, self.calls = total, page_size, []

    def call(self, name, arguments=None):
        args = dict(arguments or {})
        self.calls.append((name, args))
        offset, limit = args.get("offset", 0), args.get("limit", self.page_size)
        rows = [{"id": f"r{i}", "status": "draft"} for i in range(offset, min(offset + limit, self.total))]
        return {"data": rows, "total": self.total}


# --------------------------------------------------------------- the ceiling

def test_a_huge_table_stops_at_the_ceiling_and_says_so():
    mcp = PagingMcp(total=1_000_000)
    rows = mcp.list_all("WorkOrder")
    assert len(rows) == LIST_ALL_MAX_ROWS
    assert rows.truncated is True
    assert rows.total == 1_000_000
    assert len(mcp.calls) == LIST_ALL_MAX_ROWS // 200, "must stop paging, not read the table"


def test_a_small_table_is_complete_and_not_flagged():
    mcp = PagingMcp(total=133)
    rows = mcp.list_all("WorkOrder")
    assert len(rows) == 133 and rows.truncated is False and rows.total == 133


def test_ceiling_is_honoured_exactly_at_the_boundary():
    rows = PagingMcp(total=LIST_ALL_MAX_ROWS).list_all("WorkOrder")
    assert len(rows) == LIST_ALL_MAX_ROWS and rows.truncated is False


def test_caller_may_lower_the_ceiling():
    mcp = PagingMcp(total=10_000)
    rows = mcp.list_all("WorkOrder", max_rows=400)
    assert len(rows) == 400 and rows.truncated is True


def test_rowpage_is_still_an_ordinary_list_for_existing_callers():
    rows = PagingMcp(total=5).list_all("WorkOrder")
    assert isinstance(rows, list) and isinstance(rows, RowPage)
    assert [r["id"] for r in rows] == ["r0", "r1", "r2", "r3", "r4"]
    assert len([r for r in rows if r["status"] == "draft"]) == 5


# --------------------------------------------------------------- filters reach the server

def test_filters_are_sent_on_every_page():
    mcp = PagingMcp(total=500)
    mcp.list_all("WorkOrder", status="draft,not_started")
    assert len(mcp.calls) > 1
    assert all(args.get("status") == "draft,not_started" for _, args in mcp.calls)


def _shop(**kwargs):
    """Open and closed orders, cards and changes, plus what each domain read needs to run end to end."""
    wo = work_order("WO-2026-00048", "in_progress", start="2026-09-01", end="2026-09-10")
    return wo, Book(tables={
        "WorkOrder": [wo, work_order("WO-2026-00028", "completed", end="2026-05-10", bom_id="bom-x"),
                      work_order("WO-2026-00031", "cancelled", end="2026-06-10", bom_id="bom-x")],
        "BOM": [{"id": "bom-x", "number": "BOM-X", "materials": [{"item_id": wo["item_id"], "qty": 1}]}],
        "JobCard": [{"id": "jc-1", "number": "JC-1", "work_order_id": wo["id"], "status": "open", "time_in_mins": 10,
                     "for_qty": 5, "workstation_id": "ws-1"},
                    {"id": "jc-2", "number": "JC-2", "work_order_id": wo["id"], "status": "completed",
                     "time_in_mins": 999, "for_qty": 999, "workstation_id": "ws-1"}],
        "EngineeringChangeOrder": [
            {"id": "eco-1", "number": "ECO-1", "status": "submitted", "affected_work_orders": [{"work_order_id": wo["id"]}]},
            {"id": "eco-2", "number": "ECO-2", "status": "implemented", "affected_work_orders": [{"work_order_id": wo["id"]}]}],
        "DowntimeEntry": [{"id": "d-1", "workstation_id": "ws-1", "reason": "breakdown", "from_time": "2026-09-14T08:00:00",
                           "to_time": "2026-09-14T10:00:00", "downtime_mins": 120}],
        "Workstation": [{"id": "ws-1", "number": "WS-1", "name": "Press", "working_hours_per_day": 8, "capacity": 1}],
        "Company": [{"id": f"other-co-{i}", "name": f"Company {i}"} for i in range(300)] + [
            {"id": "co-1", "name": "Suryodaya Precision", "country": "India", "default_currency": "INR"}],
        "Item": [{"id": wo["item_id"], "code": "FG-1", "number": "ITEM-1", "name": "Bracket"}]},
        tools={"EngineeringChangeOrder.list", "JobCard.list", "DowntimeEntry.list"},
        endpoints={"endpoint.manufacturing.capacity_board": {"result": {"counts": {
            "booked_minutes": 50, "available_minutes": 10000}}}}, **kwargs)


def _every_read(wo, book):
    domain.list_late_work_orders(book)
    domain.diagnose(book, wo["number"])
    domain.downstream_impact(book, wo["number"])
    domain.capacity_outlook(book)
    domain.order_feasible_by(book, wo["number"], "2026-12-01")
    domain.downtime_summary(book, 30)
    domain.find_orders_for_item(book, "FG-1")
    domain.company_context(book)


def test_status_scans_are_narrowed_at_the_server():
    """Confirmed over MCP on Suryodaya 2026-09-29: status=draft,not_started returned 78 = 40 + 38."""
    wo, book = _shop()
    _every_read(wo, book)
    sent = {}
    for name, args in book.calls:
        if "status" in args:
            sent.setdefault(name, set()).add(args["status"])
    assert sent["WorkOrder.list"] >= {"draft,in_progress,not_started,stopped"}
    assert sent["EngineeringChangeOrder.list"] == {"draft,submitted,under_review"}
    assert sent["JobCard.list"] == {"in_progress,not_started,open,paused"}
    assert domain._csv(domain.OPEN_WO) == "draft,in_progress,not_started,stopped"


def test_date_operators_are_not_used_on_this_interface():
    """The MCP schema declares planned_end_date {"format": "date"} and rejects lt:/gte:/between:,
    though REST accepts them on the same login. Filed as a bug; not usable here meanwhile. The fake book
    rejects them the same way, so every domain read below would raise if one were sent."""
    wo, book = _shop()
    _every_read(wo, book)
    sent = [str(v) for _, args in book.calls for v in args.values()]
    assert not [v for v in sent if v.split(":", 1)[0] in DATE_OPERATORS]


def test_python_status_checks_are_kept_as_well():
    """A server filter this platform ignores must not silently widen the result."""
    wo, book = _shop(ignored_filters={"status"})
    assert [r["number"] for r in domain.list_late_work_orders(book)] == ["WO-2026-00048"]
    assert domain.downstream_impact(book, wo["number"])["potentially_blocked_work_orders"] == []
    assert [s["record"] for s in domain.diagnose(book, wo["number"])["signals"]
            if s["code"] == "engineering_change_pending"] == ["ECO-1"]
    assert domain.capacity_outlook(book)["work_content"]["minutes"] == 50.0, "the completed card must not count"
    assert domain.order_feasible_by(book, wo["number"], "2026-12-01")["remaining_work_content_minutes"] == 50.0


def test_company_context_reads_one_row_not_the_table():
    wo, book = _shop()
    out = domain.company_context(book)
    assert out["currency"] == "INR" and out["country"] == "India"
    company_reads = [args for name, args in book.calls if name == "Company.list"]
    assert company_reads == [{"limit": 1}], "one row, never the table"
    assert [args for name, args in book.calls if name == "Company.get"] == [{"id": "co-1"}]


# --------------------------------------------------------------- truncation is never silent

def test_short_result_is_passed_through_untouched():
    out = _tool_content({"a": 1})
    assert out == json.dumps({"a": 1})
    assert "TRUNCATED" not in out


def test_oversized_result_announces_the_cut():
    out = _tool_content({"rows": [{"pad": "x" * 100} for _ in range(2000)]})
    assert "[TRUNCATED:" in out
    assert "incomplete" in out
    assert "average" in out, "must tell the model not to aggregate over a fragment"


def test_truncated_output_says_how_much_was_lost():
    result = {"rows": ["y" * 200 for _ in range(1000)]}
    full = len(json.dumps(result, default=str))
    out = _tool_content(result)
    assert str(full) in out and str(TOOL_CONTENT_LIMIT) in out


def test_the_note_survives_the_cut():
    """The marker is appended after the cut, so it cannot itself be truncated away."""
    out = _tool_content({"rows": ["z" * 500 for _ in range(5000)]})
    assert out.rstrip().endswith("]")
    assert out.index("[TRUNCATED:") >= TOOL_CONTENT_LIMIT
