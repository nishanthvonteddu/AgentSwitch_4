"""query_records: the bounds it promises, checked without touching a tenant.

The failure this tool exists to prevent is a page being reported as the whole table, so most of
these assert on `total`, `truncated` and `too_many` rather than on the rows themselves.
"""
import pytest

from prod_agent import domain


class FakeMcp:
    """Records every .list call so a test can assert what was NOT fetched."""

    def __init__(self, total=0, rows=None, tools=("WorkOrder.list",), error=None):
        self.total, self._rows, self._tools, self._error = total, rows or [], set(tools), error
        self.calls = []
        # seat_capability falls through to a REST probe; give it something that fails closed.
        self.session = type("S", (), {"base": "https://example.invalid", "token": None,
                                      "with_reauth": staticmethod(lambda fn: fn())})()

    def has_tool(self, name):
        return name in self._tools

    def tool_names(self):
        return set(self._tools)

    def call(self, name, arguments=None):
        self.calls.append((name, dict(arguments or {})))
        if self._error:
            raise self._error
        limit = (arguments or {}).get("limit", 0)
        return {"data": self._rows[:limit], "total": self.total}


def _rows(n):
    return [{"id": f"id-{i}", "number": f"WO-2026-{i:05d}", "status": "draft"} for i in range(n)]


# --------------------------------------------------------------- counting costs no rows

def test_counting_question_fetches_one_row_not_the_table():
    mcp = FakeMcp(total=4312, rows=_rows(4312))
    out = domain.query_records(mcp, "WorkOrder", {"status": "draft"}, limit=0)
    assert out["total"] == 4312
    # Above the narrow threshold: the probe is the only call, and it asked for 1 row.
    assert len(mcp.calls) == 1
    assert mcp.calls[0][1]["limit"] == 1


def test_a_million_matches_reads_no_rows_and_says_why():
    mcp = FakeMcp(total=1_000_000, rows=_rows(500))
    out = domain.query_records(mcp, "WorkOrder", limit=200)
    assert out["too_many"] is True
    assert out["rows"] == [] and out["returned"] == 0
    assert out["total"] == 1_000_000
    assert "narrow" in out["instruction"].lower()
    assert len(mcp.calls) == 1, "must not page a set it has already decided is too large"


def test_empty_result_is_not_truncated():
    out = domain.query_records(FakeMcp(total=0), "WorkOrder", {"status": "draft"})
    assert out == {"entity": "WorkOrder", "filters": {"status": "draft"}, "total": 0,
                   "returned": 0, "rows": [], "truncated": False}


# --------------------------------------------------------------- the page is never all of it

def test_partial_page_is_flagged_and_carries_the_total():
    mcp = FakeMcp(total=53, rows=_rows(53))
    out = domain.query_records(mcp, "WorkOrder", limit=10)
    assert out["returned"] == 10 and out["total"] == 53
    assert out["truncated"] is True
    assert "10 of 53" in out["instruction"]
    assert "average" in out["instruction"], "must warn against aggregating over a page"


def test_full_result_is_not_flagged_truncated():
    mcp = FakeMcp(total=7, rows=_rows(7))
    out = domain.query_records(mcp, "WorkOrder", limit=50)
    assert out["returned"] == 7 and out["truncated"] is False
    assert "instruction" not in out


@pytest.mark.parametrize("asked,expected", [(1, 1), (50, 50), (500, 200), (10**6, 200)])
def test_limit_is_clamped_to_the_cap(asked, expected):
    mcp = FakeMcp(total=1000, rows=_rows(1000))
    domain.query_records(mcp, "WorkOrder", limit=asked)
    assert mcp.calls[-1][1]["limit"] == expected


def test_limit_zero_is_count_only_and_reads_no_rows():
    """The platform's own idiom: limit=0 returns the count and nothing else."""
    mcp = FakeMcp(total=1000, rows=_rows(1000))
    out = domain.query_records(mcp, "WorkOrder", {"status": "draft"}, limit=0)
    assert out["total"] == 1000 and out["returned"] == 0 and out["rows"] == []
    assert out["count_only"] is True
    assert len(mcp.calls) == 1 and mcp.calls[0][1]["limit"] == 1


