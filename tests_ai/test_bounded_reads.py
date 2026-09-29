"""Reads are bounded, and a partial read never passes for a whole one.

An unfiltered `list_all` on a large table is thousands of sequential calls whose result is then cut
mid-JSON by the tool-content limit, leaving the model a fragment that looks complete. These cover both
halves: the ceiling that stops the paging, and the note that makes a cut visible.
"""
import json

import pytest

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


def test_status_scans_are_narrowed_at_the_server():
    """Confirmed over MCP on Suryodaya 2026-09-29: status=draft,not_started returned 78 = 40 + 38."""
    text = open(domain.__file__).read()
    assert 'mcp.list_all("WorkOrder", status=_csv(OPEN_WO))' in text
    assert 'mcp.list_all("EngineeringChangeOrder", status=_csv(PENDING_ECO))' in text
    assert domain._csv(domain.OPEN_WO) == "draft,in_progress,not_started,stopped"


def test_date_operators_are_not_used_on_this_interface():
    """The MCP schema declares planned_end_date {"format": "date"} and rejects lt:/gte:/between:,
    though REST accepts them on the same login. Filed as a bug; not usable here meanwhile."""
    text = open(domain.__file__).read()
    assert "lt:" not in text.split("def _csv")[0], "no date operator may reach an MCP .list call"


def test_python_status_checks_are_kept_as_well():
    """A server filter this platform ignores must not silently widen the result."""
    text = open(domain.__file__).read()
    assert 'if wo.get("status") not in OPEN_WO' in text
    assert 'if c.get("status") in OPEN_JOB_CARD' in text


def test_company_context_reads_one_row_not_the_table():
    text = open(domain.__file__).read()
    assert 'mcp.list_all("Company")' not in text
    assert 'mcp.call("Company.list", {"limit": 1})' in text


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
