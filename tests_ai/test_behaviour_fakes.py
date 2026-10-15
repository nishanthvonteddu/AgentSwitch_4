"""An in-memory book behind the real McpClient paging code, shared by the behaviour tests.

AI-written regression tests (ungraded); the team's hand-written tests live in tests/.

Only the transport is fake. `Book` subclasses McpClient, so `list_all` and `list_window` are the real ones,
and `call` answers `.list` the way the platform does: equality filters, a comma list meaning OR, `search`,
paging with a total. Rows carry the platform's own field names, so the domain functions see what they see
live, and a test that needs the platform to ignore a filter says so with `ignored_filters`.

The tests at the bottom pin the fake itself: a fake that answers more kindly than the platform would make
every test built on it pass for the wrong reason.

Run: python -m pytest tests_ai -k behaviour -q
"""
import datetime as dt
import json
import threading
from types import SimpleNamespace

import pytest

from prod_agent import config
from prod_agent.agent import ProductionAgent
from prod_agent.mcp_client import McpClient, McpError

TODAY = dt.date(2026, 9, 16)
_CONTROLS = {"limit", "offset", "sort_by", "sort_order"}
DATE_OPERATORS = {"lt", "lte", "gt", "gte", "between"}


def work_order(number, status="not_started", start="2026-09-01", end="2026-09-10", **fields):
    """A WorkOrder row as WorkOrder.get returns it (the fields domain._wo_summary reads, plus links)."""
    return {"id": f"id-{number}", "number": number, "status": status, "item_id": f"item-{number}",
            "_item_id_display": f"Item made by {number}", "bom_id": f"bom-{number}", "priority": "medium",
            "qty": 10, "produced_qty": 0, "planned_start_date": start, "planned_end_date": end,
            "sales_order_id": None, "production_strategy": "make_to_stock", "expected_cost": 0, "actual_cost": 0,
            "company_id": "co-1", "quality_inspection_required": 0, "updated_at": "2026-09-10T08:00:00Z", **fields}


def schedule_entry(wo, verdict="on_time", projected_finish=None, causes=()):
    """One `orders` entry of endpoint.manufacturing.finite_schedule."""
    return {"work_order_id": wo["id"], "verdict": verdict, "projected_finish": projected_finish,
            "days_late": 0, "causes": list(causes), "operations": []}


class Book(McpClient):
    def __init__(self, *, tables=None, tools=(), stock=None, schedule=(), workstation_load=(), failing=(),
                 ignored_filters=(), endpoints=None, instance="suryodaya"):
        self.session = SimpleNamespace(instance=instance)
        self._id, self._lock = 0, threading.Lock()
        self._tools = {name: {} for name in tools}
        self.tables = {entity: [dict(r) for r in rows] for entity, rows in (tables or {}).items()}
        self.stock = stock or {}          # work order id -> check_stock_availability items, or an McpError
        self.schedule = list(schedule)    # finite_schedule orders
        self.workstation_load = list(workstation_load)  # finite_schedule workstation_load
        self.failing = set(failing)       # tool names that answer with a permission error
        self.ignored_filters = set(ignored_filters)
        self.endpoints = dict(endpoints or {})  # any other endpoint.* name -> its result envelope
        self.calls, self.writes, self.memories = [], [], []

    def call(self, name, arguments=None):
        args = dict(arguments or {})
        self.calls.append((name, args))
        if name in self.failing:
            raise McpError(-32001, f"{name}: permission denied", {"code": "permission_denied"})
        entity, _, op = name.partition(".")
        if op == "list":
            return self._list(entity, args)
        if op == "get":
            row = next((r for r in self.tables.get(entity, []) if r["id"] == args.get("id")), None)
            if row is None:
                raise McpError(-32004, f"{entity} not found", {"code": "not_found"})
            return dict(row)
        if name == "WorkOrder.update":
            self.writes.append(args)
            row = next(r for r in self.tables["WorkOrder"] if r["id"] == args["id"])
            row.update({k: v for k, v in args.items() if k != "id"}, updated_at=f"written-{len(self.writes)}")
            return dict(row)
        if name == "AgentMemory.create":
            self.memories.append(args)
            return {"id": f"mem-{len(self.memories)}"}
        if name == "endpoint.manufacturing.check_stock_availability":
            items = self.stock.get(args.get("work_order_id"), [])
            if isinstance(items, Exception):
                raise items
            return {"result": {"work_order_id": args.get("work_order_id"), "items": items}}
        if name == "endpoint.manufacturing.finite_schedule":
            return {"result": {"orders": self.schedule, "workstation_load": self.workstation_load}}
        if name in self.endpoints:
            return self.endpoints[name]
        raise AssertionError(f"unexpected call {name}")

    def _list(self, entity, args):
        rows = self.tables.get(entity, [])
        for key, wanted in args.items():
            if key in _CONTROLS or key in self.ignored_filters:
                continue
            if str(wanted).split(":", 1)[0] in DATE_OPERATORS:
                # The MCP schema declares date fields {"format": "date"}; REST takes these, MCP does not.
                raise McpError(-32602, f"Invalid value for {key}: expected a date", {"code": "invalid_params"})
            if key == "search":
                text = str(wanted).lower()
                rows = [r for r in rows if any(text in str(r.get(f) or "").lower() for f in ("number", "name", "code"))]
            else:
                allowed = set(str(wanted).split(","))
                rows = [r for r in rows if str(r.get(key)) in allowed]
        if args.get("sort_by"):
            rows = sorted(rows, key=lambda r: str(r.get(args["sort_by"]) or ""), reverse=args.get("sort_order") == "desc")
        offset, limit = args.get("offset", 0), args.get("limit", 200)
        return {"data": [dict(r) for r in rows[offset:offset + limit]], "total": len(rows)}

    def recorded_findings(self) -> list[dict]:
        """What AgentMemory holds, decoded the way a verifier reads it."""
        return [json.loads(m["content"][len(config.FINDING_PREFIX):]) for m in self.memories]