def test_cap_is_enforced_in_code_not_by_the_caller():
    assert domain.QUERY_MAX_ROWS <= 200
    assert domain.QUERY_NARROW_ABOVE > domain.QUERY_MAX_ROWS


# --------------------------------------------------------------- read-only, seat-scoped

def test_never_calls_a_write_tool():
    mcp = FakeMcp(total=5, rows=_rows(5))
    domain.query_records(mcp, "WorkOrder")
    assert all(name.endswith(".list") for name, _ in mcp.calls)


def test_entity_outside_the_seat_is_refused_through_the_existing_probe():
    mcp = FakeMcp(total=0, tools=("WorkOrder.list",))
    out = domain.query_records(mcp, "SalarySlip")
    assert out["error"] == "not readable by this seat"
    assert out["entity"] == "SalarySlip"
    assert not any(name.startswith("SalarySlip") for name, _ in mcp.calls), "must not call a tool it lacks"


def test_paging_controls_cannot_be_smuggled_in_as_filters():
    mcp = FakeMcp(total=100, rows=_rows(100))
    domain.query_records(mcp, "WorkOrder", {"limit": 999, "offset": 50, "status": "draft"}, limit=10)
    sent = mcp.calls[-1][1]
    assert sent["limit"] == 10, "a filter named limit must not raise the cap"
    assert "offset" not in sent
    assert sent["status"] == "draft"


def test_none_valued_filters_are_dropped():
    mcp = FakeMcp(total=3, rows=_rows(3))
    domain.query_records(mcp, "WorkOrder", {"status": "draft", "bom_id": None})
    assert mcp.calls[-1][1].get("bom_id", "absent") == "absent"


# --------------------------------------------------------------- failures stay legible

def test_a_rejected_filter_returns_the_platforms_own_message():
    from prod_agent.mcp_client import McpError
    err = McpError(-32602, "Unknown filter 'nope' for WorkOrder. It was previously ignored.")
    out = domain.query_records(FakeMcp(error=err), "WorkOrder", {"nope": "x"})
    assert "Unknown filter" in out["error"]
    assert out["filters_sent"] == ["nope"]
    assert "instruction" in out


def test_sort_is_passed_through_when_asked():
    mcp = FakeMcp(total=5, rows=_rows(5))
    domain.query_records(mcp, "WorkOrder", sort_by="planned_end_date", newest_first=False)
    assert mcp.calls[0][1]["sort_by"] == "planned_end_date"
    assert mcp.calls[0][1]["sort_order"] == "asc"


def test_missing_total_falls_back_to_counting_the_rows():
    class NoTotal(FakeMcp):
        def call(self, name, arguments=None):
            self.calls.append((name, dict(arguments or {})))
            return {"data": self._rows[:(arguments or {}).get("limit", 0)]}

    out = domain.query_records(NoTotal(total=None, rows=_rows(3)), "WorkOrder", limit=5)
    assert out["total"] == 1, "no total in the envelope: fall back to what the probe returned"


# --------------------------------------------------------------- wiring

def test_agent_exposes_it_as_a_fallback_and_guards_it_like_other_reads():
    from prod_agent.agent import ProductionAgent, SYSTEM_PROMPT

    assert "query_records" in ProductionAgent.PARALLEL_SAFE
    assert "query_records" in ProductionAgent.REPEAT_GUARDED
    assert "query_records" in SYSTEM_PROMPT
    assert "FALLBACK" in SYSTEM_PROMPT, "precedence over the specific tools must be stated in the prompt"


def test_the_model_is_not_told_to_use_unverified_filter_syntax():
    """CSV-means-OR and lt:/gte:/between: are verified on REST, not over MCP. A value format the server
    does not accept matches nothing and returns total 0, which reads as "no such records" — the silent
    wrong answer this tool exists to avoid. Advertise only equality until a live tenant confirms it."""
    from prod_agent import agent
    text = open(agent.__file__).read()
    i = text.index('"filters": {"type": "object"')
    described = text[i:i + 500]
    assert "NOT confirmed" in described
    assert "call once per value" in described, "the model needs a working alternative, not just a prohibition"