def agent_on(book, **kwargs) -> ProductionAgent:
    """The real agent, with no model behind it: tests drive it through _dispatch, as the loop does."""
    return ProductionAgent(book, llm=object(), model="gpt-4.1", **kwargs)


def finding(work_order, **over) -> dict:
    """A complete record_finding payload, as a model following the schema sends it."""
    return {"outcome": "answered", "work_order": work_order, "is_late": True, "currency": "INR",
            "blocking_causes": [], "contributing_causes": [], "evidence_records": [],
            "potentially_blocked_work_orders": [], "blocked_sales_orders": [], "rescheduled": [],
            "not_visible": [], "refusal_reason": None, **over}


def pin_today(monkeypatch):
    monkeypatch.setattr(config, "today", lambda: TODAY)
    monkeypatch.setattr(config, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.delenv("LLM_PROVIDER", raising=False)


# --------------------------------------------------------------- the fake is no kinder than the platform

def test_a_comma_list_is_or_and_an_unknown_value_matches_nothing():
    book = Book(tables={"WorkOrder": [work_order("WO-1", "draft"), work_order("WO-2", "completed")]})
    assert [r["number"] for r in book.list_all("WorkOrder", status="draft,not_started")] == ["WO-1"]
    assert book.list_all("WorkOrder", status="stopped") == []


def test_paging_reports_the_total_and_list_all_reads_every_page():
    book = Book(tables={"WorkOrder": [work_order(f"WO-{i}") for i in range(450)]})
    rows = book.list_all("WorkOrder")
    assert len(rows) == 450 and rows.total == 450 and rows.truncated is False
    assert sum(1 for name, _ in book.calls if name == "WorkOrder.list") == 3


def test_an_ignored_filter_widens_the_result_as_the_platform_would():
    book = Book(tables={"WorkOrder": [work_order("WO-1", "draft"), work_order("WO-2", "completed")]},
                ignored_filters={"status"})
    assert len(book.list_all("WorkOrder", status="draft")) == 2


def test_a_date_operator_is_rejected_as_the_mcp_schema_does():
    book = Book(tables={"WorkOrder": [work_order("WO-1")]})
    with pytest.raises(McpError):
        book.list_all("WorkOrder", planned_end_date="lt:2026-09-16")


def test_a_missing_record_and_a_denied_tool_raise_like_the_platform():
    book = Book(tables={"SalesOrder": []}, failing={"SalesOrder.get"})
    with pytest.raises(McpError) as denied:
        book.call("SalesOrder.get", {"id": "so-1"})
    assert denied.value.kind == "permission_denied"
    with pytest.raises(McpError) as missing:
        Book().call("WorkOrder.get", {"id": "nope"})
    assert missing.value.kind == "not_found"
